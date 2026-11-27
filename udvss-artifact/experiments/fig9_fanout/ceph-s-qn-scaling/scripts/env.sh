# Ceph counterpart of milvus_s_qn_scaling (Milvus S / QN sweep).
# Topology mirrors the Milvus run: client + mon/mgr on node94 (94),
# every OSD on 109 (Milvus pinned every QueryNode to query109), 4 logical
# CPUs per OSD (Milvus QN limit cpu: "4").
set -u
export EXP=/home/dev_path/experiments/ceph-s-qn-scaling
export VDEST=/vstart-extra/sqn-cluster          # mon/mgr state on 94
export SRC=/home/dev_path/ceph-vectornode-boundary-search   # branch s-qn-scaling
export BUILD=$SRC/build
export BIN=$BUILD/bin
export CEPH_CONF=$VDEST/ceph.conf
export CEPH_PORT=7600
export MON_IP=${NODE94_IP:?set NODE94_IP}
export OSD_HOST=ubuntu@node109
export OSD_IP=${NODE109_IP:?set NODE109_IP}
export RDIR=/home/ubuntu/sqn                    # on 109
export OSD_PORT_MIN=7200 OSD_PORT_MAX=7499      # 68xx belongs to an old cluster on 109
export OSD_MEM=${OSD_MEM:-16G}                             # Milvus QN memory limit 16Gi
export OSD_DEV_SIZE=${OSD_DEV_SIZE:-64G}                       # split across 4 shards; PG=1 puts all data in one
export CLIENT_CPUS=8-15                         # 0-7: selective-tree OSD on 94
export DATA=/home/dev_path/datasets/deep1m
export DSET="--base $DATA/deep1m_base.fvecs --query $DATA/deep1m_query.fvecs \
--groundtruth $DATA/deep1m_groundtruth.ivecs"
# Same index/query/tree settings as selective-tree-exact (exact tree, budget
# 64, patience 4). Only --pg-num and --m change across the sweep.
idxargs() { echo "--routing-mode pg-suboid --raw-euclidean-geometry \
--pg-num $1 --k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4"; }
export QARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes \
--tree-search-budget 64 --tree-no-improve-patience 4"
# Milvus: 500 warmup + 3000 measured, NQ=1, C=1, 3 reps.
export NQUERY=3000 REPS=3

log() { echo "[$(date -u +%FT%TZ)] $*"; }
r109() { ssh -o BatchMode=yes -o ControlMaster=auto -o ControlPath=/tmp/sqn-ssh-%r@%h -o ControlPersist=600 $OSD_HOST "$@"; }
ceph_() { $BIN/ceph -c $CEPH_CONF "$@"; }

# OSD i -> 4 logical CPUs: two physical cores on one NUMA node + HT siblings.
# 109: cpu N is on NUMA N%2, sibling N+64. CPU 0/64 belong to the old cluster.
osd_cpus() {
  local i=$1 node=$(( $1 % 2 )) slot=$(( $1 / 2 ))
  local c1=$(( node + 2 * (2 * slot + 1) )) c2=$(( node + 2 * (2 * slot + 2) ))
  [ $node -eq 1 ] && { c1=$(( 1 + 2 * (2 * slot) )); c2=$(( 1 + 2 * (2 * slot + 1) )); }
  echo "$c1,$c2,$(( c1 + 64 )),$(( c2 + 64 ))"
}

# Only daemons whose cmdline names THIS cluster's conf.
exp_pids() {
  for n in ceph-mon ceph-mgr; do
    for p in $(pgrep -x $n); do
      grep -qaF -- "$CEPH_CONF" /proc/$p/cmdline 2>/dev/null && echo $p
    done
  done
}

# CPU_POOL=all: split all of 109 except CPU 0/64 (the old ll3 cluster) --
# 126 logical CPUs = 63 core+HT pairs, NUMA 0 then NUMA 1 -- across N OSDs as
# evenly as whole pairs allow. Same total as the unlimited Milvus QueryNodes.
pool_pairs() {  # all 63 "core,sibling" pairs in allocation order
  for c in $(seq 2 2 62); do echo "$c,$((c + 64))"; done
  for c in $(seq 1 2 63); do echo "$c,$((c + 64))"; done
}
# SeaStore keeps per-shard info (46 B each) in its 4 KiB device superblock, so
# one OSD can hold at most ~84 reactors (126 overflows it: "block_read_superblock:
# got decode error!" at mkfs). Cap at 42 pairs = 84 CPUs; only N=1 is affected.
POOL_MAX_PAIRS=${POOL_MAX_PAIRS:-42}
osd_cpus_pool() {  # i N
  local i=$1 n=$2 per=$(( 63 / $2 )) extra=$(( 63 % $2 ))
  local start=$(( i * per + (i < extra ? i : extra) )) cnt=$(( per + (i < extra ? 1 : 0) ))
  [ $cnt -gt $POOL_MAX_PAIRS ] && cnt=$POOL_MAX_PAIRS
  pool_pairs | sed -n "$((start + 1)),$((start + cnt))p" | paste -sd, -
}
