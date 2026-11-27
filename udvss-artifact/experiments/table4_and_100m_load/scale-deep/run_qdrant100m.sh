#!/bin/bash
# Qdrant 100M (user 2026-10-12 "밀버스끝나면 큐드란트바로해줘").
# 1. wait for the Milvus 100M chain to end (Milvus is compose-stopped by it)
# 2. disk: delete the broken Ceph 100M cluster (user-approved 2026-10-12; results +
#    500 MB OSD log tail already archived -> kept as osd.0.log.tail500M)
# 3. Qdrant up -> run_engine qdrant deep1b100m (streamed load, 48 h index timeout) -> stop
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
until grep -q "=== MILVUS100M DONE\|FATAL milvus100m" $L; do sleep 60; done
H "docker ps --format '{{.Names}}'" | grep -q milvus && { H "cd /home/ubuntu/milvus-bench && docker compose stop" >> $L 2>&1; }
H "docker ps --format '{{.Names}}'" | grep -q milvus && { log "FATAL qdrant100m: milvus still up"; exit 1; }
log "--- qdrant100m: freeing disk (broken Ceph 100M cluster) ---"
A=$R/ceph/logs/clusters/d100m_pg16384
[ -s $A/osd.0.log.tail ] && mv $A/osd.0.log.tail $A/osd.0.log.tail500M
bash $R/ceph/scripts/teardown.sh d100m_pg16384 >> $L 2>&1 || log "teardown d100m_pg16384 failed"
FREE=$(H "df -P /home/ubuntu | awk 'NR==2{print int(\$4/1048576)}'"); log "host free ${FREE}G"
[ "${FREE:-0}" -ge 150 ] || { log "FATAL qdrant100m: only ${FREE}G free (< 150G)"; exit 1; }
log "--- qdrant100m: compose up ---"
H "cd /home/ubuntu/qdrant-bench && docker compose up -d" >> $L 2>&1
for i in $(seq 1 60); do curl -sf --max-time 5 localhost:6333/collections >/dev/null && break; sleep 10; done
curl -sf --max-time 5 localhost:6333/collections >/dev/null || { log "FATAL qdrant100m: not ready"; exit 1; }
sleep 30; log "qdrant ready"
QB_INDEX_TIMEOUT=172800 bash $R/engines/run_engine.sh qdrant deep1b100m >> $R/engines/logs/chain_qdrant.out 2>&1 \
  && log "qdrant deep1b100m done" || log "FAILED: qdrant deep1b100m (see engines/logs/qdrant_deep1b100m.log)"
H "cd /home/ubuntu/qdrant-bench && docker compose stop" >> $L 2>&1
log "qdrant stopped (collection kept)"; log "=== QDRANT100M DONE ==="
