#!/bin/bash
# v3: v2's "(a)" deploy of the non-paged schema was REJECTED (HTTP 400: Vespa requires the
# paged-setting-removal validation override; v2 did not check the deploy rc), so v2's "(a)"
# run actually measured the PAGED schema under the 48G cap = the (b) result. Relabel it,
# then run the real (a): non-paged schema (override added in engine_vespa.py) under 48G.
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; L=$D/driver.log; S=$D/state
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
BASE="cd /home/ubuntu/vespa-bench && docker compose -f docker-compose.yml"
CAP="$BASE -f docker-compose.mem48.yml"
PY=/home/dev_path/corun/.venv/bin/python
tc() { cd /home/dev_path/corun && timeout 150 $PY -c "import sys; sys.path.insert(0,'scripts'); import engine_vespa as E; print(E._total_count())" 2>/dev/null | tail -1; }
cg() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' vb-vespa").scope; }
oomk() { H "awk '\$1==\"oom_kill\"{print \$2}' $(cg)/memory.events"; }
replay() { H "docker exec vb-vespa bash -c '/opt/vespa/bin/vespa-logfmt -S searchnode -N /opt/vespa/logs/vespa/vespa.log | grep replay.progress | tail -1'" | grep -oE '"progress":[0-9.]+' ; }
wait_serve() {   # <phase> <max_min> <max_oom>
  local t0=$(date +%s) ok=0
  while [ $(( $(date +%s) - t0 )) -lt $(( $2 * 60 )) ]; do
    n=$(tc); k=$(oomk); m=$(H "cat $(cg)/memory.current")
    echo "$(date -u +%FT%TZ) $1 totalCount=${n:-NA} oom_kill=$k mem=$((m/1073741824))G $(replay)" >> $D/serve_poll.log
    [ "$n" = 100000000 ] && ok=$((ok+1)) || ok=0
    [ $ok -ge 2 ] && { log "$1: serving, totalCount=100M after $(( ($(date +%s)-t0)/60 )) min, oom_kill=$k"; return 0; }
    [ "${k:-0}" -ge $3 ] && { log "$1: oom_kill=$k reached limit $3 after $(( ($(date +%s)-t0)/60 )) min, not serving (totalCount=${n:-NA})"; return 1; }
    sleep 30
  done
  log "$1: not serving after $2 min (totalCount=${n:-NA}, oom_kill=$k)"; return 1
}
deploy() {  # VB_PAGED value
  cd /home/dev_path/corun && VB_PAGED=$1 $PY - >> $L 2>&1 <<'PY'
import sys, json, urllib.request; sys.path.insert(0, 'scripts'); import engine_vespa as E
req = urllib.request.Request(f"{E.CONFIG}/application/v2/tenant/default/prepareandactivate",
      data=E._package(96), method="POST", headers={"Content-Type": "application/zip"})
out = json.loads(urllib.request.urlopen(req, timeout=900).read() or b"{}")
print(json.dumps({k: out.get(k) for k in ("message", "configChangeActions")})[:1500])
PY
}
wait_cfg() { for i in $(seq 120); do H "curl -sf localhost:19071/state/v1/health" >/dev/null && return 0; sleep 5; done; return 1; }
clean_stop() {   # flush everything to disk so the next start has (almost) nothing to replay
  H "docker exec vb-vespa /opt/vespa/bin/vespa-proton-cmd --local prepareRestart" >> $L 2>&1
  H "docker exec vb-vespa /opt/vespa/bin/vespa-stop-services" >> $L 2>&1
  H "docker stop -t 900 vb-vespa" >> $L 2>&1
}
log "=== v3: waiting for the in-flight measurement (really paged) to finish"
while pgrep -f "run_engine.sh vespa deep1b100m" >/dev/null; do sleep 15; done
E=$R/engines
mv $E/results/vespa/deep1b100m_mem48_default $E/results/vespa/deep1b100m_mem48_paged
mv $E/logs/vespa_deep1b100m_mem48_default.log $E/logs/vespa_deep1b100m_mem48_paged.log
sed -i 's/^vespa\tdeep1b100m_mem48_default\t/vespa\tdeep1b100m_mem48_paged\t/' $E/results/chosen_c.tsv
log "(b) RESULT (relabelled): the 18:01-19:0xZ run used the PAGED schema (non-paged deploy had been rejected) under the 48G cap -> results/vespa/deep1b100m_mem48_paged"
log "(a) deploying DEFAULT (non-paged) schema with paged-setting-removal override"
deploy 0; grep -q '"message": "Session' <(tail -1 $L) || { log "FATAL: non-paged deploy failed (see above)"; clean_stop; log "=== VESPA MEM48 DONE ==="; exit 1; }
sd=$(H "curl -s localhost:19071/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default/content/schemas/vec.sd")
echo "$sd" | grep -q "attribute: paged" && { log "FATAL: active schema still paged"; clean_stop; log "=== VESPA MEM48 DONE ==="; exit 1; }
log "(a) active schema verified non-paged"
clean_stop; log "(a) clean stop done"
H "$CAP up -d" >> $L 2>&1; echo "$(cg)" >> $S.cg
log "(a) container up with 48G cap (memory.max=$(H "cat $(cg)/memory.max")), NON-paged schema, flushed index"
if wait_serve "(a) default" 120 3; then
  log "(a) RESULT: default (non-paged) config SERVES under 48G -> measuring"
  RUN_TAG=mem48_default bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $D/run_a.out 2>&1 \
    && log "(a) measurement done" || log "(a) measurement FAILED"
else
  log "(a) RESULT: default (non-paged) config does NOT serve under 48G (memory.events: $(H "tr '\n' ' ' < $(cg)/memory.events"))"
fi
H "journalctl -k --since '-3h' | grep -E 'Killed process|cgroup out'" > $D/oom_kernel_a3.log 2>&1
clean_stop; log "vespa stopped cleanly"; log "=== VESPA MEM48 DONE ==="
