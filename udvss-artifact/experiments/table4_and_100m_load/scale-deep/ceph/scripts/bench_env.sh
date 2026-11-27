# Shared environment for workload-bench.  Sourced by every script here.
#
# Read-only with respect to the loaded clusters: nothing in workload-bench
# loads, writes, deletes or re-creates a pool.  The only operation issued
# against the OSDs is --operation fanout-query.
set -u

BENCH_ROOT=/home/dev_path/experiments/scale-deep/ceph
CONFIG=$BENCH_ROOT/arms.tsv

# Binary and build tree that loaded these pools (workload-prep cards record this
# same commit).  Using anything else would compare a query path against records
# written by a different one.
SRC=/home/dev_path/ceph-vectornode-seastore-batch
BUILD=$SRC/build
BIN=$BUILD/bin
export LD_LIBRARY_PATH=$BUILD/lib
BENCH=$BIN/ceph-vector-bench-native
GIT_COMMIT=$(cd $SRC && git rev-parse HEAD 2>/dev/null || echo unknown)

# Routing geometry, copied from the load commands in the workload-prep cards.
# It must match exactly or the query would build a different pivot model than
# the one the records were placed with.
# scale-deep: per-arm model flags come from the "routing" column of arms.tsv;
# cap) differ per arm and come from the "routing" column of arms.tsv; only the
# flags every arm shares stay here.
ROUTING="--routing-mode pivot --k 10 --l 4 --seed 1315423911 --d 1 --top-k 10"

# Benchmark probe budget.  Routing keeps only --pivot-probe-budget PGs, so this
# has to be >= the largest M in the sweep or the high-M points are silently
# capped.  It is deliberately NOT the 1024 used by the workload-prep M=1024
# sanity check: that was a full-scan contamination test, this is the benchmark.
# BENCH_PROBE_BUDGET is asserted against the M list in run_sweep.sh.
BENCH_PROBE_BUDGET=${BENCH_PROBE_BUDGET:-96}

# 64 GiB per cluster.  32 GiB was tried and fails: seastore replays its journal
# fine but then aborts in SegmentCleaner::allocate_segment("out of space") the
# first time the OSD is restarted, because it sizes segments by free-segment
# ratio rather than by stored bytes.  Seven 64 GiB devices do not fit on
# /home/dev_path alone, so three clusters live on /vstart-extra (a different
# device) -- see workloads.tsv.
SEASTORE_DEVICE_SIZE=${SEASTORE_DEVICE_SIZE:-64G}
# mem48 (2026-10-14): OSD_CG=<cgroup dir> starts crimson-osd inside that cgroup (memory cap);
# OSD_EXTRA_ARGS appends seastar options (e.g. --memory 40G). Both empty = unchanged.

# ---------------------------------------------------------------------------
# CPU placement -- node94 (AMD TR 7960X, 24 physical cores, ONE NUMA node)
#
# This reproduces the placement that produced the 962.8 QPS reference for
# Cohere ll3 on this host: seastar pins its own shards from crimson_cpu_set
# (cores 0-7) and the client gets 8-47.  There is a single NUMA node here, so
# none of the socket/locality questions that occupied the Xeon host apply.
#
# SMT siblings are k and k+24, so 0-7 are eight distinct physical cores and
# 8-47 covers the remaining sixteen physical cores plus every sibling.
OSD_CORES=${OSD_CORES:-0-7}
CLIENT_CORES=${CLIENT_CORES:-8-47}

# Warm-up applied identically to every workload after its OSD starts, so no
# workload is measured on a colder cache than another.  Results are discarded.
WARMUP_M=${WARMUP_M:-16}
WARMUP_BATCH=${WARMUP_BATCH:-1024}
WARMUP_C=${WARMUP_C:-40}
WARMUP_ROUNDS=${WARMUP_ROUNDS:-8}

PIN="taskset -c $CLIENT_CORES"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
die() { echo "FATAL: $*" >&2; exit 1; }

