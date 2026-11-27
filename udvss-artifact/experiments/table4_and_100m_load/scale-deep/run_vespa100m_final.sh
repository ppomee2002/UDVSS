#!/bin/bash
# Vespa 100M, take 4: the resume pass was feed-blocked again 21:31Z-03:10Z (Vespa disk limit
# incl. transient estimate); its 2 h 507-retry ran out for the in-flight docs (~94.0-94.2M).
# Qdrant collection deleted (user-approved) -> block lifted. Let that pass finish, then re-feed
# ids >= 93M once more (idempotent), require totalCount == 100M (wait_indexed), then measure.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log; LOG=$R/engines/logs/vespa_deep1b100m.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
log "vespa100m-final started (waiting for resume feeder 2861614)"
while kill -0 2861614 2>/dev/null; do sleep 30; done
cd /home/dev_path/corun
echo "[$(date -u +%H:%M:%SZ)] final pass: re-feed ids 93000000.. (507 retried)" >> $LOG
VB_INDEX_TIMEOUT=172800 taskset -c 9-11,20-23,44-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 8 16 93000000 >> $LOG 2>&1 \
  || { log "FAILED: vespa deep1b100m final re-feed (see log)"; log "=== VESPA100M FINAL DONE ==="; exit 1; }
echo "[$(date -u +%H:%M:%SZ)] load done (parallel feed + 2 re-feed passes; totalCount settled at 100M)" >> $LOG
log "vespa100m feed+settle done (100M); measuring"
bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $R/engines/logs/chain_vespa.out 2>&1 \
  && log "vespa deep1b100m done" || log "FAILED: vespa deep1b100m (see engines/logs/vespa_deep1b100m.log)"
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped (index kept)"; log "=== VESPA100M FINAL DONE ==="
