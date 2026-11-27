#!/bin/bash
# Vespa at Deep1B 1B on 94, right after the Ceph 1B run (user 2026-10-26).
# Same Vespa setup as scale-deep 100M: default (in-memory) HNSW max-links 30 /
# explore-at-insert 360, cpuset 12-19, VB_MEM_LIMIT 0.85 / VB_DISK_LIMIT 0.90,
# parallel feeder 8 procs x 16 threads on cores 9-11,20-23,44-47. 125 GB host:
# expected outcome = feed blocked by Vespa's memory limit (HTTP 507) before 1e9.
#  1. wait for "=== 1B ALL DONE ===" (Ceph measured)
#  2. if $X/OK_DELETE_CEPH exists: stop the Ceph 1B cluster, delete /vstart-extra/d1b
#  3. compose up, deploy, sample memory, parallel feed
#  4. full load -> run_engine (FAST_PROTO as Ceph); blocked -> record "cannot load"
# Marker "=== VESPA 1B DONE ===".
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
VL=$X/logs/vespa_1b.log; MT=$X/results/vespa_1b_mem.tsv
log "60: waiting for the Ceph 1B run to finish (its all-done marker)"
N0=$(wc -l < $LOG)
until tail -n +$((N0 + 1)) $LOG | grep -q "^\[[^]]*\] === 1B ALL DONE ===$"; do
  tail -1 $LOG | grep -q FATAL && { log "60: Ceph chain FATAL -> Vespa not started"; exit 1; }
  sleep 120
done
[ -e $X/OK_DELETE_CEPH ] || { log "60: no OK_DELETE_CEPH -> not deleting the Ceph cluster, Vespa not started"; exit 1; }
touch $X/WATCH_STOP
for p in $(ps -eo pid,args | awk '$0 ~ /-c \/vstart-extra\/d1b\/ceph.conf/ && $2 ~ /crimson-osd|ceph-mon|ceph-mgr/ {print $1}'); do kill $p; done
sleep 30
for p in $(ps -eo pid,args | awk '$0 ~ /-c \/vstart-extra\/d1b\/ceph.conf/ && ($2 ~ /crimson-osd|ceph-mon|ceph-mgr|ceph-run/ || $3 ~ /ceph-run/) {print $1}'); do kill -9 $p; done
rm -rf $V
log "60: Ceph 1B cluster stopped and $V deleted; disk: $(df -h / | tail -1)"

H "cd ~/vespa-bench && docker compose up -d" >> $VL 2>&1 || { log "60: compose up failed"; exit 1; }
for i in $(seq 1 120); do
  [ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:19071/state/v1/health)" = 200 ] && break; sleep 5
done
cd /home/dev_path/corun
.venv/bin/python -u -c "import sys; sys.path.insert(0,'scripts'); import engine_vespa as E; E.deploy(96)" >> $VL 2>&1 \
  || { log "60: deploy failed (see $VL)"; exit 1; }
log "60: vespa up + schema deployed (default in-memory HNSW, mem limit 0.85)"

[ -s $MT ] || echo -e "utc\tcontainer_mem\thost_avail_g\tdocs" > $MT
( while [ ! -e $X/VESPA_SAMPLER_STOP ]; do
    m=$(H "docker stats --no-stream --format '{{.MemUsage}}' vb-vespa" 2>/dev/null)
    n=$(curl -s "http://127.0.0.1:8080/search/?yql=select%20*%20from%20sources%20*%20where%20true&hits=0" | python3 -c "import json,sys;print(json.load(sys.stdin)['root']['fields']['totalCount'])" 2>/dev/null)
    echo -e "$(date -u +%FT%TZ)\t$m\t$(awk '/MemAvailable/{printf "%.1f", $2/2^20}' /proc/meminfo)\t$n" >> $MT
    sleep 60
  done ) &
SP=$!
# Feed only (user 2026-10-27: "use every core, for the load only"): Vespa gets 32
# threads (physical 0-15 + siblings 24-39), the feeder 16 (16-23 + 40-47) -- at
# 100M the feed was Vespa-CPU-bound (~7 Vespa cores per 4.4k docs/s, ~1.3k docs/s
# per feeder process). Memory per
# document does not depend on cores, so where the memory limit stops the feed is
# unchanged; only the time to get there shrinks. Measurement (if the load ever
# completes) runs back on cpuset 12-19 as at 100M.
H "docker update --cpuset-cpus 0-15,24-39 vb-vespa" >> $VL 2>&1
log "60: parallel feed of 1e9 docs (feed only: Vespa cpuset 0-15,24-39 = 32 threads, feeder 24 procs x 16 threads on 16-23,40-47)"
VB_INDEX_TIMEOUT=172800 taskset -c 16-23,40-47 .venv/bin/python -u \
  /home/dev_path/experiments/scale-deep/engines/vespa_feed_mp.py deep1b1b 24 16 >> $VL 2>&1
RC=$?
LAST=$(grep -aoE "fed +[0-9]+/" $VL | tail -1 | tr -dc 0-9)
H "docker update --cpuset-cpus 12-19 vb-vespa" >> $VL 2>&1
log "60: Vespa cpuset back to 12-19 ($(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' vb-vespa"))"
if [ $RC = 0 ]; then
  log "60: feed + index done (1e9); measuring"
  bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh vespa deep1b1b --no-load >> $VL 2>&1 \
    && log "60: vespa deep1b1b measured" || log "60: vespa measurement FAILED (see $VL)"
else
  log "60: RESULT Vespa 1B cannot load on the 125 GB host: feed stopped at ~${LAST:-?} docs ($(grep -aE '507|feed error' $VL | tail -1 | cut -c1-200)); last memory sample: $(tail -1 $MT)"
fi
touch $X/VESPA_SAMPLER_STOP; kill $SP 2>/dev/null
H "cd ~/vespa-bench && docker exec vb-vespa /opt/vespa/bin/vespa-stop-services; docker compose stop -t 900" >> $VL 2>&1
log "=== VESPA 1B DONE ==="