assert_host() {
  # The study moved back to node94 on 2026-09-18.  Both hosts answer to
  # confusingly similar names -- the other one's hostname is literally "ubuntu"
  # and its ssh config entry is "node94" -- so this checks the address, not
  # just the name, and refuses anything else.
  local h; h=$(hostname)
  echo "host      : $h"
  echo "addresses : $(hostname -I)"
  echo "pwd       : $(pwd)"
  [ "$h" = "node94" ] || die "wrong host '$h' -- this must run on node94 / node94"
  hostname -I | grep -qw "${NODE94_IP:?set NODE94_IP}" || die "$NODE94_IP is not an address of this host"
}


# Field lookup from workloads.tsv.  wl_field <name> <column>
wl_field() {
  awk -F'\t' -v want="$1" -v col="$2" '
    /^#/ || NF < 2 { next }
    $1 == "name" { for (i = 1; i <= NF; i++) h[$i] = i; next }
    $1 == want   { if (!(col in h)) exit 3; print $(h[col]); found = 1; exit 0 }
    END { if (!found) exit 4 }
  ' "$CONFIG"
}

wl_names() {
  awk -F'\t' '/^#/ || NF < 2 || $1 == "name" { next } { print $1 }' "$CONFIG"
}

# Populates WL_* for one workload and checks every path it names really exists.
load_workload() {
  WL_NAME=$1
  WL_CLUSTER=$(wl_field "$1" cluster)   || die "no such workload: $1"
  WL_POOL=$(wl_field "$1" pool)
  WL_INDEX=$(wl_field "$1" index)
  WL_BASE=$(wl_field "$1" base)
  WL_QUERY=$(wl_field "$1" query)
  WL_GT=$(wl_field "$1" gt)
  WL_DIM=$(wl_field "$1" dim)
  WL_DTYPE=$(wl_field "$1" dtype)
  WL_METRIC=$(wl_field "$1" metric)
  WL_BASE_COUNT=$(wl_field "$1" base_count)
  WL_QUERY_COUNT=$(wl_field "$1" query_count)
  WL_PGNUM=$(wl_field "$1" pg_num)
  WL_PORT_BASE=$(wl_field "$1" port_base)
  WL_CONF=$WL_CLUSTER/ceph.conf
  WL_KEYRING=$WL_CLUSTER/keyring
  # create_and_load.sh runs before the cluster exists, so it sets
  # ALLOW_MISSING_CLUSTER=1; every other caller still requires it.
  if [ "${ALLOW_MISSING_CLUSTER:-0}" != "1" ]; then
    [ -d "$WL_CLUSTER" ] || die "$1: cluster dir missing: $WL_CLUSTER"
    [ -f "$WL_CONF" ]    || die "$1: ceph.conf missing: $WL_CONF"
  fi
  for f in "$WL_BASE" "$WL_QUERY" "$WL_GT"; do
    [ -s "$f" ] || die "$1: dataset file missing or empty: $f"
  done
  WL_DSET="--base $WL_BASE --query $WL_QUERY --groundtruth $WL_GT"
  WL_ROUTING=$(wl_field "$1" routing | tr ',' ' ')
  WL_ARGS="$ROUTING $WL_ROUTING --pg-num $WL_PGNUM --pivot-probe-budget $BENCH_PROBE_BUDGET"
}

# PIDs actually running the bench binary, by resolving /proc/<pid>/exe.
# A name match cannot be used: "ceph-vector-bench-native" is 24 characters so
# pgrep -x never matches (comm is capped at 15), and pgrep -f matches any shell
# or tmux command line that merely mentions the name.
running_writers() {
  local self=$$ p exe
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [ "$p" = "$self" ] && continue
    exe=$(readlink -f /proc/$p/exe 2>/dev/null) || continue
    [ "$exe" = "$BENCH" ] && echo "$p $(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)"
  done
}

# PID of the crimson-osd serving a given cluster, empty if none.
cluster_osd_pid() {
  local vdest=$1 p
  for p in $(pgrep -x crimson-osd 2>/dev/null); do
    grep -qa "$vdest/ceph.conf" /proc/$p/cmdline 2>/dev/null && { echo "$p"; return 0; }
  done
  return 1
}

cluster_daemon_pid() {  # cluster_daemon_pid <vdest> <ceph-mon|ceph-mgr>
  local vdest=$1 name=$2 p
  for p in $(pgrep -x "$name" 2>/dev/null); do
    grep -qa "$vdest/ceph.conf" /proc/$p/cmdline 2>/dev/null && { echo "$p"; return 0; }
  done
  return 1
}

