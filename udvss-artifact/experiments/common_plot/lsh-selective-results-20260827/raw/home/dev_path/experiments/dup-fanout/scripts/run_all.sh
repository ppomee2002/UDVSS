#!/bin/bash
# Full sequence, strictly serial (the two clusters share cores 0-7, so any
# overlap would corrupt QPS). Pivot chain first, then LSH.
S=/home/dev_path/experiments/dup-fanout/scripts
set -u
for M in pivot lsh; do
  for step in "measure 1" "grow 2" "measure 2" "grow 3" "measure 3"; do
    set -- $step
    echo "########## $M $1 $2 :: $(date -u +%H:%M:%S)"
    MODE=$M bash $S/$1.sh $2 || { echo "########## ABORT: $M $1 $2 failed"; exit 1; }
  done
done
echo "########## ALL DONE :: $(date -u +%H:%M:%S)"
