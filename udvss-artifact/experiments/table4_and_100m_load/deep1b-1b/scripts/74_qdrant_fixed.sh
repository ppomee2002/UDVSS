#!/bin/bash
# 74 = 73 with a FIXED job count (PATCH cancels in-flight optimizations; 10 parallel jobs
# thrashed the page cache). Container from 73 kept as is.
# Qdrant 1B index, faster (user 2026-10-28: more threads but keep a memory cap).
# - container: cpuset 0-39, mem_limit 115g (no swap), global optimizer threads 40,
#   restart: on-failure (a cap OOM kills only Qdrant; it restarts and resumes - finished
#   segments persist, only in-flight optimizations are lost)
# - per-collection optimizers_config.max_optimization_threads (live PATCH, also re-triggers
#   the optimizer, which stays "grey" after a restart) is steered by cgroup anon memory
#   every 60 s: anon < 80G and >= 20 min since the last change -> +8 (max 40);
#   anon > 100G -> -8 (min 8). Start $TH0 (default 16).
# - progress from GET /collections (no exact count while indexing: a 1B exact count
#   times out and loads the server); done = green + optimizer ok + indexed == points for
#   4 polls, then one exact count. Then measurement as 72 (standard compose, cpuset 0-7,
#   8 threads, cap 110g, per-collection threads reset to default). Marker === QDRANT 1B DONE ===.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
QL=$X/logs/qdrant_1b.log; T=/tmp/udvss-job; IT=$X/results/qdrant_1b_index.tsv
U=http://127.0.0.1:6333/collections/wg_deep1b1b
CG() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' qb-qdrant").scope; }
log "74: fixed max_optimization_threads=${JOBS:-4} (concurrent segment jobs; 10 jobs thrashed the page cache: 1.8 GB/s 4K reads, CPU 7.6 cores)"
patch() { curl -s --max-time 120 -X PATCH $U -H 'Content-Type: application/json' -d "{\"optimizers_config\":{\"max_optimization_threads\":$1}}"; }
[ -s $IT ] || echo -e "utc\tthreads\tanon_g\tfile_g\toom_kill\trestarts\tstatus\tpoints\tindexed\tsegments" > $IT
TH=${JOBS:-4}; ok=0; lastrc=-1; tlast=$(date +%s)
until curl -sf --max-time 10 http://127.0.0.1:6333/healthz >/dev/null; do sleep 10; done
while :; do
  info=$(curl -s --max-time 120 $U | python3 -c "import json,sys;r=json.load(sys.stdin)['result'];print(r['status'],r['optimizer_status'] if isinstance(r['optimizer_status'],str) else 'err',r.get('points_count'),r.get('indexed_vectors_count'),r.get('segments_count'))" 2>/dev/null)
  c=$(CG); m=$(H "awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{printf \"%.1f %.1f\", a/2^30, f/2^30}' $c/memory.stat; echo -n ' '; awk '\$1==\"oom_kill\"{print \$2}' $c/memory.events; docker inspect -f '{{.RestartCount}}' qb-qdrant")
  read an fi ok_k rc <<< "$m"; read st os pts idx seg <<< "$info"
  if [ "$rc" != "$lastrc" ]; then   # first pass or container restarted (OOM): re-arm the optimizer
    [ "$lastrc" != -1 ] && { log "74: Qdrant restarted (RestartCount=$rc, cap OOM)"; }
    until curl -sf --max-time 10 http://127.0.0.1:6333/healthz >/dev/null; do sleep 10; done
    log "74: PATCH max_optimization_threads=$TH: $(patch $TH)"; lastrc=$rc
  fi
  echo -e "$(date -u +%FT%TZ)\t$TH\t$an\t$fi\t$ok_k\t$rc\t$st/$os\t$pts\t$idx\t$seg" >> $IT
  if [ "$st" = green ] && [ "$os" = ok ] && [ -n "$pts" ] && [ "$idx" = "$pts" ]; then ok=$((ok+1)); else ok=0; fi
  [ $ok -ge 4 ] && break
  sleep 60
done
n=$(curl -s --max-time 1800 -X POST $U/points/count -H 'Content-Type: application/json' -d '{"exact":true}')
log "74: Qdrant 1B indexed (green, indexed=$idx, segments=$seg); exact count: $n"
patch null >/dev/null
H "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml stop -t 600 && docker compose -f docker-compose.yml -f docker-compose.cap110.yml up -d" >> $QL 2>&1
for i in $(seq 1 720); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 5; done
log "74: Qdrant on the standard compose (cpuset $(H "docker inspect -f '{{.HostConfig.CpusetCpus}}' qb-qdrant"), 8 threads, cap 110g); measuring"
QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh qdrant deep1b1b --no-load >> $QL 2>&1 \
  && log "74: qdrant deep1b1b measured" || log "74: qdrant measurement FAILED (see $QL)"
log "=== QDRANT 1B DONE ==="
