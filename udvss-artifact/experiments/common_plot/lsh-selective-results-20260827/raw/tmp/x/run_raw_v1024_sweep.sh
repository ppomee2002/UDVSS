#!/bin/bash
set -u
cd /home/dev_path/ceph-vectornode-boundary-search/build
export PATH="$PWD/bin:$PATH" LD_LIBRARY_PATH="$PWD/lib"
OUT=/tmp/x/raw_v1024_results
mkdir -p "$OUT"

# Matches the original v0 sift1m_v3 m-sweep exactly (pg_num=1024, k=10,
# l=4, seed=1315423911, d=1, distance_bucket_bits=4, residual_bits=4,
# hamming_radius=10, query-limit=1000, base-limit=1000000, ON/PRUNING
# budget=4). Only --raw-euclidean-geometry is added (unavoidable: it's
# what makes PRUNING meaningful/safe at all).
COMMON=(--conf /tmp/x/ceph.conf
  --base /home/dev_path/ceph/sift1M/sift/sift_base.fvecs
  --query /home/dev_path/ceph/sift1M/sift/sift_query.fvecs
  --groundtruth /home/dev_path/ceph/sift1M/sift/sift_groundtruth.ivecs
  --pool sift1m_raw_v1024
  --index sift1m_raw_v1024_index
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
  echo "=== m=$m OFF ==="
  timeout 600 ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    > "$OUT/off_m${m}.log" 2>&1
  echo "exit=$?"; tail -3 "$OUT/off_m${m}.log"

  echo "=== m=$m ON(budget=4, no-prune) ==="
  timeout 600 ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    --tree-search-budget 4 \
    > "$OUT/on_m${m}.log" 2>&1
  echo "exit=$?"; tail -3 "$OUT/on_m${m}.log"

  echo "=== m=$m PRUNING(budget=4, prune) ==="
  timeout 600 ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    --tree-search-budget 4 --tree-prune \
    > "$OUT/pruning_m${m}.log" 2>&1
  echo "exit=$?"; tail -3 "$OUT/pruning_m${m}.log"

  echo "=== m=$m FULLPG ==="
  timeout 1800 ceph-vector-bench-native "${COMMON[@]}" --m "$m" \
    --distance-bucket-radius 15 --residual-hamming-radius 4 --probe-limit-per-pg 0 \
    > "$OUT/fullpg_m${m}.log" 2>&1
  echo "exit=$?"; tail -3 "$OUT/fullpg_m${m}.log"
done

echo "SWEEP1024 DONE"
