#!/bin/bash
# chain5 (user 2026-10-10 19:00Z): after the Ceph d10m_pg4096 re-run (which stops
# its OSD), run Qdrant then Vespa on deep1b1m and deep1b10m, one engine at a time.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
WAIT=$1
log "=== chain5: waiting for the Ceph pg4096 re-run (pid $WAIT) ==="
while kill -0 $WAIT 2>/dev/null; do sleep 30; done
pgrep -x crimson-osd >/dev/null && { log "FATAL: a crimson-osd is still up; not starting engines"; exit 1; }
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
