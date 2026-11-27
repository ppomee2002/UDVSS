#!/bin/bash
# create_and_load.sh <workload>
#
# Creates this workload's cluster from scratch and loads it exactly once, then
# proves the load with a full-PG entry count.  Mirrors the procedure that was
# verified on the Xeon host, with node94's device size and CPU placement.
#
# Fail-stop throughout.  It refuses to touch a cluster directory that already
# exists, never re-runs a load, and never repairs.
source /home/dev_path/experiments/scale-deep/ceph/scripts/bench_env.sh

WL=${1:?usage: create_and_load.sh <workload>}
ALLOW_MISSING_CLUSTER=1
load_workload "$WL"

LOGDIR=$BENCH_ROOT/logs/load; mkdir -p "$LOGDIR"
TS=$(date -u +%Y%m%d_%H%M%SZ)

echo "=== $WL ==="
echo "cluster : $WL_CLUSTER   device $SEASTORE_DEVICE_SIZE   port $WL_PORT_BASE"
echo "pool    : $WL_POOL"
echo "cores   : OSD $OSD_CORES | client $CLIENT_CORES"

[ -e "$WL_CLUSTER" ] && die "$WL_CLUSTER already exists -- refusing to reuse or delete a cluster"

# --- other OSDs must be down: one OSD at a time -----------------------------
for w in $(wl_names); do
  [ "$w" = "$WL" ] && continue
  v=$(wl_field "$w" cluster)
  cluster_osd_pid "$v" > /dev/null 2>&1 && die "$w's OSD is running; only one OSD may be up"
done

echo "--- vstart ---"
mkdir -p "$WL_CLUSTER"
cd "$BUILD"
VSTART_DEST=$WL_CLUSTER CEPH_CONF=$WL_CONF CEPH_PORT=$WL_PORT_BASE \
  MON=1 OSD=1 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore --crimson-smp 8 \
  --seastore-device-size "$SEASTORE_DEVICE_SIZE" --without-dashboard \
  -o osd_pool_default_size=1 -o osd_pool_default_min_size=1 \
  > "$LOGDIR/${WL}_vstart_${TS}.log" 2>&1 || die "vstart failed, see $LOGDIR/${WL}_vstart_${TS}.log"
echo "  vstart done"

python3 - "$WL_CONF" <<'PY'
import sys
p=sys.argv[1]; out=[]
for l in open(p).read().split('\n'):
    out.append(l)
    if l.strip() in ('[global]','[osd]','[client]','[mon]','[mgr]'):
        out.append('        seastore_experimental_vector_node=true')
        out.append('        seastore_max_concurrent_transactions = 256')
open(p,'w').write('\n'.join(out))
PY
echo "  vector node + txn 256 written into ceph.conf"
# scale-deep: the PG 4096 arm needs more than vstart's mon_max_pg_per_osd=1000
# (runtime option; harmless for the 1024-PG arms).
# The first attempt (16:01Z) was silently not applied (pool create then saw 100),
# so retry until the mon reports it.
for i in $(seq 1 30); do
  $BIN/ceph -c "$WL_CONF" config set global mon_max_pg_per_osd 16384 2>/dev/null
  # the mon read 1000 from ceph.conf at startup and a conf-file value wins over
  # the config DB, so also inject it into the running mon
  $BIN/ceph -c "$WL_CONF" tell mon.a config set mon_max_pg_per_osd 16384 >/dev/null 2>&1
  [ "$($BIN/ceph -c "$WL_CONF" config get mon mon_max_pg_per_osd 2>/dev/null)" = 16384 ] && break
  sleep 5
done
[ "$($BIN/ceph -c "$WL_CONF" config get mon mon_max_pg_per_osd 2>/dev/null)" = 16384 ] || die "mon_max_pg_per_osd not applied"
sed -i 's/mon_max_pg_per_osd = 1000/mon_max_pg_per_osd = 16384/' "$WL_CONF"
echo "  mon_max_pg_per_osd=16384"

# seastar pins its own shards from this; it is a startup-only option, and the
# OSD below is started after it is set.
$BIN/ceph -c "$WL_CONF" config set osd.0 crimson_cpu_set "$OSD_CORES" 2>/dev/null
echo "  crimson_cpu_set=$($BIN/ceph -c "$WL_CONF" config dump 2>/dev/null | awk '$3=="crimson_cpu_set"{print $4}')"
# mem48: CRIMSON_MEMORY=<size> sets the seastar allocator size (crimson_memory -> --memory);
# unset = seastar default (all memory visible to it, i.e. the cgroup limit, minus a reserve).
if [ -n "${CRIMSON_MEMORY:-}" ]; then
  $BIN/ceph -c "$WL_CONF" config set osd.0 crimson_memory "$CRIMSON_MEMORY" 2>/dev/null
  echo "  crimson_memory=$($BIN/ceph -c "$WL_CONF" config get osd.0 crimson_memory 2>/dev/null)"
fi

