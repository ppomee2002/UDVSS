#!/bin/bash
# Full-PG entry count: every one of the 1e9 rows is stored exactly once.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
set -e
L=$X/logs/count.log
log "40: full-PG count (M = $PG)"
taskset -c $CLIENT_CORES $QBENCH --operation fanout-query --pool $POOL --index $INDEX $ROUTE --pivot-probe-budget $PG $QDSET \
  --m $PG --query-limit 10 --rounds 1 --query-concurrency 1 --fanout-mode parallel \
  --query-batch-size 1 --warmup-rounds 0 > $L 2>&1 || die "40: count run failed"
F=$(grep -a "^FANOUT_QUERY" $L | tail -1)
g() { echo "$F" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }
R=$(g avg_total_matching_records_per_query); R=${R%%.*}
log "40: records=$R duplicate_entries=$(g duplicate_entries) failures=$(g failures) recall=$(g recall_at_top_k)"
[ "$R" = "$N" ] || die "40: entry count $R != $N"
[ "$(g duplicate_entries)" -le $((N / 10000)) ] || die "40: too many duplicate entries"
echo "LOADED_OK d1b" >> $L
log "=== 40 complete ==="
