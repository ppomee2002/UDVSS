#!/usr/bin/env bash
# Phase B: M-sweep against the REUSED /tmp/x cluster's sift1m_raw_v1024 pool
# (already loaded with the full 1M vectors -- see MANIFEST.md). Boundary
# search and tree-prune are OFF; --query-concurrency fixed at 32 across every
# M for fair QPS comparison. M list decided from the Phase A diagnose report
# (query-level GT-hit coverage saturates by M=256).
set -Eeuo pipefail

ROOT=/home/dev_path/experiments/routing-diagnostics-sift1m
BUILD=/home/dev_path/ceph-vectornode-boundary-search/build
BENCH=$BUILD/bin/ceph-vector-bench-native
DATA=/home/dev_path/ceph/sift1M/sift
CONF=/tmp/x/ceph.conf

mkdir -p "$ROOT/logs/msweep" "$ROOT/results"

COMMON=(
  --conf "$CONF"
  --base "$DATA/sift_base.fvecs"
  --query "$DATA/sift_query.fvecs"
  --groundtruth "$DATA/sift_groundtruth.ivecs"
  --pool sift1m_raw_v1024
  --index sift1m_raw_v1024_index
  --routing-mode pg-suboid
  --raw-euclidean-geometry
  --pg-num 1024 --k 10 --l 4 --seed 1315423911 --d 1
  --distance-bucket-bits 4 --residual-bits 4 --probe-limit-per-pg 1
  --hamming-radius 10 --top-k 10
  --operation fanout-query
  --query-concurrency 32
  --warmup-rounds 0 --rounds 1 --min-queries 0 --min-seconds 0
  --fanout-log-count 20
)

M_LIST="1 2 4 8 16 32 64 128 256"

for m in $M_LIST; do
  echo "=== m=$m ==="
  "$BENCH" "${COMMON[@]}" --m "$m" \
    --fanout-log "$ROOT/logs/msweep/fanout_m${m}.log" \
    > "$ROOT/logs/msweep/m${m}.log" 2>&1
  status=$?
  echo "exit=$status"
  tail -3 "$ROOT/logs/msweep/m${m}.log"
  if [ "$status" -ne 0 ]; then
    echo "M=$m FAILED -- stopping sweep here (this IS the 'actual unique PG count' ceiling if it's a probe_pg_count mismatch, not just a fluke)."
    break
  fi
done

echo "MSWEEP DONE"
