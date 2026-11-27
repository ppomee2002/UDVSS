#!/bin/bash
# One full-PG scan at M=$1, concurrency 256 (recall/candidates do not depend on it).
source $HOME/hier/scripts/env.sh
M=$1; OBJS=$H/results/$POOL.objs; tag=mscan_E_fullpg_m$M; lg=$H/logs/$tag.log
bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $SQARGS $DSET \
  --m $M --query-limit 1000 --query-concurrency 256 --warmup-rounds 0 --rounds 1 \
  --full-pg-scan --object-list $OBJS > $lg 2>&1
line=$(grep "^FANOUT_QUERY" $lg | tail -1)
[ -z "$line" ] && { log "FATAL: $tag"; exit 1; }
echo "$tag m=$M args=[--full-pg-scan] $line" > $H/results/$tag.line
log "$tag $(echo "$line" | grep -oE '(recall_at_top_k|avg_total_matching_records_per_query|failures|missing_objects)=[^ ]+' | tr '\n' ' ')"
