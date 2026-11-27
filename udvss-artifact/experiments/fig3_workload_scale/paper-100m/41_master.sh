#!/bin/bash
# Strictly sequential (user, 2026-10-08: no parallel runs; latency first):
#   ws 1m/10m/100m (running) -> ws 1m redo (22_redo_1m.sh) -> delete smoke pools on 7390
#   -> sm 1m (split, merge) -> sm 100m (split, merge). Stops at the first failure.
P=/home/dev_path/experiments/paper-100m; L=$P/master.log
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
log "master(41): waiting for ws pipeline"
until grep -qE "RUN_ALL DONE|FATAL" $P/ws/run_all.log 2>/dev/null; do sleep 60; done
grep -q "FATAL" $P/ws/run_all.log && { log "FATAL: ws pipeline failed; nothing further started"; exit 1; }
log "ws done; === ws 1m redo ==="
$P/ws/scripts/22_redo_1m.sh >> $P/ws/run_all.log 2>&1 || { log "FATAL: ws 1m redo"; exit 1; }
log "ws 1m redo done; deleting smoke pools on 7390"
( source $P/sm/scripts/env.sh; ceph_ config set mon mon_allow_pool_delete true
  for p in $(ceph_ osd pool ls); do ceph_ osd pool rm $p $p --yes-i-really-really-mean-it; done
  for t in $(seq 1 360); do [ -z "$(ceph_ osd pool ls)" ] && exit 0; sleep 10; done; exit 1
) >> $L 2>&1 || { log "FATAL: smoke pool cleanup"; exit 1; }
log "=== sm 1m ==="
$P/sm/scripts/30_splitmerge.sh 1m both >> $P/sm/run_1m.log 2>&1 || { log "FATAL: sm 1m (see sm/run_1m.log)"; exit 1; }
log "sm 1m done; === sm 100m ==="
$P/sm/scripts/30_splitmerge.sh 100m both >> $P/sm/run_100m.log 2>&1 || { log "FATAL: sm 100m (see sm/run_100m.log)"; exit 1; }
log "=== MASTER DONE ==="
