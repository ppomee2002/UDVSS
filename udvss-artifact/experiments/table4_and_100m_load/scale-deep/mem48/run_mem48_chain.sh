#!/bin/bash
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

log "=== mem48 chain started; waiting for Vespa"
until grep -q "=== VESPA MEM48 DONE" $D/driver.log; do sleep 60; done
log "Vespa done -> deleting Vespa index ($(H 'du -sh /home/ubuntu/vespa-bench/volumes/vespa | cut -f1'))"
H "docker rm -f vb-vespa >/dev/null; docker run --rm --user 0 --entrypoint rm -v /home/ubuntu/vespa-bench/volumes:/v vespaengine/vespa:latest -rf /v/vespa; mkdir -p /home/ubuntu/vespa-bench/volumes/vespa; df -h / | tail -1" >> $L 2>&1

# ---------------- Qdrant ----------------
qwipe() { H "cd /home/ubuntu/qdrant-bench && docker compose down >/dev/null 2>&1; docker run --rm --user 0 --entrypoint rm -v /home/ubuntu/qdrant-bench/volumes:/v qdrant/qdrant:latest -rf /v/qdrant; mkdir -p /home/ubuntu/qdrant-bench/volumes/qdrant"; }
QOK=0
for cfg in default vecdisk vechnswdisk; do
  qwipe; H "$QC up -d" >> $L 2>&1
  for i in $(seq 60); do curl -sf http://127.0.0.1:6333/ >/dev/null && break; sleep 5; done
  log "qdrant[$cfg] up, memory.max=$(H "cat $(cgof qb-qdrant)/memory.max")"; sampler qb-qdrant qdrant_mem48_$cfg
  case $cfg in default) EV="";; vecdisk) EV="QB_VEC_ON_DISK=1";; vechnswdisk) EV="QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1";; esac
  env $EV QB_INDEX_TIMEOUT=43200 RUN_TAG=mem48_$cfg bash $R/engines/run_engine.sh qdrant deep1b100m >> $D/qdrant_$cfg.out 2>&1; rc=$?
  k=$(oomk qb-qdrant); st=$(cstate qb-qdrant); touch $D/qdrant_mem48_$cfg.stop
  if [ $rc = 0 ] && [ "${k:-0}" = 0 ] && echo "$st" | grep -q "^running"; then
    log "qdrant[$cfg] RESULT: runs within 48 GiB (rc=0, oom_kill=0, $st)"; QOK=1; break
  fi
  log "qdrant[$cfg] RESULT: Cannot load/run within 48 GiB (rc=$rc, oom_kill=${k:-NA}, $st); last log: $(grep -E 'FATAL|Error|error' $R/engines/logs/qdrant_deep1b100m_mem48_$cfg.log | tail -2 | cut -c1-200)"
  H "docker logs --tail 30 qb-qdrant" > $D/qdrant_$cfg.container.log 2>&1
done
[ $QOK = 1 ] || log "qdrant: no configuration ran within 48 GiB"
qwipe; log "qdrant data deleted"

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
mwipe; H "$MB up -d" >> $L 2>&1; mhealthy || log "WARN milvus not healthy"
log "milvus[default] up, memory.max=$(H "cat $(cgof milvus-standalone)/memory.max")"; sampler milvus-standalone milvus_mem48_default
mrun mem48_default; rc=$?
k=$(oomk milvus-standalone); st=$(cstate milvus-standalone); touch $D/milvus_mem48_default.stop
if [ $rc = 0 ] && [ "${k:-0}" = 0 ] && echo "$st" | grep -q "^running"; then
  log "milvus[default] RESULT: runs within 48 GiB (rc=0, oom_kill=0, $st)"
else
  log "milvus[default] RESULT: Cannot load/run within 48 GiB (rc=$rc, oom_kill=${k:-NA}, $st); last: $(grep -iE 'error|exception|FATAL|insufficient|memory' $R/engines/logs/milvus_deep1b100m_mem48_default.log | tail -3 | cut -c1-300)"
  H "docker logs --tail 200 milvus-standalone 2>&1 | grep -iE 'memory|oom|insufficient|load' | tail -30" > $D/milvus_default.container.log 2>&1
  # reuse the inserted+indexed data if the default run got that far, else full reload
  built=$(grep -cE "^  index build" $R/engines/logs/milvus_deep1b100m_mem48_default.log)
  if [ "$built" -ge 1 ] && [ "$st" != "" ] && echo "$st" | grep -qE "^(running|exited)"; then
    H "$MM up -d" >> $L 2>&1; mhealthy || log "WARN milvus not healthy"
    log "milvus[mmap] standalone recreated with vectorField+vectorIndex mmap (data kept); env: $(H "docker exec milvus-standalone env | grep QUERYNODE_MMAP | tr '\n' ' '")"
    sampler milvus-standalone milvus_mem48_mmap
    ( cd /home/dev_path/milvus-bench && timeout 7200 .venv/bin/python - >> $D/milvus_mmap_load.out 2>&1 <<'PY'
import sys, time; sys.path.insert(0, 'scripts'); import bench_env as E
from pymilvus import MilvusClient
c = MilvusClient(uri=E.URI); n = E.collection_name('deep1b100m')
print('index:', c.describe_index(n, 'vector')); c.release_collection(n); t = time.time()
c.load_collection(n, replica_number=E.REPLICA_NUMBER)
print(f'load {time.time()-t:.1f}s rows={c.get_collection_stats(n)["row_count"]} state={c.get_load_state(n)}')
PY
    ); lrc=$?
    log "milvus[mmap] load rc=$lrc: $(tail -1 $D/milvus_mmap_load.out)"
    [ $lrc = 0 ] && mrun mem48_mmap --no-load; rc=$?
  else
    mwipe; H "$MM up -d" >> $L 2>&1; mhealthy; sampler milvus-standalone milvus_mem48_mmap
    log "milvus[mmap] full reload with vectorField+vectorIndex mmap"; mrun mem48_mmap; rc=$?
  fi
  k=$(oomk milvus-standalone); st=$(cstate milvus-standalone); touch $D/milvus_mem48_mmap.stop
  [ $rc = 0 ] && [ "${k:-0}" = 0 ] && log "milvus[mmap] RESULT: runs within 48 GiB ($st)" \
    || log "milvus[mmap] RESULT: Cannot load/run within 48 GiB (rc=$rc, oom_kill=${k:-NA}, $st)"
fi
mwipe; log "milvus data deleted"

# ---------------- Ceph ----------------
log "waiting for mem48/ceph_stage.sh"
until [ -x $D/ceph_stage.sh ]; do sleep 300; done
bash $D/ceph_stage.sh >> $D/ceph_stage.out 2>&1
log "=== MEM48 CHAIN DONE ==="
