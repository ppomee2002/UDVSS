#!/bin/bash
# Selective trade-off re-measure. Commands and arguments are those of
# selective-tree-exact-sift1m/scripts/01_load.sh + 03_selective.sh
# (IDXARGS, QARGS, TREE, DSET, schedule, 10k queries, concurrency 32);
# only the placement differs between the two arms, on the same cluster
# (lsh-aligned-1m: pg-split crimson-osd + lsh-aligned bench):
#   old : canonical pg-lsh-v0 placement, as in the original figure
#   new : + --lsh-aligned-locators
# Usage: 20_selective.sh old|new [query_limit]
source /home/dev_path/experiments/lsh-aligned-1m/scripts/env.sh
ARM=$1 QL=${2:-10000}
S=$EXP/selective
PGNUM=1024
POOL=sel_${ARM}_v1024 INDEX=sel_${ARM}_v1024_index
IDXARGS="--routing-mode pg-suboid --raw-euclidean-geometry \
--pg-num $PGNUM --k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4"
case $ARM in
  old) ;;
  new) IDXARGS="$IDXARGS --lsh-aligned-locators" ;;
  *) echo "usage: $0 old|new"; exit 2 ;;
esac
SQARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes"
STREE="--tree-search-budget 64 --tree-no-improve-patience 4"
SCHED=8,16,24,32,48,64,96,128

ceph_ osd pool create $POOL $PGNUM $PGNUM >/dev/null
ceph_ osd pool set $POOL pg_autoscale_mode off >/dev/null
ceph_ osd pool application enable $POOL rados >/dev/null
wait_pool $POOL $PGNUM 1800 || { log "FATAL: pool $POOL not clean"; exit 1; }

log "=== $ARM: load SIFT1M into $POOL ==="
bench --operation load --pool $POOL --index $INDEX \
  $IDXARGS $DSET --query-limit 1000 --load-concurrency 32 --top-k 10 \
  > $S/logs/01_load_$ARM.log 2>&1 || { log "FATAL: load $ARM"; tail -3 $S/logs/01_load_$ARM.log; exit 1; }
grep -E "^LOAD" $S/logs/01_load_$ARM.log | cut -c1-200

log "=== $ARM: selective run ql=$QL schedule=$SCHED tree=[$STREE] ==="
bench --operation fanout-query --pool $POOL --index $INDEX \
  $IDXARGS $SQARGS $DSET $STREE --m 128 --query-limit $QL \
  --query-concurrency 32 --warmup-rounds 0 \
  --selective --selective-schedule $SCHED --selective-stop never \
  --selective-csv $S/results/${ARM}_selective_q${QL}.csv \
  > $S/logs/03_selective_${ARM}_q${QL}.log 2>&1 || { log "FATAL: query $ARM"; tail -3 $S/logs/03_selective_${ARM}_q${QL}.log; exit 1; }
tr ' ' '\n' < $S/logs/03_selective_${ARM}_q${QL}.log \
  | grep -E '^(completed|failures|missing_objects|seconds|recall_at_top_k|avg_total_matching_records_per_query)='
log "=== $ARM done ==="
