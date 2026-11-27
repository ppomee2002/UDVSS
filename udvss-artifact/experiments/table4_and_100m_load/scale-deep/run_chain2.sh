#!/bin/bash
# Re-ordered tail of run_chain.sh (user 2026-10-10 12:05Z: Milvus and Ceph first).
# Waits for the orphaned Milvus 10M run_engine.sh, stops Milvus, runs Ceph d1m,
# then Qdrant and Vespa as before.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
MPID=$1
log "=== chain2 (reordered): waiting for milvus deep1b10m (pid $MPID) ==="
while kill -0 $MPID 2>/dev/null; do sleep 60; done
grep -q "DONE milvus deep1b10m" $R/engines/logs/milvus_deep1b10m.log && log "milvus deep1b10m done" || log "FAILED: milvus deep1b10m"
H "cd /home/ubuntu/milvus-bench && docker compose stop" >> $L 2>&1; log "milvus stopped"
log "--- ceph d1m ---"
KEEP=1 bash $R/ceph/scripts/run_arm.sh d1m "1 2 4 8 16 32 64 96" 16 >> $R/ceph/logs/run_all.log 2>&1 \
  && log "ceph d1m done" || log "FAILED: ceph d1m"
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; pgrep -x crimson-osd >/dev/null && kill -9 $(pgrep -x crimson-osd)
log "ceph osds stopped"
ready() { case $1 in
    qdrant) curl -sf --max-time 5 localhost:6333/collections >/dev/null ;;
    vespa)  curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' ;;
  esac; }
for ENG in qdrant vespa; do
  log "--- $ENG: compose up ---"
  H "cd /home/ubuntu/$ENG-bench && docker compose up -d" >> $L 2>&1
  for i in $(seq 1 60); do ready $ENG && break; sleep 10; done
  ready $ENG || { log "FATAL: $ENG not ready after 10 min"; H "cd /home/ubuntu/$ENG-bench && docker compose stop" >> $L 2>&1; continue; }
  log "$ENG ready"
  for DS in deep1b1m deep1b10m; do
    bash $R/engines/run_engine.sh $ENG $DS --drop >> $R/engines/logs/chain_${ENG}.out 2>&1 \
      && log "$ENG $DS done" || log "FAILED: $ENG $DS (see engines/logs/${ENG}_${DS}.log)"
  done
  H "cd /home/ubuntu/$ENG-bench && docker compose stop" >> $L 2>&1
  log "$ENG stopped"
done
log "=== CHAIN DONE ==="
