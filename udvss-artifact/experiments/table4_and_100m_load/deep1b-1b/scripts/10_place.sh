#!/bin/bash
# Pivot model + capped placement of all 1e9 rows, no cluster (nothing else runs:
# all 48 cores). Writes $MODEL and $PLACEMENT; skips if both exist.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
set -e
[ -s $MODEL ] && [ -s $PLACEMENT ] && { log "10: model + placement exist, skip"; exit 0; }
[ -n "$(osd_pid)" ] && die "10: OSD running; placement must run alone"
log "10: place (sample 4194304, PG $PG, topn 16)"
L=$X/logs/place.log
taskset -c 0-47 $BENCH --operation place $ROUTE --pivot-probe-budget $PROBE_BUDGET --base $BASE \
  --placement-file $PLACEMENT --place-topn 16 --place-chunk $CHUNK > $L 2>&1 || die "10: place failed ($(tail -1 $L))"
grep -aE "^PIVOT_MODEL |^PIVOT_PLACEMENT|^PLACE_RANK_HIST|^PLACEMENT_FILE" $L | cut -c1-400 | tee -a $LOG
log "=== 10 complete ==="
