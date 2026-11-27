# Duplicate-storage (write fan-out d=1/2/3) QPS experiment.
#
# duplicate=D means each base vector is physically stored D times, in D
# distinct PGs -- the tool's existing --d write fan-out. The d=2 and d=3
# datasets are built INCREMENTALLY on top of the existing d=1 1M pools with
# --write-rank-begin, which is proven (experiments/dup-fanout/results/
# eq_*.cmp, 50k vectors, pg_num 64) to leave the identical per-object
# record census as a full --d D reload.
set -u
export EXP=/home/dev_path/experiments/dup-fanout
export SRC=/home/dev_path/ceph-vectornode-staged-prefix
export BUILD=$SRC/build
export BIN=$BUILD/bin
export DATA=/home/dev_path/ceph/sift1M/sift
export DSET="--base $DATA/sift_base.fvecs --query $DATA/sift_query.fvecs \
--groundtruth $DATA/sift_groundtruth.ivecs"
export MODE=${MODE:-pivot}

if [ "$MODE" = pivot ]; then
  export VDEST=/home/dev_path/vstart/ffpivot-smp8-1m
  export POOL=ffp_v1024_smp8
  export INDEX=ffp_v1024_smp8_index
  export PGNUM=1024
  # Identical to ffpivot-smp8-1m/scripts/env.sh except --d, which this
  # experiment varies. Routing itself is untouched.
  export IDXBASE="--routing-mode pivot --pg-num $PGNUM --k 10 --l 4 \
--seed 1315423911 --pivot-build-sample 16384 --pivot-probe-budget 96"
  export QEXTRA=""
  export QLIMIT=1000
  # (M, concurrency, io_depth) winners from the d=1 M sweep
  export ARMS="1:60:32 8:40:32 16:20:64 32:10:128"
else
  export VDEST=/home/dev_path/vstart/vectornode-smp8-1m
  export POOL=tb1m_v1024_smp8
  export INDEX=tb1m_v1024_smp8_index
  export PGNUM=1024
  export IDXBASE="--routing-mode pg-suboid --raw-euclidean-geometry \
--pg-num $PGNUM --k 10 --l 4 --seed 1315423911 \
--distance-bucket-bits 4 --residual-bits 4"
  export QEXTRA="--tree-search-budget 64 --tree-no-improve-patience 4 \
--tree-onode-budget 64"
  export QLIMIT=150
  export ARMS="1:5:0 8:5:0 16:1:0 32:1:0"
fi
export CEPH_CONF=$VDEST/ceph.conf
export CEPH_KEYRING=$VDEST/keyring

log() { echo "[$(date -u +%H:%M:%S)] [$MODE] $*"; }

# LSH-only: --probe-limit-per-pg/--hamming-radius/--margin-ordered-probes as
# in the canonical sweeps, plus --query-table-count == d (vector_pg_lsh.h:763:
# data written with write_pg_count=d only lands in PGs derived from tables
# [0,d), so probing beyond that spends budget on PGs that cannot hold a
# related vector). Pivot has no table concept.
qargs() {  # $1 = d
  if [ "$MODE" = pivot ]; then
    echo "--top-k 10"
  else
    echo "--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count $1 --margin-ordered-probes"
  fi
}

stop_osd() {
  local pids=""
  for p in $(pgrep -x crimson-osd); do
    grep -qa "$CEPH_CONF" /proc/$p/cmdline 2>/dev/null && pids="$pids $p"
  done
  [ -z "$pids" ] && { log "no osd running"; return 0; }
  log "stopping osd:$pids"
  kill -TERM $pids 2>/dev/null || true
  for i in $(seq 1 12); do
    sleep 5
    local alive=0
    for p in $pids; do kill -0 $p 2>/dev/null && alive=1; done
    [ $alive = 0 ] && { log "osd exited cleanly"; return 0; }
  done
  log "SIGTERM ignored -- SIGKILL"; kill -9 $pids 2>/dev/null || true; sleep 10
}

wait_osd_up() {
  local pid=$1
  for i in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || { log "FATAL: osd pid $pid died"; tail -20 $EXP/logs/osd_${MODE}.log >&2; return 1; }
    if $BIN/ceph -c $CEPH_CONF -s 2>/dev/null | grep -qE "osd: 1 osds: 1 up"; then
      sleep 5
      kill -0 "$pid" 2>/dev/null || { log "FATAL: osd died right after up"; return 1; }
      log "osd up after $((i*5))s"; return 0
    fi
    sleep 5
  done
  log "FATAL: osd did not come up in 300s"; tail -20 $EXP/logs/osd_${MODE}.log >&2; return 1
}

start_osd() {
  cd $BUILD
  bash -c 'ulimit -l unlimited; exec nohup '"$BIN"'/crimson-osd -i 0 -c '"$CEPH_CONF"' -f' \
    >> $EXP/logs/osd_${MODE}.log 2>&1 &
  local pid=$!
  log "started osd pid $pid"
  wait_osd_up $pid
}
