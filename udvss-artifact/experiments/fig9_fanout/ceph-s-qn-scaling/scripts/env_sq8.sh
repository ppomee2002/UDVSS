# SQ8 (int8) variant: LSH+SQ8 and pivot+SQ8. Everything else as env.sh.
# Usage: ROUTE=lsh|pivot source env_sq8.sh
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
export ROUTE=${ROUTE:-lsh}
export SQ_SRC=/home/dev_path/ceph-vectornode-seastore-batch   # vnni d260c0f78f7 + port, see sq8/*.patch
export SQ_BIN=$SQ_SRC/build/bin
export BUNDLE=bundle-sq8
export RES=${SQ_RES:-$EXP/results_sq8}   # SQ_RES: separate dir per CPU condition
export QUANT="--quantize int8"
export RECALL_TARGET=0.95
# pivot: as workload-bench (farthest-first + 3 Lloyd passes); no sub-OIDs,
# every probe scans its whole PG. lsh: as the FP32 sweep (pg-suboid +
# tree budget 64 / patience 4 -- this tree predates the exact-tree fixes).
sq_idxargs() {  # PG
  if [ "$ROUTE" = pivot ]; then
    echo "--routing-mode pivot --pg-num $1 --k 10 --l 4 --seed 1315423911 --d 1 \
--pivot-build-sample 16384 --pivot-refine-iters 3 --pivot-probe-budget $1"
  else
    # --raw-euclidean-geometry fails build_put_targets on int8 vectors
    # (target_violations=1 on the first put), so SQ8 LSH uses the v0
    # normalized-angle distance bucket; the tree search runs without --tree-prune
    # either way.
    idxargs $1 | sed 's/ --raw-euclidean-geometry//'
  fi
}
sq_qargs() {
  if [ "$ROUTE" = pivot ]; then echo "--top-k 10"
  else echo "$QARGS"; fi
}
sq_pool() { echo sq8_${ROUTE}_pg$1; }
