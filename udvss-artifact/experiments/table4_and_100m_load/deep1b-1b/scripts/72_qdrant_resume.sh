#!/bin/bash
# Qdrant 1B resume after the 13:37Z host OOM (user 2026-10-28: 16 optimizer threads, 110g cap).
# Data (1B points, ~11% indexed) is on disk: restart Qdrant with mem_limit 110g (no swap),
# cpuset 0-39, 16 optimizer threads; wait until indexed (count timeouts tolerated);
# then measure on the standard compose (cpuset 0-7, 8 search/opt threads) + the 110g cap
# kept as host protection (onlinejudge lives on this host). Marker "=== QDRANT 1B DONE ===".
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
QL=$X/logs/qdrant_1b.log; T=/tmp/udvss-job
cat > $T/qdrant-load.yml <<'Y'
services:
  qdrant:
    cpuset: "0-39"
    mem_limit: 110g
    memswap_limit: 110g
    environment:
      QDRANT__STORAGE__PERFORMANCE__MAX_OPTIMIZATION_THREADS: "16"
Y
cat > $T/qdrant-cap.yml <<'Y'
services:
  qdrant:
    mem_limit: 110g
    memswap_limit: 110g
Y
scp -q -P 31234 $T/qdrant-load.yml ubuntu@127.0.0.1:qdrant-bench/docker-compose.load1b.yml
scp -q -P 31234 $T/qdrant-cap.yml ubuntu@127.0.0.1:qdrant-bench/docker-compose.cap110.yml
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml up -d" >> $QL 2>&1
log "72: Qdrant restarted after OOM: cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant"), mem $(H "docker inspect -f '{{.HostConfig.Memory}}' qb-qdrant") B, 16 optimizer threads"
cd /home/dev_path/corun
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 taskset -c 40-47 .venv/bin/python -u - >> $QL 2>&1 <<'PY'
import sys, time, json; sys.path.insert(0, 'scripts'); import engine_qdrant as E
t0 = time.time()
while True:
    try:
        ok, info = E.wait_indexed('deep1b1b', 1_000_000_000, timeout_s=3600)
        if ok:
            print(json.dumps(dict(resume_index_seconds=round(time.time() - t0), **info), default=str)); break
    except Exception as e:  # noqa: BLE001  (server busy / restarting: keep waiting)
        print(f"  wait error ({time.time()-t0:.0f}s): {str(e)[:200]}", flush=True); time.sleep(60)
PY
log "72: Qdrant 1B indexed: $(tail -1 $QL | cut -c1-300)"
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml stop -t 600 && docker compose -f docker-compose.yml -f docker-compose.cap110.yml up -d" >> $QL 2>&1
for i in $(seq 1 720); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 5; done
log "72: Qdrant on the standard compose (cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant"), 8 threads, cap 110g); measuring"
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh qdrant deep1b1b --no-load >> $QL 2>&1 \
  && log "72: qdrant deep1b1b measured" || log "72: qdrant measurement FAILED (see $QL)"
log "=== QDRANT 1B DONE ==="
