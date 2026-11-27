#!/bin/bash
# RESUME4 (2026-10-15 18:0xZ user): Milvus mmap ef curve stops after ef=256 (ef512/1024 skipped: hours per run,
# recall already saturated at ef64). Waits for ef=256 r1, stops resume3 + run_engine, records the result, wipes Milvus, runs Ceph.
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
CHAIN=935328; RE=935349
ML=$R/engines/logs/milvus_deep1b100m_mem48_mmap.log
killtree() { for c in $(ps -o pid= --ppid $1); do killtree $c; done; kill $1 2>/dev/null; }
until grep -qE '^\[.*\]   ef=256 r1:' $ML; do
  kill -0 $RE 2>/dev/null || { log "resume4: run_engine ended before ef=256 -> resume3 continues on its own"; exit 0; }
  sleep 20; done
kill $CHAIN; killtree $RE; sleep 5
echo "[$(date -u +%H:%M:%SZ)] ef curve stopped after ef=256 by user (ef512/1024 not measured under 48 GiB mmap: hours per run)" >> $ML
k=$(oomk milvus-standalone); st=$(cstate milvus-standalone); touch $D/milvus_mem48_mmap.stop
log "milvus[mmap] RESULT: loads and runs within 48 GiB (oom_kill=${k:-NA}, $st); ef curve 10..256 measured, ef512/1024 skipped by user"
mwipe; log "milvus data deleted; $(df -h / | tail -1)"
bash $D/ceph_stage.sh >> $D/ceph_stage.out 2>&1
log "=== MEM48 CHAIN DONE ==="
