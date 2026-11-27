#!/bin/bash
# Per-query latency of the Fig 7 operating points (M=128, B=5, patience 4) on the fig7-knee-94
# index, original Fig 7 bundle. query-concurrency 1 (pure per-query latency), 1000 queries,
# 3 reps with rotated order, after one discarded warm-up pass. Waits for the prune sweep.
source /home/dev_path/experiments/prune-strength-94/scripts/lib.sh   # osd_restart, ceph_, bench
export L=/home/dev_path/experiments/latency-94
export RAW=$L/results/raw.txt CMDLOG=$L/logs/commands.log
export RUNARGS="--query-concurrency 1 --warmup-rounds 0 --rounds 1"
while ps -eo args | grep -q "[2]0_phaseB.sh"; do sleep 20; done
osd_restart $V/bundle "" "" || exit 1
run() {  # TAG O
  local tag=$1 o=$2 lg=$L/logs/$1.log line
  bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $SQARGS $DSET --m $M --query-limit 1000 \
    $RUNARGS --tree-search-budget 5 --tree-no-improve-patience 4 --tree-onode-budget $o > $lg 2>&1
  line=$(grep "^FANOUT_QUERY" $lg | tail -1)
  [[ "$line" == *"completed=1000 failures=0 first_error=0 missing_objects=0"* ]] || { log "REJECTED $tag"; return 1; }
  echo "$tag m=$M args=[conc1 B5 O$o] $line" >> $RAW
  log "$tag $(echo "$line" | grep -oE '(logical_latency_us_avg|logical_latency_us_p50|recall_at_top_k|avg_total_matching_records_per_query)=[^ ]+' | tr '\n' ' ')"
}
OS="0 100 96 92 88 84 80 76"
run warm_o0 0 || exit 1
for r in 1 2 3; do
  case $r in 2) ord=$(echo $OS | tr ' ' '\n' | tac | tr '\n' ' ');; 3) ord="88 0 76 96 80 100 84 92";; *) ord=$OS;; esac
  for o in $ord; do run b5_o${o}_r$r $o || exit 1; done
done
log "=== latency done ==="
