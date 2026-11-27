# Shared environment for the vectornode tree-boundary 1M experiment.
# NOTHING here may touch /tmp/x -- that cluster and its data are off limits.
set -u
export EXP=/home/dev_path/experiments/vectornode-tree-boundary-1m
export VDEST=/home/dev_path/vstart/vectornode-tree-boundary-1m
export SRC=/home/dev_path/ceph-vectornode-boundary-search
export BUILD=$SRC/build
export BIN=$BUILD/bin
export CEPH_CONF=$VDEST/ceph.conf
export CEPH_KEYRING=$VDEST/keyring
export DATA=/home/dev_path/ceph/sift1M/sift
export POOL=tb1m_v1024
export INDEX=tb1m_v1024_index
export PGNUM=1024
# Routing/index config -- identical to the /tmp/x sift1m_raw_v1024 index so
# the baseline is directly comparable to the numbers recorded 2026-08-27.
export IDXARGS="--routing-mode pg-suboid --raw-euclidean-geometry \
--pg-num $PGNUM --k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4"
export QARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes"
export DSET="--base $DATA/sift_base.fvecs --query $DATA/sift_query.fvecs \
--groundtruth $DATA/sift_groundtruth.ivecs"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Refuses to run if the protected cluster is not the one we think it is.
assert_tmp_x_untouched() {
  if ! pgrep -x crimson-osd >/dev/null; then
    echo "FATAL: no crimson-osd running at all" >&2; return 1
  fi
  local found=0
  for p in $(pgrep -x crimson-osd); do
    grep -qa "/tmp/x/ceph.conf" /proc/$p/cmdline 2>/dev/null && found=1
  done
  if [ "$found" != 1 ]; then
    echo "FATAL: /tmp/x crimson-osd is NOT running -- refusing to continue" >&2
    return 1
  fi
  log "guard ok: /tmp/x osd alive"
}

# Only ever kills the OSD belonging to THIS experiment's conf path, and
# waits for it to actually exit -- crimson-osd has been observed ignoring
# SIGTERM here, and starting a second OSD while the first still holds the
# device aborts the new one at boot (SIGABRT in OSD::main).
stop_exp_osd() {
  local pids=""
  for p in $(pgrep -x crimson-osd); do
    if grep -qa "$VDEST/ceph.conf" /proc/$p/cmdline 2>/dev/null; then
      pids="$pids $p"
    fi
  done
  [ -z "$pids" ] && { log "no experiment osd running"; return 0; }
  log "stopping experiment osd:$pids"
  kill -TERM $pids 2>/dev/null || true
  for i in $(seq 1 12); do
    sleep 5
    local alive=0
    for p in $pids; do kill -0 $p 2>/dev/null && alive=1; done
    [ $alive = 0 ] && { log "experiment osd exited cleanly"; return 0; }
  done
  log "SIGTERM ignored after 60s -- escalating to SIGKILL (SeaStore replays its journal on next mount)"
  kill -9 $pids 2>/dev/null || true
  sleep 10
  for p in $pids; do kill -0 $p 2>/dev/null && { log "FATAL: osd $p will not die"; return 1; }; done
  return 0
}

# Blocks until the experiment OSD reports up, so a bench never runs against
# a down cluster and silently hangs.
wait_osd_up() {
  for i in $(seq 1 60); do
    if $BIN/ceph -c $CEPH_CONF -s 2>/dev/null | grep -qE "osd: 1 osds: 1 up"; then
      log "experiment osd up after $((i*5))s"; return 0
    fi
    sleep 5
  done
  log "FATAL: experiment osd did not come up within 300s"
  tail -20 $EXP/logs/osd_stdout.log >&2
  return 1
}

start_exp_osd() {
  cd $BUILD
  nohup $BIN/crimson-osd -i 0 -c $CEPH_CONF -f \
    >> $EXP/logs/osd_stdout.log 2>&1 &
  log "started experiment osd pid $!"
  wait_osd_up
}
