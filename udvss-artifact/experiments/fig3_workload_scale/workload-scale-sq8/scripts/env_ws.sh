# Workload-scale (N = 1M -> 10M -> 100M) with PG fixed, LSH+SQ8, M sweep.
# Same cluster/topology/LSH/SQ8 settings as ceph-s-qn-scaling phase 2
# (env_sq8.sh, ROUTE=lsh); only the dataset (Deep1B prefix) and device size change.
export ROUTE=lsh
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
export WS=/home/dev_path/experiments/workload-scale-sq8
export RES=$WS/results
export OSD_DEV_SIZE=256G                      # 100M SQ8 ~10 GB raw + onodes
export OSD_MEM=32G                            # 16G: shard 0 reached 3.87/4 GiB during the 100m load (dirty + LRU)
export WS_PG=1024
export WS_MS="8 32 96 384"
export WS_NQ=${WS_NQ:-500} WS_WARM=${WS_WARM:-50}
export DDIR=/home/dev_path/datasets/deep1b
ws_n() { case $1 in 1m) echo 1000000;; 10m) echo 10000000;; 100m) echo 100000000;; esac; }
ws_dset() {  # 1m|10m|100m -- one 100M fvecs, prefix via --base-limit
  echo "--base $DDIR/deep100m_base.fvecs --base-limit $(ws_n $1) \
--query $DDIR/deep1b_query.fvecs --groundtruth $DDIR/deep$1_gt_q1000.ivecs"; }
ws_pool() { echo ws_lsh_pg${WS_PG}_$1; }
# tree = phase-2 LSH+SQ8 settings (budget 64, patience 4, in QARGS);
# full = same probe, tree limits lifted so every onode of the PG is scanned.
ws_qargs() {
  if [ "$1" = full ]; then
    echo "$QARGS" | sed -e 's/--tree-search-budget [0-9]*/--tree-search-budget 1000000/' \
                        -e 's/--tree-no-improve-patience [0-9]*/--tree-no-improve-patience 1000000/'
  else echo "$QARGS"; fi
}