# Shard count of a crimson-osd, read from its thread names.
#
# Counting "reactor-N" undercounts by one: seastar runs shard 0 on the main
# thread, whose comm stays "crimson-osd", so an --smp 8 OSD shows reactor-1..7.
# The per-shard "syscall-N" helpers are numbered from 0 and so give the real
# count; reactor+1 is used as a cross-check.
osd_smp() {
  local pid=$1 comms sysc react
  comms=$(for t in /proc/$pid/task/*; do cat "$t/comm" 2>/dev/null; done)
  sysc=$(echo "$comms" | grep -c '^syscall-')
  react=$(echo "$comms" | grep -c '^reactor-')
  if [ "$sysc" -gt 0 ]; then echo "$sysc"; else echo $((react + 1)); fi
}

# --- OSD lifecycle ---------------------------------------------------------
# Only ever acts on the OSD whose cmdline names the given cluster's ceph.conf.
# Nothing here touches mon, mgr, or any file on the seastore device.

stop_osd() {   # stop_osd <vdest>  -- returns 0 when no OSD for it is running
  local vdest=$1 pid i t name log segv
  pid=$(cluster_osd_pid "$vdest") || { return 0; }
  name=$(basename "$vdest")

  # Take the OSD out of the mon's view before asking it to stop.  The crash on
  # 2026-09-18 was SeaStore::stat() reaching a shard that had already run
  # TransactionManager::close, reached through the usage report the mon keeps
  # asking for; marking it down first stops that traffic.  Best effort -- if the
  # mon will not take it, the shutdown still proceeds.
  $BIN/ceph -c "$vdest/ceph.conf" osd down 0 > /dev/null 2>&1 || true

  # Release the affinity.  A seastar shutdown runs a handler on every shard, and
  # an OSD still pinned to one core takes minutes to do that and looks like it
  # is ignoring SIGTERM.  Shutdown is not being measured, so give it everything.
  for t in /proc/$pid/task/*; do
    taskset -pc 0-127 "$(basename "$t")" > /dev/null 2>&1
  done

  kill -TERM "$pid" 2>/dev/null || true
  for i in $(seq 1 24); do
    sleep 5
    kill -0 "$pid" 2>/dev/null || { echo "  stopped $name (pid $pid) after $((i*5))s"; return 0; }
  done

  # Still here after 120s.  Two ways that happens, and waiting longer fixes
  # neither, so tell them apart and clear both.
  #
  #   a) a fatal signal fired: SIGTERM is blocked from then on, and the abort
  #      handler is stuck forking addr2line against a 1.5 GB binary.
  #   b) shutdown wedged with no crash: seastore never reaches
  #      "TransactionManager::close: done", the log stops growing, and the
  #      process spins. Seen on both hosts, always with monc mid-handshake.
  #
  # Either way the data is journalled and is re-counted when the workload is
  # next activated, so the process is cleared and the reason recorded.
  log=$vdest/out/osd.0.log
  segv=0; closed=0; s1=0; s2=0
  if [ -f "$log" ]; then
    segv=$(tail -c 2000000 "$log" 2>/dev/null | grep -c "Aborting Got SIG" || true)
    closed=$(tail -c 2000000 "$log" 2>/dev/null | grep -c "TransactionManager::close: done" || true)
    s1=$(stat -c %s "$log" 2>/dev/null || echo 0); sleep 10
    s2=$(stat -c %s "$log" 2>/dev/null || echo 0)
  fi

  if [ "${segv:-0}" -gt 0 ]; then
    echo "  CRASH: $name (pid $pid) hit a fatal signal and is stuck in its abort handler" >&2
    for t in $(pgrep -P "$pid" 2>/dev/null) $(pgrep -f "addr2line.*crimson-osd" 2>/dev/null); do
      kill -9 "$t" 2>/dev/null || true
    done
    for i in $(seq 1 6); do sleep 5; kill -0 "$pid" 2>/dev/null || break; done
    reason="crashed during stop"
  elif [ "${closed:-0}" = "0" ]; then
    echo "  WEDGED: $name (pid $pid) ignored SIGTERM for 120s and seastore never" >&2
    echo "          reached TransactionManager::close; a growing log is not progress," >&2
    echo "          it is the monc retry loop (log $((s2-s1)) bytes in 10s)" >&2
    reason="wedged during stop (no seastore close)"
  else
    echo "  FATAL: $name pid $pid is still making progress after 120s -- not escalating" >&2
    return 1
  fi

  if kill -0 "$pid" 2>/dev/null; then
    echo "  SIGKILL $name (pid $pid)" >&2
    kill -9 "$pid" 2>/dev/null || true
    for i in $(seq 1 12); do sleep 5; kill -0 "$pid" 2>/dev/null || break; done
  fi
  kill -0 "$pid" 2>/dev/null && { echo "  FATAL: $name will not die" >&2; return 1; }
  mkdir -p "$BENCH_ROOT/logs/activate"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $name $reason; entry count re-verified on next activation" \
    >> "$BENCH_ROOT/logs/activate/UNCLEAN_STOPS.txt"
  echo "  cleared; recorded in logs/activate/UNCLEAN_STOPS.txt"
  return 0
}

start_osd() {  # start_osd <vdest> <logfile>  -- starts pinned, waits for "1 up"
  local vdest=$1 log=$2 pid i
  cluster_osd_pid "$vdest" > /dev/null && { echo "  already running"; return 0; }
  # setsid + </dev/null is load-bearing, not tidiness.  Without it the OSD stays
  # a child of this script, and a caller that pipes us into `tee` never sees EOF
  # -- the daemon holds the pipe open forever and the sweep hangs before its
  # first case.  Detaching also means bash does not wait on it at exit.
  setsid bash -c "${OSD_CG:+echo \$\$ > $OSD_CG/cgroup.procs; }ulimit -l unlimited; exec $BIN/crimson-osd -i 0 -c $vdest/ceph.conf -f ${OSD_EXTRA_ARGS:-}" \
    >> "$log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  for i in $(seq 1 60); do
    sleep 5
    pid=$(cluster_osd_pid "$vdest") || continue
    if $BIN/ceph -c "$vdest/ceph.conf" -s 2>/dev/null | grep -qE "osd: 1 osds: 1 up"; then
      sleep 5
      kill -0 "$pid" 2>/dev/null || { echo "  FATAL: osd died right after coming up" >&2; return 1; }
      echo "  started pid $pid, up after $((i*5))s"; return 0
    fi
  done
  echo "  FATAL: osd did not come up in 300s -- see $log" >&2
  return 1
}


# Seastar takes an inherited taskset mask but does not pin each shard inside it,
# so the reactors would float across the set.  Assign shard k to one core:
# shard 0 is the main thread (comm stays "crimson-osd"), 1..7 are "reactor-N".
pin_osd_shards() {   # pin_osd_shards <pid>
  local pid=$1 i=0 t tid comm
  local -a cores
  IFS=',' read -r -a cores <<< "$OSD_CORES"
  for t in /proc/$pid/task/*; do
    tid=$(basename "$t"); comm=$(cat "$t/comm" 2>/dev/null)
    case "$comm" in
      crimson-osd) taskset -pc "${cores[0]}" "$tid" > /dev/null 2>&1 ;;
      reactor-*)   i=${comm#reactor-}
                   [ "$i" -lt "${#cores[@]}" ] && taskset -pc "${cores[$i]}" "$tid" > /dev/null 2>&1 ;;
      syscall-*|iou-wrk-*) taskset -pc "$OSD_CORES" "$tid" > /dev/null 2>&1 ;;
    esac
  done
}

show_osd_shards() {  # show_osd_shards <pid> -- "0:1 1:3 2:5 ..."
  local pid=$1 t tid comm out=""
  for t in /proc/$pid/task/*; do
    tid=$(basename "$t"); comm=$(cat "$t/comm" 2>/dev/null)
    case "$comm" in
      crimson-osd) out="$out 0:$(taskset -pc "$tid" 2>/dev/null | sed 's/.*list: //')" ;;
      reactor-*)   out="$out ${comm#reactor-}:$(taskset -pc "$tid" 2>/dev/null | sed 's/.*list: //')" ;;
    esac
  done
  echo "$out" | tr ' ' '\n' | grep -v '^$' | sort -t: -k1,1n | tr '\n' ' '
}
