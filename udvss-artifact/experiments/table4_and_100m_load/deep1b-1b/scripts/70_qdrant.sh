#!/bin/bash
# Qdrant at Deep1B 1B right after Vespa (user 2026-10-27), memory-saving mode:
# vectors AND HNSW graph on disk (QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1); HNSW m=30,
# ef_construct=360, 1 shard, same as 100M. Load + index only: Qdrant gets cores 0-39
# and 40 optimizer threads (100M: cpuset 0-7, 8 threads); measurement restarts it
# on the standard compose (cpuset 0-7, 8 search/optimizer threads) as at 100M.
# Also a Vespa stall guard: if Vespa (paged) adds < 300 docs/s for 2 h, its feed is
# stopped and recorded as stalled.  Marker "=== QDRANT 1B DONE ===".
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
QL=$X/logs/qdrant_1b.log; MT=$X/results/vespa_1b_paged_mem.tsv
log "70: waiting for Vespa 1B (paged) to finish; stall guard on"
until grep -q "^\[[^]]*\] === VESPA 1B PAGED DONE ===$" $LOG; do
  python3 - $X/logs/vespa_1b_paged.log <<'PY' && { P=$(ps -eo pid,args | awk '$0 ~ /vespa_feed_mp.py deep1b1b/ && $0 !~ /awk/ {print $1}'); log "70: Vespa paged STALLED (<300 docs/s over 2 h) -> stopping its feeder $P"; kill $P; }
# rate from the feeder's own progress lines ("fed N/... Ts"): Vespa's totalCount
# from a search under heavy feed is not reliable (it swung 53.7M..59.8M in 3 min)
import re, sys
pts = [(float(t), int(n)) for n, t in re.findall(r"fed +(\d+)/\d+ +([\d.]+)s", open(sys.argv[1]).read())]
if len(pts) < 2: sys.exit(1)
end = pts[-1]; old = [p for p in pts if end[0] - p[0] >= 7200]
if not old: sys.exit(1)
st = old[-1]; rate = (end[1] - st[1]) / (end[0] - st[0])
sys.exit(0 if end[1] > 1.5e8 and rate < 300 else 1)
PY
  sleep 300
done
log "70: Vespa done -> stopping Vespa and deleting its data for disk space"
H "docker exec vb-vespa /opt/vespa/bin/vespa-stop-services; cd ~/vespa-bench && docker compose stop -t 900 && rm -rf volumes/vespa/*" >> $QL 2>&1
log "70: disk after Vespa removal: $(df -h / | tail -1)"
cat > /tmp/udvss-job/qdrant-load.yml <<'EOF'
services:
  qdrant:
    cpuset: "0-39"
    environment:
      QDRANT__STORAGE__PERFORMANCE__MAX_OPTIMIZATION_THREADS: "40"
EOF
scp -q -P 31234 /tmp/udvss-job/qdrant-load.yml ubuntu@127.0.0.1:qdrant-bench/docker-compose.load1b.yml
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml up -d" >> $QL 2>&1
for i in $(seq 1 60); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 5; done
log "70: Qdrant up for load+index (cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant")); vectors+HNSW on disk"
cd /home/dev_path/corun
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 QB_INDEX_TIMEOUT=604800 taskset -c 40-47 .venv/bin/python -u -c "
import sys, json; sys.path.insert(0, 'scripts'); import engine_qdrant as E
print(json.dumps(E.load('deep1b1b'), default=str))" >> $QL 2>&1
RC=$?
if [ $RC != 0 ]; then
  log "70: RESULT Qdrant 1B (vectors+HNSW on disk) load/index FAILED: $(grep -aE 'Error|error|Exception' $QL | tail -1 | cut -c1-200)"
  log "=== QDRANT 1B DONE ==="; exit 1
fi
log "70: Qdrant 1B loaded + indexed: $(tail -1 $QL | cut -c1-300)"
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml stop -t 600 && docker compose -f docker-compose.yml up -d" >> $QL 2>&1
for i in $(seq 1 120); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 5; done
log "70: Qdrant restarted on the standard compose (cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant")); measuring"
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh qdrant deep1b1b --no-load >> $QL 2>&1 \
  && log "70: qdrant deep1b1b measured" || log "70: qdrant measurement FAILED (see $QL)"
log "=== QDRANT 1B DONE ==="
