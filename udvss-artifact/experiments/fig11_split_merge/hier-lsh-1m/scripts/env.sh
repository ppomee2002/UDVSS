# Hierarchical-LSH re-run of tree_pareto_paper_m32 and selective_tradeoff
# (SIFT1M, PG 1024, lsh-aligned locators) on 109. Runs ON 109.
#   bundle: pg-split crimson-osd/ceph-mon (same as lsh-aligned-1m) +
#           bench from feature/pg-lsh-hierarchical
#   mon/mgr/CLI: 109's native seastore-batch build (vstart)
set -u
export H=$HOME/hier
export SRC=/home/dev_path/ceph-vectornode-seastore-batch
export OB=$SRC/build/bin
export BUNDLE=$H/bundle
export VDEST=$H/cluster
export CONF=$VDEST/ceph.conf
export PORT=7900
export IP=${NODE109_IP:?set NODE109_IP}
export DATA=$H/data
# NUMA 0 physical cores 2..32 (even) + HT siblings 66..96: 32 reactors.
# CPU 0/64 belong to the old ll3 cluster. Clients run on NUMA 1 (odd CPUs).
export OSD_CPUS=$(echo $(seq 2 2 32) $(seq 66 2 96) | tr ' ' ,)
export CLIENT_CPUS=$(echo $(seq 1 2 127) | tr ' ' ,)
export OSD_MEM=${OSD_MEM:-32G} OSD_DEV_SIZE=${OSD_DEV_SIZE:-256G}
export POOL=hier_v1024 INDEX=hier_v1024_index
# S = 0.5 * mean ||v - centroid|| over SIFT1M (386.3), chosen offline.
export HSCALE=${HSCALE:-193}
export IDXARGS="--routing-mode pg-suboid --raw-euclidean-geometry \
--pg-num 1024 --k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4 --lsh-aligned-locators \
--placement-mode hierarchical_lsh --hierarchical-scale $HSCALE"
export SQARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes"
export DSET="--base $DATA/sift1m_base.fvecs --query $DATA/sift1m_query.fvecs \
--groundtruth $DATA/sift1m_groundtruth.ivecs"
export CMDLOG=$H/logs/commands.log

log() { echo "[$(date -u +%FT%TZ)] $*"; }
ceph_() { $OB/ceph -c $CONF "$@" 2>/dev/null; }
bench() {
  echo "[$(date -u +%FT%TZ)] bench $*" >> $CMDLOG
  taskset -c $CLIENT_CPUS $BUNDLE/run.sh ceph-vector-bench-native --conf $CONF "$@"
}
# Only daemons of THIS cluster (cmdline names our conf).
exp_pids() {
  for p in $(pgrep -f -- "$H/"); do
    grep -qaE -- "(ceph-mon|ceph-mgr|crimson-osd)" /proc/$p/cmdline 2>/dev/null &&
      grep -qaF -- "$H/" /proc/$p/cmdline 2>/dev/null && echo $p
  done
}
