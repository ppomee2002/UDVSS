#!/bin/bash
cd /home/dev_path/ceph-vectornode-boundary-search/build
SCRATCH=/tmp/udvss-scratch/boundary-search
OUT=/home/dev_path/experiments/selective-fanout-sift1m
R=$(cat "$SCRATCH/recon_args.txt")
one() {
  n="$1"; shift
  ./bin/ceph-vector-bench-native $R "$@" > "$OUT/logs/inner_$n.log" 2>&1
  echo "== $n : $(grep -oE 'recall_at_top_k=[0-9.]+' $OUT/logs/inner_$n.log) $(grep -oE 'seconds=[0-9.]+' $OUT/logs/inner_$n.log)"
}
one tree16_m32  --m 32 --tree-search-budget 16
one tree64_m32  --m 32 --tree-search-budget 64
one tree64p_m32 --m 32 --tree-search-budget 64 --tree-prune
