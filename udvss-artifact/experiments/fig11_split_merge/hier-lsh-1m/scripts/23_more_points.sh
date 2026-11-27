#!/bin/bash
# Extra pareto points at M=$1 (lower ONode budgets), appended to pareto_raw_m$1.txt.
source $HOME/hier/scripts/env.sh
M=$1; OUT=$H/results/pareto_raw_m$M.txt
for b in 4 8; do for o in 16 24 32 40 48 56; do
  tag=pareto_b${b}_o${o}_m$M; lg=$H/logs/$tag.log
  bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $SQARGS $DSET \
    --m $M --query-limit 1000 --query-concurrency 128 --warmup-rounds 0 --rounds 1 \
    --tree-search-budget $b --tree-no-improve-patience 4 --tree-onode-budget $o > $lg 2>&1
  line=$(grep "^FANOUT_QUERY" $lg | tail -1)
  [ -z "$line" ] && { log "FATAL: $tag"; exit 1; }
  echo "$tag m=$M args=[--tree-search-budget $b --tree-no-improve-patience 4 --tree-onode-budget $o] $line" >> $OUT
  log "$tag $(echo "$line" | grep -oE '(recall_at_top_k|avg_total_matching_records_per_query|failures|missing_objects)=[^ ]+' | tr '\n' ' ')"
done; done
log "=== more points M=$M done ==="
