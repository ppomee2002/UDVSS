#!/bin/bash
cd /home/dev_path/ceph-vectornode-boundary-search/build
SCRATCH=/tmp/udvss-scratch/boundary-search
OUT=/home/dev_path/experiments/selective-fanout-sift1m
R=$(cat "$SCRATCH/recon_args.txt")
# budget이 구속하는지 확인
for b in 1 4; do
  ./bin/ceph-vector-bench-native $R --m 32 --tree-search-budget $b > "$OUT/logs/inner_tree${b}_m32.log" 2>&1
  echo "budget=$b : $(grep -oE 'recall_at_top_k=[0-9.]+|avg_total_matching_records_per_query=[0-9.]+' $OUT/logs/inner_tree${b}_m32.log | tr '\n' ' ')"
done
# M 스윕: selective로 곡선 전체를 한 번에
./bin/ceph-vector-bench-native $R --m 128 --tree-search-budget 16 \
  --selective --selective-schedule 8,16,32,64,128 \
  --selective-csv "$OUT/results/tree_msweep.csv" > "$OUT/logs/tree_msweep.log" 2>&1
echo "msweep rc=$? : $(grep -oE 'recall_at_top_k=[0-9.]+|seconds=[0-9.]+' $OUT/logs/tree_msweep.log | tr '\n' ' ')"
