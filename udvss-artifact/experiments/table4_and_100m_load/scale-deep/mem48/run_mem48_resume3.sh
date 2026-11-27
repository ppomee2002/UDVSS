#!/bin/bash
# RESUME3 (2026-10-15 09:45Z): Milvus default = Cannot load (Milvus refused: segment request resource failed[Memory]).
# The first mmap attempt never started (standalone recreated right after the failed load exited 1 -> not a memory result).
# Redo: clean volumes, standalone already up with mmap override (verified mmapVectorIndex=true mmapVectorField=true),
# full reload under FAST_PROTO, then Ceph stage from scratch (the default arm was aborted after 50 min).
# mem48 chain (user, 2026-10-14: run everything automatically, delete each system's data
# when moving on). Protocol:
#   Default = original 100M config + 48 GiB cap only; failure -> "Cannot load/run within 48 GiB".
#   Memory-constrained = same index + accuracy params, only official mmap/disk-backed placement,
#   used only when Default fails. Qdrant: vectors on_disk, then (last resort) hnsw on_disk.
#   Milvus: same HNSW + queryNode.mmap.vectorField/vectorIndex. Ceph: ceph_stage.sh.
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; L=$D/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
PY=/home/dev_path/corun/.venv/bin/python
cgof() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' $1").scope; }
oomk() { H "awk '\$1==\"oom_kill\"{print \$2}' $(cgof $1)/memory.events 2>/dev/null"; }
cstate() { H "docker inspect -f '{{.State.Status}} oomkilled={{.State.OOMKilled}} exit={{.State.ExitCode}}' $1"; }
sampler() { rm -f $D/$2.stop; nohup setsid bash $D/sample_cg.sh $1 $D/$2.tsv $D/$2.stop >/dev/null 2>&1 < /dev/null & }
QC="cd /home/ubuntu/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.mem48.yml"
MB="cd /home/ubuntu/milvus-bench && docker compose -f docker-compose.yml -f docker-compose.mem48.yml"
MM="$MB -f docker-compose.mem48mmap.yml"
qwipe() { H "cd /home/ubuntu/qdrant-bench && docker compose down >/dev/null 2>&1; docker run --rm --user 0 --entrypoint rm -v /home/ubuntu/qdrant-bench/volumes:/v qdrant/qdrant:latest -rf /v/qdrant; mkdir -p /home/ubuntu/qdrant-bench/volumes/qdrant"; }
# ---------------- Milvus ----------------
mwipe() { H "cd /home/ubuntu/milvus-bench && docker compose down >/dev/null 2>&1; docker run --rm --user 0 --entrypoint rm -v /home/ubuntu/milvus-bench/volumes:/v milvusdb/milvus:v2.6.23 -rf /v/etcd /v/minio /v/milvus; mkdir -p /home/ubuntu/milvus-bench/volumes/{etcd,minio,milvus}"; }
mhealthy() { for i in $(seq 120); do H "curl -sf localhost:9091/healthz" >/dev/null && return 0; sleep 10; done; return 1; }
# run_engine with a guard: if the index is built but load_collection has not returned in 90 min, kill it
mrun() {  # <tag> [--no-load]
  local tag=$1; shift; local lg=$R/engines/logs/milvus_deep1b100m_$tag.log
  RUN_TAG=$tag bash $R/engines/run_engine.sh milvus deep1b100m "$@" >> $D/milvus_$tag.out 2>&1 & local p=$!
  local tidx=""
  while kill -0 $p 2>/dev/null; do
    if [ -z "$tidx" ] && grep -q "index build" $lg 2>/dev/null; then tidx=$(date +%s); fi
    if [ -n "$tidx" ] && ! grep -qE "^  load [0-9.]+s" $lg && [ $(( $(date +%s) - tidx )) -gt 5400 ]; then
      log "milvus[$tag]: load_collection not finished 90 min after index build -> killing"; kill $p; pkill -f "load_dataset.py --dataset deep1b100m"; wait $p; return 9; fi
    sleep 30
  done
  wait $p
}
log "milvus[mmap] REDO: clean volumes, standalone up with vectorField+vectorIndex mmap (verified in querynode log), full reload, FAST_PROTO"
H "$MM up -d" >> $L 2>&1; mhealthy || log "WARN milvus not healthy"
log "milvus[mmap] up, memory.max=$(H "cat $(cgof milvus-standalone)/memory.max"), env: $(H "docker exec milvus-standalone env | grep QUERYNODE_MMAP | tr '\n' ' '")"
sampler milvus-standalone milvus_mem48_mmap
mrun mem48_mmap; rc=$?
k=$(oomk milvus-standalone); st=$(cstate milvus-standalone); touch $D/milvus_mem48_mmap.stop
if [ $rc = 0 ] && [ "${k:-0}" = 0 ] && echo "$st" | grep -q "^running"; then log "milvus[mmap] RESULT: runs within 48 GiB ($st)"
else log "milvus[mmap] RESULT: Cannot load/run within 48 GiB (rc=$rc, oom_kill=${k:-NA}, $st); last: $(grep -iE 'error|exception|FATAL|insufficient' $R/engines/logs/milvus_deep1b100m_mem48_mmap.log | tail -2 | cut -c1-300)"
  H "docker logs --tail 300 milvus-standalone 2>&1 | grep -iE 'memory|oom|insufficient|load|panic' | tail -40" > $D/milvus_mmap.container.log 2>&1; fi
mwipe; log "milvus data deleted"
bash $D/ceph_stage.sh >> $D/ceph_stage.out 2>&1
log "=== MEM48 CHAIN DONE ==="
