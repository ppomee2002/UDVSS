#!/bin/bash
# Vespa 1B in the memory-saving mode (user 2026-10-27): the same schema/HNSW
# params as 100M plus `attribute: paged` (vectors mmap'ed from disk, HNSW graph in
# memory) -- the mode used for Vespa in mem48/mem40. Switch as in mem48 v2:
# deploy paged -> clean stop (prepareRestart, stop services) -> start -> feed.
# The default-mode feed had reached ~26M docs; the feed restarts at 0 (PUT by
# doc id is idempotent). Feed only: Vespa cpuset 0-15,24-39, feeder 16-23,40-47.
# Marker "=== VESPA 1B PAGED DONE ===".
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
VL=$X/logs/vespa_1b_paged.log; MT=$X/results/vespa_1b_paged_mem.tsv
cd /home/dev_path/corun
log "61: deploying the PAGED schema"
VB_PAGED=1 .venv/bin/python - >> $VL 2>&1 <<'PY' || { log "61: paged deploy FAILED (see $VL)"; exit 1; }
import sys, json, urllib.request; sys.path.insert(0, 'scripts'); import engine_vespa as E
req = urllib.request.Request(f"{E.CONFIG}/application/v2/tenant/default/prepareandactivate",
      data=E._package(96), method="POST", headers={"Content-Type": "application/zip"})
out = json.loads(urllib.request.urlopen(req, timeout=900).read() or b"{}")
print(json.dumps({k: out.get(k) for k in ("message", "configChangeActions")})[:1500])
PY
log "61: clean stop + restart so the paged attribute takes effect"
H "docker exec vb-vespa /opt/vespa/bin/vespa-proton-cmd --local prepareRestart" >> $VL 2>&1
H "docker exec vb-vespa /opt/vespa/bin/vespa-stop-services" >> $VL 2>&1
H "docker stop -t 900 vb-vespa" >> $VL 2>&1
H "cd ~/vespa-bench && docker compose up -d" >> $VL 2>&1
for i in $(seq 1 360); do
  n=$(curl -s "http://127.0.0.1:8080/search/?yql=select%20*%20from%20sources%20*%20where%20true&hits=0" | python3 -c "import json,sys;print(json.load(sys.stdin)['root']['fields']['totalCount'])" 2>/dev/null)
  [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null && break; sleep 10
done
log "61: vespa serving again after restart (totalCount=${n:-NA})"
H "docker exec vb-vespa bash -c 'grep -rl paged /opt/vespa/var/db/vespa/config_server/serverdb/tenants/default/sessions/*/schemas/*.sd 2>/dev/null | tail -1'" >> $VL 2>&1
rm -f $X/VESPA_SAMPLER_STOP
[ -s $MT ] || echo -e "utc\tcontainer_mem\thost_avail_g\tdocs" > $MT
( while [ ! -e $X/VESPA_SAMPLER_STOP ]; do
    m=$(H "docker stats --no-stream --format '{{.MemUsage}}' vb-vespa" 2>/dev/null)
    n=$(curl -s "http://127.0.0.1:8080/search/?yql=select%20*%20from%20sources%20*%20where%20true&hits=0" | python3 -c "import json,sys;print(json.load(sys.stdin)['root']['fields']['totalCount'])" 2>/dev/null)
    echo -e "$(date -u +%FT%TZ)\t$m\t$(awk '/MemAvailable/{printf "%.1f", $2/2^20}' /proc/meminfo)\t$n" >> $MT
    sleep 60
  done ) &
SP=$!
H "docker update --cpuset-cpus 0-15,24-39 vb-vespa" >> $VL 2>&1
log "61: paged feed of 1e9 docs from 0 (Vespa cpuset 0-15,24-39, feeder 24x16 on 16-23,40-47)"
VB_INDEX_TIMEOUT=172800 taskset -c 16-23,40-47 .venv/bin/python -u \
  /home/dev_path/experiments/scale-deep/engines/vespa_feed_mp.py deep1b1b 24 16 >> $VL 2>&1
RC=$?
H "docker update --cpuset-cpus 12-19 vb-vespa" >> $VL 2>&1
LAST=$(grep -aoE "fed +[0-9]+/" $VL | tail -1 | tr -dc 0-9)
if [ $RC = 0 ]; then
  log "61: paged feed + index done (1e9); measuring on cpuset 12-19"
  bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh vespa deep1b1b --no-load >> $VL 2>&1 \
    && log "61: vespa deep1b1b (paged) measured" || log "61: vespa paged measurement FAILED (see $VL)"
else
  log "61: RESULT Vespa 1B (paged) did not load on the 125 GB host: feed stopped at ~${LAST:-?} docs ($(grep -aE '507|feed error' $VL | tail -1 | cut -c1-200)); last memory sample: $(tail -1 $MT)"
fi
touch $X/VESPA_SAMPLER_STOP; kill $SP 2>/dev/null
log "=== VESPA 1B PAGED DONE ==="
