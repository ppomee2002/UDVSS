# LSH-aligned bucket locators (feature/pg-lsh-aligned, ceph-vectornode-lsh-aligned).
# Same cluster recipe as pg-objloc-1m; only the bench (librados header) differs.
#   --bucket-locators       : PG = rjenkins(bucket locator) folded (old, hashed)
#   --lsh-aligned-locators  : PG = low log2(pg_num) bits of the LSH bucket id
set -u
export EXP=/home/dev_path/experiments/lsh-aligned-1m
export OSD_SRC=/home/dev_path/ceph-vectornode-staged-prefix   # vstart, mgr, CLI
export OB=$OSD_SRC/build/bin
export NB=/root/pgsplit-build/bin     # crimson-osd, ceph-mon: same OSD/mon source as the snapshot (only librados + bench changed)
export BB=/root/lshalign-build/bin    # ceph-vector-bench-native with --lsh-aligned-locators
export DATA=/home/dev_path/ceph/sift1M/sift
export VDEST=/vstart-extra/lsh-aligned-1m
export CONF=$VDEST/ceph.conf
export CEPH_KEYRING=$VDEST/keyring
export PORT=7380
export IDXBASE="--routing-mode pg-suboid --raw-euclidean-geometry \
--k 10 --l 4 --seed 1315423911 --d 1 \
--distance-bucket-bits 4 --residual-bits 4 --object-locators"
export QARGS="--probe-limit-per-pg 1 --hamming-radius 10 --top-k 10 \
--query-table-count 1 --margin-ordered-probes"
export TREE="--tree-search-budget 64 --tree-no-improve-patience 4 --tree-onode-budget 64"
export DSET="--base $DATA/sift_base.fvecs --query $DATA/sift_query.fvecs \
--groundtruth $DATA/sift_groundtruth.ivecs"
export MLIST="1 8 16 32 64 96"
export QLIMIT=150
export CMDLOG=$EXP/logs/commands.log
export STATELOG=$EXP/logs/pg_state.log

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

# $1 pool $2 C (routing / partition count) $3 live pg_num $4 tag
census() {
  $OB/rados -c $CONF -p $1 ls 2>/dev/null | sort > $EXP/results/$4.objs
  bench --operation object-census --pool $1 --index ${1}_index \
    $IDXBASE --pg-num $2 --partition-num $2 $DSET \
    --object-list $EXP/results/$4.objs --top-k 10 \
    > $EXP/results/$4.census.csv 2> $EXP/results/$4.census.err
  grep CENSUS_TOTAL $EXP/results/$4.census.err
}

# $1 pool $2 C $3 live pg_num $4 out raw file $5 tag
msweep() {
  for m in $MLIST; do
    local out
    out=$(bench --operation fanout-query --pool $1 --index ${1}_index \
      $IDXBASE --pg-num $2 --partition-num $2 --live-pg-num $3 \
      $QARGS $DSET --m $m --query-limit $QLIMIT $TREE \
      --query-concurrency 1 --warmup-rounds 0 --rounds 1 --io-depth 0 \
      --query-results-csv $EXP/results/$5_m$m.rows.csv 2>&1)
    local line; line=$(echo "$out" | grep "^FANOUT_QUERY")
    if [ -z "$line" ]; then
      echo "$5 m=$m NO_RESULT $(echo "$out" | grep -i error | head -2 | tr '\n' ' ')" | tee -a $4
      return 1
    fi
    echo "$5 pool=$1 C=$2 live=$3 m=$m $line" >> $4
    echo "  $5 m=$m $(echo "$line" | grep -oE "(failures|missing_objects|duplicate_entries|recall_at_top_k|avg_total_matching_records_per_query|osd_requests_per_query|logical_qps)=[^ ]+" | tr '\n' ' ')"
  done
}

# Index args: identical to pg-objloc-1m bucket runs except the locator mode.
export IB_OLD="--routing-mode pg-suboid --raw-euclidean-geometry --k 10 --l 4 \
--seed 1315423911 --d 1 --distance-bucket-bits 4 --residual-bits 4 --bucket-locators"
export IB_NEW="${IB_OLD/--bucket-locators/--lsh-aligned-locators}"
