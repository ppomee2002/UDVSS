#!/bin/bash
cd /home/dev_path/ceph-vectornode-boundary-search/build
SCRATCH=/tmp/udvss-scratch/boundary-search
OUT=/home/dev_path/experiments/selective-fanout-sift1m
R=$(cat "$SCRATCH/recon_args.txt")
# 1) M=256 tree: 정말 포화하는가
./bin/ceph-vector-bench-native $R --m 256 --tree-search-budget 16 > "$OUT/logs/tree_m256.log" 2>&1
echo "tree m256 : $(grep -oE 'recall_at_top_k=[0-9.]+|avg_total_matching_records_per_query=[0-9.]+|seconds=[0-9.]+' $OUT/logs/tree_m256.log | tr '\n' ' ')"
# 2) PG 전체 sub_oid 훑기(내부탐색 없이): 89% 천장이 깨지는가
#    distance_bucket_bits=4 -> 16 buckets, residual_bits=4 -> radius 4 = 전체 16개 => 256 sub_oid
for M in 16 32; do
  ./bin/ceph-vector-bench-native $R --m $M \
    --distance-bucket-radius 15 --residual-hamming-radius 4 --probe-limit-per-pg 0 \
    > "$OUT/logs/fullpg_m$M.log" 2>&1
  echo "fullpg m$M : $(grep -oE 'recall_at_top_k=[0-9.]+|avg_total_matching_records_per_query=[0-9.]+|seconds=[0-9.]+|error.*' $OUT/logs/fullpg_m$M.log | tr '\n' ' ')"
done
