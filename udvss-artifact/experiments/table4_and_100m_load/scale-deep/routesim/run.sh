#!/bin/bash
# Offline route-sim on deep1b10m, PG 4096: pick the pivot variant for the Ceph
# d10m_pg4096 arm (user 2026-10-10). Pinned off the engine cores.
B=/home/dev_path/ceph-vectornode-seastore-batch/build
export LD_LIBRARY_PATH=$B/lib
D=/home/dev_path/datasets/deep1b10m/deep1b10m
O=/home/dev_path/experiments/scale-deep/routesim/logs
COMMON="--operation route-sim --routing-mode pivot --pg-num 4096 --seed 1315423911 --d 1 --top-k 10 --query-limit 1000 --sim-m-list 1,2,4,8,16,32,64,96 --base ${D}_base.fvecs --query ${D}_query.fvecs --groundtruth ${D}_groundtruth.ivecs"
run() { t=$1; shift; [ -s $O/$t.log ] && grep -q GT_PG_RANK $O/$t.log && return
  echo "[$(date -u +%H:%M:%S)] $t"; taskset -c 32-47 $B/bin/ceph-vector-bench-native $COMMON "$@" > $O/$t.log 2>&1 || echo "FAILED $t"; }
run ll3_s65536      --pivot-build-sample 65536  --pivot-refine-iters 3
run sph_s65536_it25 --pivot-build-sample 65536  --pivot-refine-iters 25 --pivot-spherical
run sph_s262144_it25 --pivot-build-sample 262144 --pivot-refine-iters 25 --pivot-spherical
run s262144_it25    --pivot-build-sample 262144 --pivot-refine-iters 25
run bal_s262144_it25 --pivot-build-sample 262144 --pivot-refine-iters 25 --pivot-balance-eps 0.1 --sim-place-cap-eps 0.1
echo "[$(date -u +%H:%M:%S)] done"
