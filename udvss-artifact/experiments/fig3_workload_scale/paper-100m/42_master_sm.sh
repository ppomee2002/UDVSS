#!/bin/bash
# Restart of the sm part of 41_master.sh (ws and the 1m redo are done; the smoke pools
# were deleted): sm 1m (split, merge) -> sm 100m (split, merge). Stops at the first failure.
P=/home/dev_path/experiments/paper-100m; L=$P/master.log
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
log "=== sm 1m (42) ==="
$P/sm/scripts/30_splitmerge.sh 1m both >> $P/sm/run_1m.log 2>&1 || { log "FATAL: sm 1m (see sm/run_1m.log)"; exit 1; }
log "sm 1m done; === sm 100m ==="
$P/sm/scripts/30_splitmerge.sh 100m both >> $P/sm/run_100m.log 2>&1 || { log "FATAL: sm 100m (see sm/run_100m.log)"; exit 1; }
log "=== MASTER DONE ==="
