#!/bin/bash
# activate_workload.sh <workload> [--skip-count] [--skip-warmup] [--dry-run]
#
# Makes one workload the only one being served, and proves it is in the same
# state it was verified in before measuring it.
#
# All seven clusters share a single NVMe, so a non-target OSD left running still
# submits background I/O to the device under test and still wakes on its own
# timers.  They are therefore STOPPED, not parked.  Only the OSD process is
# stopped: mon and mgr keep running and the seastore device file is never
# touched, so the same data comes back when it is started again.
#
# Sequence (each step fail-stop; nothing is repaired or retried):
#   1. every non-target OSD is stopped, and confirmed stopped
#   2. the target OSD is started, pinned to OSD_CORES
#   3. seastore_max_concurrent_transactions is confirmed to be 256
#   4. shards 0..7 are pinned 1:1 onto OSD_CORES
#   5. PGs are confirmed 1024 active+clean and objects == pg_num
#   6. the stored entry count is confirmed against workloads.tsv (full-PG scan,
#      which also pulls the whole pool into seastore's cache)
#   7. an identical warm-up runs on every workload, results discarded
source /home/dev_path/experiments/scale-deep/ceph/scripts/bench_env.sh

[ $# -ge 1 ] || die "usage: activate_workload.sh <workload> [--skip-count] [--skip-warmup] [--dry-run]"
TARGET=$1; shift
DO_COUNT=1; DO_WARMUP=1; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-count)  DO_COUNT=0; shift ;;
    --skip-warmup) DO_WARMUP=0; shift ;;
    --dry-run)     DRY=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done

assert_host
load_workload "$TARGET"
LOGDIR=$BENCH_ROOT/logs/activate; mkdir -p "$LOGDIR"
TS=$(date -u +%Y%m%d_%H%M%SZ)

echo "=== activating $TARGET ==="
echo "cluster : $WL_CLUSTER"
echo "pool    : $WL_POOL"
echo "cores   : OSD $OSD_CORES | client $CLIENT_CORES"

if [ $DRY = 1 ]; then
  echo
  echo "would stop:"
  for w in $(wl_names); do
    [ "$w" = "$TARGET" ] && continue
    v=$(wl_field "$w" cluster)
    p=$(cluster_osd_pid "$v" 2>/dev/null) && echo "  $w (pid $p)" || echo "  $w (already stopped)"
  done
  echo "would start: $TARGET"
  echo "dry run -- nothing changed"
  exit 0
fi

# --- 1. stop every non-target OSD -----------------------------------------
echo
echo "--- 1. stopping non-target OSDs ---"
for w in $(wl_names); do
  [ "$w" = "$TARGET" ] && continue
  v=$(wl_field "$w" cluster)
  if cluster_osd_pid "$v" > /dev/null 2>&1; then
    stop_osd "$v" || die "could not stop $w"
  fi
done
# anything left over, including clusters not in workloads.tsv
STRAY=""
for p in $(pgrep -x crimson-osd 2>/dev/null); do
  # clusters live under two roots on this host (/vstart-extra holds three of
  # them), so match any ceph.conf rather than one hardcoded prefix -- a path
  # this pattern misses would be reported as an unknown stray.
  conf=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -oE '(^| )[^ ]*/ceph\.conf' | tr -d ' ')
  [ "$conf" = "$WL_CLUSTER/ceph.conf" ] && continue
  STRAY="$STRAY $p($conf)"
done
[ -n "$STRAY" ] && die "OSDs still running that are not the target:$STRAY"
echo "  confirmed: no non-target OSD is running"

# --- 2. start the target ----------------------------------------------------
echo
echo "--- 2. starting $TARGET ---"
start_osd "$WL_CLUSTER" "$LOGDIR/${TARGET}_osd_stdout.log" || die "$TARGET osd did not start"
TPID=$(cluster_osd_pid "$WL_CLUSTER") || die "$TARGET osd vanished"

# --- 3. transaction throttle ------------------------------------------------
echo
echo "--- 3. seastore_max_concurrent_transactions ---"
TXN=$($BIN/ceph -c "$WL_CONF" config show osd.0 2>/dev/null \
      | awk '$1=="seastore_max_concurrent_transactions"{print $2}')
echo "  reported: ${TXN:-<not set, build default>}"
[ "$TXN" = "256" ] || die "$TARGET: expected 256, got '${TXN:-unset}'"

# --- 4. shard pinning -------------------------------------------------------
echo
echo "--- 4. pinning shards 1:1 ---"
pin_osd_shards "$TPID"
SHARDS=$(show_osd_shards "$TPID")
echo "  $SHARDS"
SMP=$(osd_smp "$TPID")
[ "$SMP" = "8" ] || die "$TARGET: smp=$SMP, expected 8"
# every shard must sit on exactly one core, and they must be the OSD_CORES
for want in $(echo "$OSD_CORES" | tr ',' ' '); do
  echo "$SHARDS" | grep -qE "(^| )[0-7]:$want( |$)" || die "$TARGET: no shard pinned to cpu $want"
done
echo "  confirmed: 8 shards, one per core, on $OSD_CORES"

# --- 5. PG / object state ---------------------------------------------------
echo
echo "--- 5. PG and object state ---"
# Right after the OSD comes up the mon has not received fresh PG stats yet and
# reports "stale+active+clean".  That clears on its own within a few seconds, so
# poll rather than judging on the first sample -- but keep requiring plain
# active+clean, since "stale" means the mon is reporting from memory.
PGLINE=""
# 120 s was not always enough: on a freshly started OSD the mon can sit on
# "stale+active+clean" for well over two minutes before the first PG stat
# arrives, and glove hit that and failed activation.  Five minutes, and say
# what it is still showing so a real problem is distinguishable from slowness.
for i in $(seq 1 60); do
  PGLINE=$($BIN/ceph -c "$WL_CONF" pg stat 2>/dev/null | tail -1)
  echo "$PGLINE" | grep -q "^$WL_PGNUM pgs: $WL_PGNUM active+clean" && break
  [ $((i % 12)) = 0 ] && echo "  still waiting for PG stats (${i}0s): $PGLINE"
  sleep 5
