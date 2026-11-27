#!/bin/bash
cd /home/dev_path/ceph-vectornode-boundary-search/build
COMMON="$(cat /tmp/udvss-scratch/boundary-search/common_args.txt)"
SEL="--selective --selective-schedule 8,16,32,64,96 --m 96"
run() {
  name="$1"; shift
  ./bin/ceph-vector-bench-native $COMMON $SEL "$@" \
    --selective-csv "/home/dev_path/experiments/selective-fanout-sift1m/results/rerank_$name.csv" > "/home/dev_path/experiments/selective-fanout-sift1m/logs/rerank_$name.log" 2>&1
  echo "done $name rc=$?"
}
run tableonly   --query-table-count 1
run margin      --margin-ordered-probes
run both        --query-table-count 1 --margin-ordered-probes
