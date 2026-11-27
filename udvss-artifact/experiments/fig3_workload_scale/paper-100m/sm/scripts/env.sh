# Split/merge at 1M and 100M, full PG scan (revision 2026-10-08).
# Cluster recipe = lsh-aligned-1m (local vstart on 94, staged-prefix vstart,
# ceph-mon from /root/pgsplit-build, crimson-osd --crimson-smp 8), with the
# OSD + bench from feature/pg-lsh-aligned-sq8 (lsh-aligned + SQ8 port).
# Changes vs lsh-aligned-1m 30_splitmerge.sh, all forced by SQ8 / Deep1B / 100M:
#   Deep1B prefix (--base-limit) + its 1000-query GT; --quantize int8; no
#   --raw-euclidean-geometry (SQ8 cannot place with it); device 256G;
#   full PG scan for every stage (main result; tree sweep not run);
#   split and merge pools run one after the other (two 100M pools do not fit).
set -u
export EXP=/home/dev_path/experiments/paper-100m/sm
export OSD_SRC=/home/dev_path/ceph-vectornode-staged-prefix   # vstart, mgr, CLI
export OB=$OSD_SRC/build/bin
export MB=/root/pgsplit-build/bin      # ceph-mon (crimson_allow_pg_merge), as lsh-aligned-1m
export NB=/root/lshsq8-build/bin       # crimson-osd + ceph-vector-bench-native (SQ8 + lsh-aligned)
export BB=$NB
export VDEST=/vstart-extra/paper-sm
export CONF=$VDEST/ceph.conf
export CEPH_KEYRING=$VDEST/keyring
export PORT=7390
export DEV_SIZE=${DEV_SIZE:-256G}
export DDIR=/home/dev_path/datasets/deep1b
export GTDIR=/home/dev_path/experiments/workload-scale-sq8/groundtruth
export QUANT="--quantize int8"
export IB="--routing-mode pg-suboid --k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4 --lsh-aligned-locators"
export QARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes"
export MLIST="${MLIST:-1 8 16 32 64 96}"
export QLIMIT=150
export CMDLOG=$EXP/logs/commands.log
export STATELOG=$EXP/logs/pg_state.log
sm_n() { case $1 in 1m) echo 1000000;; 10m) echo 10000000;; 100m) echo 100000000;; esac; }
sm_dset() {
  echo "--base $DDIR/deep100m_base.fvecs --base-limit $(sm_n $1) \
--query $GTDIR/deep1b_query.fvecs --groundtruth $GTDIR/deep$1_gt_q1000.ivecs"; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
ceph_() { $OB/ceph -c $CONF "$@" 2>/dev/null; }
bench() {
  echo "[$(date -u +%FT%TZ)] ceph-vector-bench-native $*" >> $CMDLOG
  $BB/ceph-vector-bench-native --conf $CONF "$@"
}
pids_of() {  # $1 = comm pattern
  for p in $(pgrep -f "$1"); do
    grep -qa "$CONF" /proc/$p/cmdline 2>/dev/null && echo $p
  done
}
osd_pids() { pids_of crimson-osd; }
pool_get() { ceph_ osd pool get $1 $2 | awk '{print $2}'; }

pg_state() {  # $1 pool $2 label
  {
    echo "=== $(date -u +%FT%TZ) $1 $2"
    ceph_ osd pool ls detail | grep "'$1'"
    ceph_ osd dump -f json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['pools']:
    if p['pool_name']=='$1':
        print({k:p.get(k) for k in ('pg_num','pgp_num','pg_num_target','pgp_num_target','pg_num_pending')})"
    ceph_ pg ls-by-pool $1 -f json | python3 -c "
import json,sys,collections
d=json.load(sys.stdin); pgs=d['pg_stats'] if isinstance(d,dict) else d
print(dict(collections.Counter(p['state'] for p in pgs)))"
  } >> $STATELOG
}

all_clean() {
  local st; st=$(ceph_ pg stat)
  local tot cln
  tot=$(echo "$st" | grep -oE "^[0-9]+")
  cln=$(echo "$st" | grep -oE "[0-9]+ active\+clean[;,]" | head -1 | grep -oE "^[0-9]+")
  [ -n "$tot" ] && [ "$tot" = "$cln" ]
}

# Wait until pg_num == pgp_num == $2 and every PG in the cluster is
# active+clean. $3 = timeout seconds.
wait_pool() {
  local deadline=$(( $(date +%s) + $3 ))
  while [ $(date +%s) -lt $deadline ]; do
    if [ "$(pool_get $1 pg_num)" = $2 ] && [ "$(pool_get $1 pgp_num)" = $2 ] && all_clean; then
      return 0
    fi
    [ -z "$(osd_pids)" ] && { log "CRITICAL: OSD is gone"; return 2; }
    sleep 10
  done
  return 1
}

osd_watchdog() {
  while sleep 30; do
    if [ -z "$(osd_pids)" ]; then
      echo "[$(date -u +%H:%M:%S)] CRITICAL: OSD is gone" | tee -a $EXP/logs/watchdog.log
      for p in $(pgrep -x ceph-vector-ben); do
        grep -qa "$CONF" /proc/$p/cmdline 2>/dev/null && kill $p
      done
      return
    fi
  done
}

integrity() {  # $1 pool $2 label
  {
    echo "=== $(date -u +%FT%TZ) $1 $2"
    $OB/rados -c $CONF df 2>/dev/null | grep -E "POOL_NAME|^$1 "
    echo "objects_listed=$($OB/rados -c $CONF -p $1 ls 2>/dev/null | wc -l)"
  } >> $EXP/logs/integrity.log
}

