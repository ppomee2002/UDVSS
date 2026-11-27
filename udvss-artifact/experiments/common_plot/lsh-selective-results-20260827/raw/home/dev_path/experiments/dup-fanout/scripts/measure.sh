#!/bin/bash
# $1 = duplicate level (1|2|3). Restarts the OSD (equal cache state for
# every level), warms, then runs the fixed M ladder. Query conditions are
# IDENTICAL across duplicate levels for a given arm -- same top-k, same
# query count, same (M, concurrency, io_depth) grid, same tree budgets.
source /home/dev_path/experiments/dup-fanout/scripts/env.sh
set -u
D=$1
QA=$(qargs $D)
R=$EXP/results/${MODE}_d${D}.csv

stop_osd; start_osd || exit 1

log "warm-up (discarded)"
cd $BUILD
$BIN/ceph-vector-bench-native --operation fanout-query --pool $POOL \
  --index $INDEX $IDXBASE --d $D $QA $DSET $QEXTRA --m 32 \
  --query-limit $QLIMIT --query-concurrency 4 --warmup-rounds 0 \
  --min-seconds 60 > $EXP/logs/warm_${MODE}_d${D}.log 2>&1 \
  || { log "FATAL: warm-up failed"; tail -5 $EXP/logs/warm_${MODE}_d${D}.log; exit 1; }
log "warm-up $(grep -o 'completed=[0-9]*' $EXP/logs/warm_${MODE}_d${D}.log | head -1)"

echo "mode,duplicate,m,concurrency,io_depth,arm,completed,failures,recall_at_top_k,candidates_per_query,logical_qps,latency_us_avg,latency_us_p50,latency_us_p99" > $R
run() {  # m c iod arm
  local m=$1 c=$2 iod=$3 arm=$4
  cd $BUILD
  out=$($BIN/ceph-vector-bench-native --operation fanout-query --pool $POOL \
    --index $INDEX $IDXBASE --d $D $QA $DSET $QEXTRA --m $m \
    --query-limit $QLIMIT --query-concurrency $c --warmup-rounds 0 \
    --rounds 1 --io-depth $iod 2>&1)
  echo "$out" > $EXP/logs/q_${MODE}_d${D}_m${m}_${arm}.log
  f() { echo "$out" | tr ' ' '\n' | grep -m1 "^$1=" | cut -d= -f2; }
  echo "$MODE,$D,$m,$c,$iod,$arm,$(f completed),$(f failures),$(f recall_at_top_k),$(f avg_total_matching_records_per_query),$(f logical_qps),$(f logical_latency_us_avg),$(f logical_latency_us_p50),$(f logical_latency_us_p99)" | tee -a $R
}
for spec in $ARMS; do
  IFS=: read -r m c iod <<< "$spec"
  run "$m" 1 0 ref_c1
  [ "$c:$iod" = "1:0" ] || run "$m" "$c" "$iod" best
done
log "=== duplicate=$D measurement complete -> $R ==="
