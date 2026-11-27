#!/bin/bash
# User 2026-10-11: before any 100M load, pick the PG count offline.
#  1. wait for the PG16384 fast run (never share CPU with a measurement)
#  2. one extra PG16384 point (M=32, 1 rep) -> second point for the per-PG-count cost term
#  3. route-sim on the full deep100m base, PG 4096 / 8192 / 16384, balanced pivot at
#     64 samples/PG (same family as the d10m_pg4096/pg16384 arms)
R=/home/dev_path/experiments/scale-deep; S=$R/ceph/scripts; L=$R/chain.log; O=$R/routesim100m/logs
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
until grep -q "PG16384 FAST DONE" $L; do sleep 30; done
pgrep -x crimson-osd >/dev/null && sleep 60
log "--- pg16384 extra point M=32 r1 ---"
if bash $S/activate_workload.sh d10m_pg16384 --skip-count --skip-warmup > $R/ceph/logs/d10m_pg16384_activate2.log 2>&1; then
  out=$(bash $S/run_one.sh d10m_pg16384 32 1024 20 r1 2>&1 | tail -2 | head -1)
  echo "  [pg16384] $out" >> $R/ceph/logs/run_all.log
  log "pg16384 M32 r1: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|cand=[0-9.]+' | tr '\n' ' ')"
else log "FAILED: pg16384 activate2"; fi
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
B=/home/dev_path/ceph-vectornode-seastore-batch/build; export LD_LIBRARY_PATH=$B/lib
COMMON="--operation route-sim --routing-mode pivot --seed 1315423911 --d 1 --top-k 10 --query-limit 1000 \
 --sim-m-list 1,2,4,8,16,32,64,96,128,192,256 --pivot-probe-budget 256 --pivot-refine-iters 25 \
 --pivot-balance-eps 0.1 --sim-place-cap-eps 0.1 \
 --base /home/dev_path/datasets/deep1b/deep100m_base.fvecs \
 --query /home/dev_path/datasets/deep1b10m/deep1b10m_query.fvecs \
 --groundtruth /home/dev_path/experiments/workload-scale-sq8/groundtruth/deep100m_gt_q1000.ivecs"
for pg in 4096 8192 16384; do
  t=bal_pg${pg}_s$((pg*64)); [ -s $O/$t.log ] && grep -q GT_PG_RANK $O/$t.log && continue
  log "routesim100m $t start"
  taskset -c 8-47 $B/bin/ceph-vector-bench-native $COMMON --pg-num $pg --pivot-build-sample $((pg*64)) > $O/$t.log 2>&1 \
    && log "routesim100m $t done" || log "FAILED: routesim100m $t"
done
log "=== ROUTESIM100M DONE ==="