stop_osd "$WL_CLUSTER"
echo "--- start osd ---"
cd "$BUILD"
setsid bash -c "${OSD_CG:+echo \$\$ > $OSD_CG/cgroup.procs; }ulimit -l unlimited; exec $BIN/crimson-osd -i 0 -c $WL_CONF -f ${OSD_EXTRA_ARGS:-}" \
  >> "$LOGDIR/${WL}_osd_stdout.log" 2>&1 < /dev/null &
for i in $(seq 1 60); do
  sleep 5
  cluster_osd_pid "$WL_CLUSTER" > /dev/null 2>&1 || continue
  $BIN/ceph -c "$WL_CONF" -s 2>/dev/null | grep -qE "osd: 1 osds: 1 up" && break
done
P=$(cluster_osd_pid "$WL_CLUSTER") || die "osd did not come up"
echo "  pid=$P  shards=$(show_osd_shards "$P")"
grep -ah "get_early_config" "$LOGDIR/${WL}_osd_stdout.log" | tail -1 | sed 's/^/  /'

$BIN/ceph -c "$WL_CONF" osd pool create "$WL_POOL" "$WL_PGNUM" "$WL_PGNUM" 2>&1 | tail -1
for i in $(seq 1 720); do   # 12288 PGs: the mon creates them in throttled batches (>10 min)
  PG=$($BIN/ceph -c "$WL_CONF" pg stat 2>/dev/null | tail -1)
  echo "$PG" | grep -q "$WL_PGNUM pgs: $WL_PGNUM active+clean" && break
  sleep 5
done
echo "  $PG"
echo "$PG" | grep -q "$WL_PGNUM pgs: $WL_PGNUM active+clean" || die "pool $WL_POOL not created/clean: $PG"

# --- the single load --------------------------------------------------------
# The bench tool finds the cluster through these, not through --conf.
export CEPH_CONF=$WL_CONF CEPH_KEYRING=$WL_KEYRING
OBJS=$($BIN/rados -c "$WL_CONF" -p "$WL_POOL" ls 2>/dev/null | wc -l)
[ "$OBJS" = "0" ] || die "$WL_POOL is not empty ($OBJS objects)"
W=$(running_writers); [ -n "$W" ] && { echo "$W"; die "another writer is running"; }
LOADLOG=$LOGDIR/${WL}_load_${TS}.log
CMD="$PIN $BENCH --operation load --pool $WL_POOL --index $WL_INDEX $WL_ARGS $WL_DSET --load-concurrency ${LOAD_CONC:-128}"
echo "--- LOAD (once) ---"
echo "COMMAND: $CMD" > "$LOADLOG"
S=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$S" >> "$LOADLOG"
eval "$CMD" >> "$LOADLOG" 2>&1; RC=$?
E=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$E" >> "$LOADLOG"
grep -aE "^PIVOT_MODEL|^LOAD |^PUT_OID_EXAMPLE" "$LOADLOG" | sed 's/^/  /'
[ $RC = 0 ] || die "load exited $RC"
f() { grep -aoE "$1=[^ ]+" "$LOADLOG" | tail -1 | cut -d= -f2; }
[ "$(f completed)" = "$WL_BASE_COUNT" ] || die "completed=$(f completed) expected $WL_BASE_COUNT"
[ "$(f failures)" = "0" ] || die "failures=$(f failures)"
echo "  load ok: $S -> $E"

# --- prove it ---------------------------------------------------------------
echo "--- full-PG entry count ---"
CL=$LOGDIR/${WL}_count_${TS}.log
$PIN $BENCH --operation fanout-query --pool "$WL_POOL" --index "$WL_INDEX" $WL_ARGS $WL_DSET \
  --m "$WL_PGNUM" --pivot-probe-budget "$WL_PGNUM" --query-limit 10 --rounds 1 \
  --query-concurrency 1 --fanout-mode parallel --query-batch-size 1 --warmup-rounds 0 \
  > "$CL" 2>&1 || die "full-PG scan failed"
L=$(grep -a "^FANOUT_QUERY" "$CL" | tail -1)
g() { echo "$L" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }
R=$(g avg_total_matching_records_per_query); R=${R%%.*}
echo "  records=$R (expect $WL_BASE_COUNT)  duplicate_entries=$(g duplicate_entries)  recall=$(g recall_at_top_k)  failures=$(g failures)"
[ "$R" = "$WL_BASE_COUNT" ] || die "entry count $R != $WL_BASE_COUNT"
# entry_id is a 32-bit rjenkins hash: a full-PG merge sees a few collisions
# (10M: 7 at PG4096, 30 at PG16384). With the record count exact they are not
# stored duplicates -- accept up to max(100, N/10000) (2026-10-11, 100M arm).
DUPMAX=$(( WL_BASE_COUNT / 10000 > 100 ? WL_BASE_COUNT / 10000 : 100 ))
[ "$(g duplicate_entries)" -le $DUPMAX ] 2>/dev/null || die "duplicate_entries=$(g duplicate_entries) > $DUPMAX"
echo "LOADED_OK $WL"
