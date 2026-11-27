#!/bin/bash
# Qdrant 1B: wait for the index (as 74, no PATCH except re-arming 6 jobs after a restart),
# 76 = 75 with the ORIGINAL measurement (user: the deadline variant was not wanted):
# run_engine.sh FAST protocol as before. (75 header kept below for reference)
# then measure with a HARD DEADLINE (user 2026-10-28: QPS must exist by Fri 10/30 morning KST).
# Measurement conditions as 100M/UDVSS: standard compose (cpuset 0-7, 8 search threads) + cap110,
# client taskset 20-23,44-47, 1024-vector batches, 120 s time-bound runs, 1 rep.
# Priority order: ef64 C=20 -> ef64 C=40 (only if C=20 took < 30 min) -> best C (C=40 only if
# >= 1.03x C=20) -> ef 16, 128, 32, 256, 10, 512, 1024. A run is skipped if
# now + duration of the previous run > DEADLINE (default 2026-10-30T00:00Z = Fri 09:00 KST).
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
QL=$X/logs/qdrant_1b.log; IT=$X/results/qdrant_1b_index.tsv; U=http://127.0.0.1:6333/collections/wg_deep1b1b
DL=$(date -u -d "${DEADLINE:-2026-10-30T00:00:00Z}" +%s)
patch() { curl -s --max-time 120 -X PATCH $U -H 'Content-Type: application/json' -d "{\"optimizers_config\":{\"max_optimization_threads\":$1}}"; }
CG() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' qb-qdrant").scope; }
log "76: waiting for the index (6 jobs), then run_engine FAST protocol"
ok=0; lastid=$(H "docker inspect -f '{{.Id}} {{.RestartCount}}' qb-qdrant")
while :; do
  info=$(curl -s --max-time 120 $U | python3 -c "import json,sys;r=json.load(sys.stdin)['result'];print(r['status'],r['optimizer_status'] if isinstance(r['optimizer_status'],str) else 'err',r.get('points_count'),r.get('indexed_vectors_count'),r.get('segments_count'))" 2>/dev/null)
  c=$(CG); m=$(H "awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{printf \"%.1f %.1f\", a/2^30, f/2^30}' $c/memory.stat; echo -n ' '; awk '\$1==\"oom_kill\"{print \$2}' $c/memory.events")
  id=$(H "docker inspect -f '{{.Id}} {{.RestartCount}}' qb-qdrant")
  read an fi ok_k <<< "$m"; read st os pts idx seg <<< "$info"
  if [ "$id" != "$lastid" ]; then
    log "76: Qdrant restarted ($id) -> re-arm 6 jobs"
    until curl -sf --max-time 10 http://127.0.0.1:6333/healthz >/dev/null; do sleep 10; done; sleep 60
    log "76: PATCH 6: $(patch 6)"; lastid=$id
  fi
  echo -e "$(date -u +%FT%TZ)\t6\t$an\t$fi\t$ok_k\t${id#* }\t$st/$os\t$pts\t$idx\t$seg" >> $IT
  if [ "$st" = green ] && [ "$os" = ok ] && [ -n "$pts" ] && [ "$idx" = "$pts" ]; then ok=$((ok+1)); else ok=0; fi
  [ $ok -ge 4 ] && break
  sleep 60
done
n=$(curl -s --max-time 1800 -X POST $U/points/count -H 'Content-Type: application/json' -d '{"exact":true}')
log "76: Qdrant 1B indexed (green, indexed=$idx, segments=$seg); exact count: $n"
patch null >/dev/null
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml stop -t 900 && docker compose -f docker-compose.yml -f docker-compose.cap110.yml up -d" >> $QL 2>&1
for i in $(seq 1 720); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 5; done
for i in $(seq 1 120); do curl -s --max-time 60 $U | grep -q '"status":"green"' && break; sleep 30; done
log "76: Qdrant on the standard compose (cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant"), 8 threads, cap 110g, status $(curl -s $U | grep -o '"status":"[a-z]*"' | head -1)); measuring (run_engine FAST protocol, as Ceph 1B)"
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh qdrant deep1b1b --no-load >> $QL 2>&1 \
  && log "76: qdrant deep1b1b measured" || log "76: qdrant measurement FAILED (see $QL)"
log "=== QDRANT 1B DONE ==="
