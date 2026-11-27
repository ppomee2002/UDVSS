#!/bin/bash
# Vespa 100M, take 3: the parallel feed hit Vespa's disk feed-block (host disk 90.1% at 20:29Z,
# during a flush spike) -> 11.8M PUTs got 507 from ~88M on. Docs persisted; re-feed ids >= 87M
# (idempotent), now retrying 507 for up to 2 h, then settle -> run_engine --no-load.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log; LOG=$R/engines/logs/vespa_deep1b100m.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
ready() { curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' &&
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 localhost:8080/ApplicationStatus)" = 200 ]; }
log "--- vespa100m resume: compose up ---"; log "vespa100m-resume started"
H "cd /home/ubuntu/vespa-bench && docker compose up -d" >> $L 2>&1
for i in $(seq 1 120); do ready && break; sleep 10; done
ready || { log "FATAL vespa100m-resume: not ready"; exit 1; }
sleep 60
cd /home/dev_path/corun
N0=$(.venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import engine_vespa as E; print(E._total_count())")
log "vespa100m resume: totalCount before re-feed = $N0"
echo "[$(date -u +%H:%M:%SZ)] resume: totalCount=$N0; re-feed ids 87000000.. (507 retried)" >> $LOG
VB_INDEX_TIMEOUT=172800 taskset -c 9-11,20-23,44-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 8 16 87000000 >> $LOG 2>&1 \
  || { log "FAILED: vespa deep1b100m re-feed (see engines/logs/vespa_deep1b100m.log)"; H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1; log "=== VESPA100M RESUME DONE ==="; exit 1; }
echo "[$(date -u +%H:%M:%SZ)] load done (parallel feed + resume)" >> $LOG
log "vespa100m feed+settle done; measuring"
bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $R/engines/logs/chain_vespa.out 2>&1 \
  && log "vespa deep1b100m done" || log "FAILED: vespa deep1b100m (see engines/logs/vespa_deep1b100m.log)"
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped (index kept)"; log "=== VESPA100M RESUME DONE ==="
