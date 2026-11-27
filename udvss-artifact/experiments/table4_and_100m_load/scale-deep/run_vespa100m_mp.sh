#!/bin/bash
# Vespa 100M, take 2: run_vespa100m.sh's single-process feed was client-bound (1.3k/s, ~21 h).
# Schema already deployed by it (same params). Parallel feed -> settle -> run_engine --no-load.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log; LOG=$R/engines/logs/vespa_deep1b100m.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
cd /home/dev_path/corun
echo "[$(date -u +%H:%M:%SZ)] parallel feed (8 procs x 16 threads, client on cores 9-11,20-23,44-47)" >> $LOG
VB_INDEX_TIMEOUT=172800 taskset -c 9-11,20-23,44-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 8 16 >> $LOG 2>&1 \
  || { log "FAILED: vespa deep1b100m parallel feed (see engines/logs/vespa_deep1b100m.log)"; H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1; log "=== VESPA100M DONE ==="; exit 1; }
echo "[$(date -u +%H:%M:%SZ)] load done (parallel feed)" >> $LOG
log "vespa100m feed+settle done; measuring"
bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $R/engines/logs/chain_vespa.out 2>&1 \
  && log "vespa deep1b100m done" || log "FAILED: vespa deep1b100m (see engines/logs/vespa_deep1b100m.log)"
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped (index kept)"; log "=== VESPA100M DONE ==="