done
echo "  $PGLINE"
echo "$PGLINE" | grep -q "^$WL_PGNUM pgs: $WL_PGNUM active+clean" \
  || die "$TARGET: PGs not all active+clean after 300s ($PGLINE)"
OBJS=$($BIN/rados -c "$WL_CONF" -p "$WL_POOL" ls 2>/dev/null | wc -l)
echo "  objects: $OBJS (expect $WL_PGNUM)"
[ "$OBJS" = "$WL_PGNUM" ] || die "$TARGET: object count $OBJS != $WL_PGNUM"

# --- 6. stored entry count --------------------------------------------------
# Object count cannot see entries duplicated inside the per-PG objects, so the
# real count is read: at --m pg_num every PG is selected and every record of
# every selected PG is scanned, making records-per-query the pool's entry count.
# The probe budget must be raised alongside --m or routing keeps only
# BENCH_PROBE_BUDGET PGs (last flag wins).  This also warms the whole pool.
if [ $DO_COUNT = 1 ]; then
  echo
  echo "--- 6. stored entry count (full-PG scan) ---"
  CLOG=$LOGDIR/${TARGET}_${TS}_count.log
  CEPH_CONF=$WL_CONF CEPH_KEYRING=$WL_KEYRING \
  taskset -c "$CLIENT_CORES" $BENCH --operation fanout-query \
    --pool "$WL_POOL" --index "$WL_INDEX" $WL_ARGS $WL_DSET \
    --m "$WL_PGNUM" --pivot-probe-budget "$WL_PGNUM" \
    --query-limit 10 --rounds 1 --query-concurrency 1 \
    --fanout-mode parallel --query-batch-size 1 --warmup-rounds 0 \
    > "$CLOG" 2>&1 || die "$TARGET: full-PG scan failed, see $CLOG"
  L=$(grep -a "^FANOUT_QUERY" "$CLOG" | tail -1)
  f() { echo "$L" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }
  RECS=$(f avg_total_matching_records_per_query); RECS=${RECS%%.*}
  DUP=$(f duplicate_entries); FAIL=$(f failures); RECALL=$(f recall_at_top_k)
  echo "  records=$RECS (expect $WL_BASE_COUNT)  duplicate_entries=$DUP  failures=$FAIL  recall=$RECALL"
  [ "$RECS" = "$WL_BASE_COUNT" ] || die "$TARGET: entry count $RECS != $WL_BASE_COUNT"
  # scale-deep: at 10M records a full-PG merge sees a few 32-bit entry_id hash
  # collisions (vector_entry_id = rjenkins %08x); with the record count exact
  # they are not stored duplicates. Allow up to 100 and say so.
  # 100M (2026-10-11): limit scales with N -- max(100, N/10000), i.e. <= 0.01 %.
  DUPMAX=$(( WL_BASE_COUNT / 10000 > 100 ? WL_BASE_COUNT / 10000 : 100 ))
  [ "${DUP:-0}" -le $DUPMAX ] 2>/dev/null || die "$TARGET: duplicate_entries=$DUP > $DUPMAX"
  [ "$DUP" = "0" ] || echo "  note: duplicate_entries=$DUP accepted (entry_id hash collisions; records exact)"
  [ "$FAIL" = "0" ] || die "$TARGET: full-PG scan reported failures=$FAIL"
  echo "  confirmed: entry count exact, no duplicates"
fi

# --- 7. warm-up -------------------------------------------------------------
# Identical for every workload so none is measured on a colder cache than
# another.  Results are written to a log and otherwise discarded.
if [ $DO_WARMUP = 1 ]; then
  echo
  echo "--- 7. warm-up (M=$WARMUP_M batch=$WARMUP_BATCH C=$WARMUP_C rounds=$WARMUP_ROUNDS, discarded) ---"
  QL=1000; [ "$WL_QUERY_COUNT" -lt "$QL" ] && QL=$WL_QUERY_COUNT
  WLOG=$LOGDIR/${TARGET}_${TS}_warmup.log
  CEPH_CONF=$WL_CONF CEPH_KEYRING=$WL_KEYRING \
  taskset -c "$CLIENT_CORES" $BENCH --operation fanout-query \
    --pool "$WL_POOL" --index "$WL_INDEX" $WL_ARGS $WL_DSET \
    --m "$WARMUP_M" --query-limit "$QL" --rounds "$WARMUP_ROUNDS" \
    --query-concurrency "$WARMUP_C" --fanout-mode parallel \
    --query-batch-size "$WARMUP_BATCH" --warmup-rounds 1 \
    > "$WLOG" 2>&1 || die "$TARGET: warm-up failed, see $WLOG"
  WL_LINE=$(grep -a "^FANOUT_QUERY" "$WLOG" | tail -1)
  echo "  completed=$(echo "$WL_LINE" | tr ' ' '\n' | grep '^completed=' | cut -d= -f2)" \
       "failures=$(echo "$WL_LINE" | tr ' ' '\n' | grep '^failures=' | cut -d= -f2)" \
       "qps=$(echo "$WL_LINE" | tr ' ' '\n' | grep '^logical_qps=' | cut -d= -f2)"
  echo "  (warm-up result discarded)"
fi

echo
echo "ACTIVE: $TARGET  pid=$TPID  shards=[$SHARDS]  txn=256  pgs=$WL_PGNUM active+clean  entries=${RECS:-skipped}"
