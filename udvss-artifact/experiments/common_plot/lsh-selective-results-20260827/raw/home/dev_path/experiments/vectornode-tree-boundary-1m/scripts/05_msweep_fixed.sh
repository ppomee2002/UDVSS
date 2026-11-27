#!/bin/bash
source /home/dev_path/experiments/vectornode-tree-boundary-1m/scripts/env.sh
set -u
assert_tmp_x_untouched || exit 1
OUT=$EXP/results/05_msweep
mkdir -p $OUT
R=$EXP/results/05_msweep.txt
: > $R
log "=== phase 05: m-sweep with the leaf-crossing fix, commit $(cd $SRC && git rev-parse --short HEAD) ===" | tee -a $R

# Mirrors /tmp/x/run_raw_v1024_sweep.sh exactly -- same routing (NO
# --query-table-count / --margin-ordered-probes), budget 4, concurrency 8 --
# so the ON/PRUNING columns drop straight into that table. Only the pool and
# the binary differ. /tmp/x itself is never contacted.
COMMON=(--conf $CEPH_CONF
  --base $DATA/sift_base.fvecs
  --query $DATA/sift_query.fvecs
  --groundtruth $DATA/sift_groundtruth.ivecs
  --pool $POOL
  --index $INDEX
  --routing-mode pg-suboid
  --raw-euclidean-geometry
  --residual-bits 4
  --probe-limit-per-pg 1
  --pg-num 1024
  --k 10 --l 4 --seed 1315423911 --d 1 --hamming-radius 10
  --distance-bucket-bits 4
  --top-k 10
  --operation fanout-query --query-limit 1000
  --load-concurrency 1 --query-concurrency 8
  --warmup-rounds 0 --rounds 1 --min-queries 0 --min-seconds 0)

for m in 1 2 4 8 16 32 64 96; do
  echo "=== m=$m OFF ===" | tee -a $R
  timeout 900 $BIN/ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    > "$OUT/off_m${m}.log" 2>&1
  echo "exit=$?" | tee -a $R

  echo "=== m=$m ON(budget=4, no-prune) ===" | tee -a $R
  timeout 1800 $BIN/ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    --tree-search-budget 4 \
    > "$OUT/on_m${m}.log" 2>&1
  echo "exit=$?" | tee -a $R

  echo "=== m=$m PRUNING(budget=4, prune) ===" | tee -a $R
  timeout 1800 $BIN/ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    --tree-search-budget 4 --tree-prune \
    > "$OUT/pruning_m${m}.log" 2>&1
  echo "exit=$?" | tee -a $R
  assert_tmp_x_untouched || exit 1
done
log "=== phase 05 complete ===" | tee -a $R
