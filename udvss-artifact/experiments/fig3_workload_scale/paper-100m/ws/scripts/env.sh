# Workload scaling re-measure (revision 2026-10-08): N = 1M -> 10M -> 100M,
# pg_num 1024 fixed, LSH + SQ8, now on the latest split/merge code
# (feature/pg-lsh-aligned-sq8 = lsh-aligned + SQ8 port) with --lsh-aligned-locators
# and a full PG scan. Topology as workload-scale-sq8: mon/mgr + client on 94
# (ceph-s-qn-scaling 00_mon.sh), 1 OSD on 109 (smp 4, pinned).
# Changes vs workload-scale-sq8 (everything else identical):
#   OSD = bundle-lshsq8, bench = /root/lshsq8-build, + --lsh-aligned-locators,
#   full PG scan (--full-pg-scan --object-list, tree budget 0, no patience),
#   OSD_MEM 32G at every N (was 16G at 1m/10m), 3 reps, p95 recorded.
export ROUTE=lsh
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
export WS=/home/dev_path/experiments/paper-100m/ws
export RES=$WS/results
export BUNDLE=bundle-lshsq8
export BB=/root/lshsq8-build/bin                 # ceph-vector-bench-native (SQ8 + lsh-aligned)
export OSD_DEV_SIZE=256G
export OSD_MEM=32G
export WS_PG=1024
export WS_MS="${WS_MS:-8 32 96 384}"
export WS_NQ=${WS_NQ:-500} WS_WARM=${WS_WARM:-50} WS_REPS=${WS_REPS:-3}
# full rows give recall/candidates only (deterministic: 3 reps identical at 1m), so
# from 10m on they run once at concurrency 32; tree (latency) rows stay C=1 x WS_REPS.
export WS_FULL_REPS=${WS_FULL_REPS:-1} WS_FULL_C=${WS_FULL_C:-32}
export DDIR=/home/dev_path/datasets/deep1b
export GTDIR=/home/dev_path/experiments/workload-scale-sq8/groundtruth
ws_n() { case $1 in 1m) echo 1000000;; 10m) echo 10000000;; 100m) echo 100000000;; esac; }
ws_dset() {
  echo "--base $DDIR/deep100m_base.fvecs --base-limit $(ws_n $1) \
--query $GTDIR/deep1b_query.fvecs --groundtruth $GTDIR/deep$1_gt_q1000.ivecs"; }
ws_pool() { echo ws_lshsq8_pg${WS_PG}_$1; }
# sq_idxargs = workload-scale-sq8 index args (normalized geometry; SQ8 cannot take
# --raw-euclidean-geometry) + the latest placement.
ws_idxargs() { echo "$(sq_idxargs $1) --lsh-aligned-locators"; }
# QARGS without the tree limits: a full PG scan requires --tree-search-budget 0.
ws_qargs_full() {
  echo "$QARGS" | sed -e 's/--tree-search-budget [0-9]*//' -e 's/--tree-no-improve-patience [0-9]*//'
  echo "--tree-search-budget 0 --full-pg-scan --object-list $RES/objs/$1.objs"; }
# Latency series (user, 2026-10-08): --full-pg-scan is a client-side expansion
# (one request per bucket object, 478/q at 1m M=8 vs 8), so latency comes from
# the OSD tree with every limit lifted (M requests/q, as workload-scale-sq8 "full").
# At 1m it scans 98.9-99.0% of the full-PG candidates (not exact).
ws_qargs_tree() {
  echo "$QARGS" | sed -e 's/--tree-search-budget [0-9]*/--tree-search-budget 1000000/' \
                      -e 's/--tree-no-improve-patience [0-9]*/--tree-no-improve-patience 1000000/'
  echo "--tree-onode-budget 0"; }
