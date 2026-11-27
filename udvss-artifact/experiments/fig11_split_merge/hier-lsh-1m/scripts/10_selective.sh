#!/bin/bash
# selective_tradeoff re-run: never-stop staged dispatch, exact tree (budget 64,
# patience 4), 10k queries. Schedule extended to 512: hierarchical PGs are
# ~3x smaller, so the same candidate budget needs ~3-4x the PGs.
source $HOME/hier/scripts/env.sh
QL=${1:-10000}
SCHED=8,16,24,32,48,64,96,128,192,256,384,512
log "selective ql=$QL schedule=$SCHED"
bench --operation fanout-query --pool $POOL --index $INDEX \
  $IDXARGS $SQARGS $DSET --tree-search-budget 64 --tree-no-improve-patience 4 \
  --m 512 --query-limit $QL --query-concurrency 64 --warmup-rounds 0 \
  --selective --selective-schedule $SCHED --selective-stop never \
  --selective-csv $H/results/hier_selective_q${QL}.csv \
  > $H/logs/10_selective_q${QL}.log 2>&1
rc=$?
tr ' ' '\n' < $H/logs/10_selective_q${QL}.log \
  | grep -E '^(completed|failures|missing_objects|seconds|recall_at_top_k|avg_total_matching_records_per_query)=' | tr '\n' ' '
log "selective done rc=$rc"
