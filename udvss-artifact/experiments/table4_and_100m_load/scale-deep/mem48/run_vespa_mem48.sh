#!/bin/bash
# Vespa 100M under a 48 GiB cgroup cap (docker mem_limit = memswap_limit = 48g).
#  (a) original config (in-memory tensor attribute + HNSW), index from the 100M run kept:
#      does it come up and serve at all? Wait up to 120 min or 3 OOM kills.
#      Serves -> full measurement (RUN_TAG=mem48_default); otherwise record the failure.
#  (b) VB_PAGED=1: vectors as paged attribute (mmap), graph in memory; deploy + restart
#      (no re-feed), wait totalCount==100M, full measurement (RUN_TAG=mem48_paged).
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; L=$D/driver.log; S=$D/state
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
UP="cd /home/ubuntu/vespa-bench && docker compose -f docker-compose.yml -f docker-compose.mem48.yml"
PY=/home/dev_path/corun/.venv/bin/python
tc() { cd /home/dev_path/corun && timeout 150 $PY -c "import sys; sys.path.insert(0,'scripts'); import engine_vespa as E; print(E._total_count())" 2>/dev/null | tail -1; }
cg() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' vb-vespa").scope; }
oomk() { H "awk '\$1==\"oom_kill\"{print \$2}' $(cg)/memory.events"; }
errs() { H "docker exec vb-vespa bash -c '/opt/vespa/bin/vespa-logfmt -l error,fatal,warning -N /opt/vespa/logs/vespa/vespa.log | grep -iE \"memory|oom|signal|exit|killed|abort\" | tail -15'" >> $L 2>&1; }
# wait_serve <phase> <max_min> <max_oom>: 0 when totalCount==100M twice in a row
wait_serve() {
  local t0=$(date +%s) ok=0
  while [ $(( $(date +%s) - t0 )) -lt $(( $2 * 60 )) ]; do
    n=$(tc); k=$(oomk); m=$(H "cat $(cg)/memory.current")
    echo "$(date -u +%FT%TZ) $1 totalCount=${n:-NA} oom_kill=$k mem=$((m/1073741824))G" >> $D/serve_poll.log
    [ "$n" = 100000000 ] && ok=$((ok+1)) || ok=0
    [ $ok -ge 2 ] && { log "$1: serving, totalCount=100M after $(( ($(date +%s)-t0)/60 )) min, oom_kill=$k"; return 0; }
    [ "${k:-0}" -ge $3 ] && { log "$1: oom_kill=$k reached limit $3 after $(( ($(date +%s)-t0)/60 )) min, not serving (totalCount=${n:-NA})"; return 1; }
    sleep 30
  done
  log "$1: not serving after $2 min (totalCount=${n:-NA}, oom_kill=$k)"; return 1
}
log "=== vespa mem48 started"
H "docker stop vb-vespa" >> $L 2>&1
H "$UP up -d" >> $L 2>&1
log "(a) container up with 48G cap: $(H "cat $(cg)/memory.max") bytes, swap.max=$(H "cat $(cg)/memory.swap.max"), cgroup $(cg)"
echo "$(cg)" > $S.cg
if wait_serve "(a) default" 120 3; then
  log "(a) RESULT: default config SERVES under 48G -> measuring"
  RUN_TAG=mem48_default bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $D/run_a.out 2>&1 \
    && log "(a) measurement done" || log "(a) measurement FAILED (see engines/logs/vespa_deep1b100m_mem48_default.log)"
else
  log "(a) RESULT: default config does NOT serve under 48G (memory.events: $(H "tr '\n' ' ' < $(cg)/memory.events"))"
  errs
fi
log "(b) deploying paged schema"
cd /home/dev_path/corun && VB_PAGED=1 $PY - >> $L 2>&1 <<'PY'
import sys, json, urllib.request; sys.path.insert(0, 'scripts'); import engine_vespa as E
pkg = E._package(96); open('/home/dev_path/experiments/scale-deep/mem48/paged_pkg.zip', 'wb').write(pkg)
req = urllib.request.Request(f"{E.CONFIG}/application/v2/tenant/default/prepareandactivate",
      data=pkg, method="POST", headers={"Content-Type": "application/zip"})
out = json.loads(urllib.request.urlopen(req, timeout=900).read() or b"{}")
print(json.dumps({k: out.get(k) for k in ("message", "configChangeActions")})[:2000])
PY
[ $? = 0 ] || { log "(b) FATAL: deploy failed"; log "=== VESPA MEM48 DONE ==="; exit 1; }
H "docker restart vb-vespa" >> $L 2>&1
echo "$(cg)" >> $S.cg
log "(b) restarted with paged schema; oom_kill counter now $(oomk)"
k0=$(oomk)
if wait_serve "(b) paged" 180 $(( ${k0:-0} + 3 )); then
  RUN_TAG=mem48_paged bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $D/run_b.out 2>&1 \
    && log "(b) measurement done" || log "(b) measurement FAILED (see engines/logs/vespa_deep1b100m_mem48_paged.log)"
else
  log "(b) RESULT: paged config does NOT serve under 48G"; errs
fi
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped (index kept)"; log "=== VESPA MEM48 DONE ==="
