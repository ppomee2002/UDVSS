# Deep1B 1B Ceph run on 94 (node94) -- same criteria as scale-deep 100M:
# 1 crimson OSD, 8 reactors on cores 0-7, client on 8-47, no memory cap,
# C ladder at M16, M sweep x3, >= 120 s per point, B=1024, first 1000 public
# queries, official 1B GT. Sourced by every script here.
set -u
X=/home/dev_path/experiments/deep1b-1b
RUN=/data1b/run                      # model, placement, converted queries/GT
V=/vstart-extra/d1b                  # cluster dir (SeaStore device lives here)
SRC=/home/dev_path/ceph-vectornode-seastore-batch
BLD=$SRC/build
BIN=$BLD/bin                         # mon/mgr/osd/ceph CLI (OSD md5 695566a7)
BENCH=$X/bench/obj/ceph-vector-bench-native
export LD_LIBRARY_PATH=$BLD/lib
PORT=7610
OSD_CORES=0-7
CLIENT_CORES=8-47
PG=65536
N=1000000000
DEV_SIZE=600G
LOAD_CONC=256    # steady 3.1-3.3k/s; 1024 2.6-2.8k/s, 4096 ~3k/s with conflicts (2026-10-24)
CHUNK=20000000                       # rows per load process (7.7 GB of base)
MIN_SECONDS=120
BATCH=1024
PROBE_BUDGET=256
BASE=/data1b/deep1b/base.1B.fbin
QUERY=$RUN/deep1b_query1000.fvecs
GT=$RUN/deep1b_gt1000.ivecs
MODEL=$RUN/pivot_model_pg65536.bin
PLACEMENT=$RUN/placement_pg65536.bin
POOL=d1b
INDEX=${POOL}_index
# pivot arm exactly as 100M (bal, 64 samples/PG, Lloyd 25, eps 0.1, cap 0.1)
ROUTE="--routing-mode pivot --k 10 --l 4 --seed 1315423911 --d 1 --top-k 10 --pivot-build-sample 4194304 --pivot-refine-iters 25 --pivot-balance-eps 0.1 --pivot-place-cap-eps 0.1 --pivot-model-file $MODEL --pg-num $PG"
DSET="--base $BASE --query $QUERY --groundtruth $GT"
BASE_BYTES=384000000008
# MINI=1: the same scripts on Deep 1M / PG 1024 (pipeline self-test)
if [ "${MINI:-0}" = 1 ]; then
  X=/home/dev_path/experiments/deep1b-1b/mini; RUN=$X/run; V=/vstart-extra/d1b-mini
  PORT=7620; PG=1024; N=1000000; DEV_SIZE=64G; CHUNK=300000; MIN_SECONDS=10
  BASE=$RUN/deep1m.fbin; BASE_BYTES=$(stat -c %s $BASE 2>/dev/null || echo 0)
  MODEL=$RUN/pivot_model_pg1024.bin; PLACEMENT=$RUN/placement_pg1024.bin; POOL=d1bmini; INDEX=${POOL}_index
  ROUTE="--routing-mode pivot --k 10 --l 4 --seed 1315423911 --d 1 --top-k 10 --pivot-build-sample 65536 --pivot-refine-iters 25 --pivot-balance-eps 0.1 --pivot-place-cap-eps 0.1 --pivot-model-file $MODEL --pg-num $PG"
  QUERY=/home/dev_path/datasets/deep1b1m/deep1b1m_query.fvecs; GT=/home/dev_path/datasets/deep1b1m/deep1b1m_groundtruth.ivecs
  DSET="--base $BASE --query $QUERY --groundtruth $GT"
fi
# Queries / count use the SAME bench binary as the scale-deep 100M Ceph run
# (382f02df, -O2 generic build), not the 1B load build: the query path is then
# identical code and code generation. With the cached pivot model it reads only
# the first --base row (for the dimension), so a 1-row .fvecs of row 0 stands in.
QBENCH=$BLD/bin/ceph-vector-bench-native
QBASE=$RUN/deep1b_row0.fvecs
[ "${MINI:-0}" = 1 ] && QBASE=/home/dev_path/datasets/deep1b1m/deep1b1m_base.fvecs
QDSET="--base $QBASE --query $QUERY --groundtruth $GT"
S=/home/dev_path/experiments/deep1b-1b/scripts
CONF=$V/ceph.conf
LOG=$X/logs/driver.log
mkdir -p $X/logs $X/results $RUN

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $LOG; }
die() { log "FATAL: $*"; exit 1; }
ceph_() { timeout 120 $BIN/ceph -c $CONF "$@" 2>/dev/null; }
osd_pid() { ps -eo pid,args | grep "[b]in/crimson-osd -i 0 -c $V/ceph.conf" | awk '{print $1}' | head -1; }
bench() { taskset -c $CLIENT_CORES $BENCH "$@"; }
export CEPH_CONF=$CONF CEPH_KEYRING=$V/keyring
