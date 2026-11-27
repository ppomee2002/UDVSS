#!/bin/bash
# Parallel remainder of 20_pareto.sh (same run_arm): $1 = mscan | pareto M
source $HOME/hier/scripts/env.sh
QL=1000
OBJS=$H/results/$POOL.objs
RUNARGS="--query-concurrency 64 --warmup-rounds 0 --rounds 1"
run_arm() {  # tag M args...
  local tag=$1 M=$2; shift 2
  local lg=$H/logs/$tag.log
  bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $SQARGS $DSET \
    --m $M --query-limit $QL $RUNARGS "$@" > $lg 2>&1
  local line; line=$(grep "^FANOUT_QUERY" $lg | tail -1)
  [ -z "$line" ] && { log "FATAL: $tag no result: $(grep -iE 'error|fail' $lg | head -2)"; return 1; }
  echo "$tag m=$M args=[$*] $line" >> $OUT
  log "$tag $(echo "$line" | grep -oE '(recall_at_top_k|avg_total_matching_records_per_query|failures|missing_objects)=[^ ]+' | tr '\n' ' ')"
}
if [ "$1" = mscan ]; then
  OUT=$H/results/m_scan_raw.txt
  for M in 64 96 128 192 256; do run_arm mscan_E_fullpg_m$M $M --full-pg-scan --object-list $OBJS || exit 1; done
else
  M=$2; OUT=$H/results/pareto_raw_m$M.txt; : > $OUT
  for b in 4 8; do for o in 64 72 80 88 96 128 160 192 256 384 0; do
    run_arm pareto_b${b}_o${o}_m$M $M --tree-search-budget $b --tree-no-improve-patience 4 --tree-onode-budget $o || exit 1
  done; done
fi
log "=== $* done ==="
