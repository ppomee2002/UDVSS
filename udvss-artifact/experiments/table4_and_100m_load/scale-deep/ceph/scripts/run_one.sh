#!/bin/bash
# run_one.sh <workload> <M> <batch> <concurrency> <repeat_tag> [--query-limit N] [--rounds N] [--dry-run]
#
# Exactly one ceph-vector-bench-native --operation fanout-query invocation.
# It issues no other RADOS operation, and in particular never loads.
#
# Records EVERY field the tool emits on its FANOUT_QUERY line (32 of them),
# plus the pivot model's digest and build time, plus measured OSD and client CPU
# over the query phase.  Capturing the whole line rather than a hand-picked
# subset means a question asked later -- p99, queue wait, inflight, distance
# computations, missing objects -- can be answered from the rows already on disk
# instead of re-running the sweep.
source /home/dev_path/experiments/scale-deep/ceph/scripts/bench_env.sh
# scale-deep (2026-10-10): every measurement runs >= MIN_SECONDS of
# queries (time-bound, after one discarded warm-up round), not a fixed count.
MIN_SECONDS=${MIN_SECONDS:-120}

[ $# -ge 5 ] || die "usage: run_one.sh <workload> <M> <batch> <concurrency> <repeat_tag> [--query-limit N] [--rounds N] [--dry-run]"
WL=$1; M=$2; BATCH=$3; CONC=$4; REP=$5; shift 5

QUERY_LIMIT=""; ROUNDS=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --query-limit) QUERY_LIMIT=$2; shift 2 ;;
    --rounds)      ROUNDS=$2; shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done

load_workload "$WL"

[ "$M" -le "$BENCH_PROBE_BUDGET" ] || \
  die "M=$M exceeds --pivot-probe-budget $BENCH_PROBE_BUDGET; routing would keep only $BENCH_PROBE_BUDGET PGs"

if [ -z "$QUERY_LIMIT" ]; then
  QUERY_LIMIT=1000
  [ "$WL_QUERY_COUNT" -lt "$QUERY_LIMIT" ] && QUERY_LIMIT=$WL_QUERY_COUNT
fi
if [ -z "$ROUNDS" ]; then
  ROUNDS=$(( (CONC * BATCH * 4 + QUERY_LIMIT - 1) / QUERY_LIMIT ))
  [ "$ROUNDS" -lt 4 ] && ROUNDS=4
fi

OUTDIR=$BENCH_ROOT/results/$WL
LOGDIR=$BENCH_ROOT/logs/$WL
mkdir -p "$OUTDIR" "$LOGDIR"
CASE="m${M}_b${BATCH}_c${CONC}_r${REP}"
LOG=$LOGDIR/${CASE}.log
CMDF=$LOGDIR/${CASE}.cmd
ROW=$OUTDIR/${CASE}.row

CMD="$PIN $BENCH --operation fanout-query \
--pool $WL_POOL --index $WL_INDEX $WL_ARGS $WL_DSET \
--m $M --query-limit $QUERY_LIMIT --rounds $ROUNDS --query-concurrency $CONC \
--fanout-mode parallel --query-batch-size $BATCH --warmup-rounds 1 \
--min-queries $QUERY_LIMIT --min-seconds $MIN_SECONDS"

if [ $DRY = 1 ]; then
  echo "### $WL m=$M batch=$BATCH conc=$CONC rep=$REP (query_limit=$QUERY_LIMIT rounds=$ROUNDS)"
  echo "CEPH_CONF=$WL_CONF CEPH_KEYRING=$WL_KEYRING LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
  echo "$CMD"; echo
  exit 0
fi

W=$(running_writers)
[ -n "$W" ] && { echo "$W"; die "a ceph-vector-bench-native process is already running"; }

export CEPH_CONF=$WL_CONF CEPH_KEYRING=$WL_KEYRING
{ echo "CEPH_CONF=$WL_CONF"; echo "CEPH_KEYRING=$WL_KEYRING"
  echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"; echo "$CMD"; } > "$CMDF"

# CPU accounting: sample the OSD and the client across the run.  The client's
# pivot-model build happens before any query is issued, so the OSD share is
# quoted over the query phase only -- start the window once the OSD is actually
# doing something rather than at t=0.
HZ=$(getconf CLK_TCK)
OSDPID=$(cluster_osd_pid "$WL_CLUSTER" 2>/dev/null || echo "")
osd_j() { [ -n "$OSDPID" ] && awk '{print $14+$15}' /proc/$OSDPID/stat 2>/dev/null || echo 0; }
proc_j() { awk '{print $14+$15}' /proc/$1/stat 2>/dev/null || echo 0; }

START=$(date -u +%Y-%m-%dT%H:%M:%SZ); T0=$(date +%s.%N)
eval "$CMD" > "$LOG" 2>&1 &
CPID=$!
# wait for the query phase: the OSD starts burning CPU once queries arrive
OJ0=0; CJ0=0; TW0=0
for i in $(seq 1 240); do
  sleep 2
  kill -0 $CPID 2>/dev/null || break
  cur=$(osd_j)
  if [ "$cur" -gt 0 ] && [ -z "${MARKED:-}" ]; then
    sleep 3
    a=$(osd_j); sleep 2; b=$(osd_j)
    if [ $((b - a)) -gt $((HZ / 2)) ]; then      # >0.25 core: queries are flowing
      OJ0=$(osd_j); CJ0=$(proc_j $CPID); TW0=$(date +%s.%N); MARKED=1
      break
    fi
  fi
done
wait $CPID; RC=$?
OJ1=$(osd_j); CJ1=$(proc_j $CPID 2>/dev/null || echo "$CJ0"); TW1=$(date +%s.%N)
T1=$(date +%s.%N); END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ELAPSED=$(awk -v a="$T0" -v b="$T1" 'BEGIN{printf "%.3f", b-a}')
CPUW=$(awk -v a="$TW0" -v b="$TW1" 'BEGIN{d=b-a; printf "%.2f", (d>0?d:0)}')
cores() { awk -v d="$1" -v hz="$HZ" -v w="$CPUW" 'BEGIN{ if (w>0) printf "%.3f", d/hz/w; else printf "NA" }'; }
OSD_CORES_USED=$(cores $((OJ1 - OJ0)))
CLI_CORES_USED=$(cores $((CJ1 - CJ0)))

# The warmup round prints its own FANOUT_QUERY line first, so take the last.
FQ=$(grep -a "^FANOUT_QUERY" "$LOG" | tail -1)
PM=$(grep -a "^PIVOT_MODEL"  "$LOG" | tail -1)
PR=$(grep -a "^PIVOT_REFINE" "$LOG" | tail -1)
fq() { echo "$FQ" | tr ' ' '\n' | grep "^$1=" | tail -1 | cut -d= -f2; }
pm() { echo "$PM" | tr ' ' '\n' | grep "^$1=" | tail -1 | cut -d= -f2; }
pr() { echo "$PR" | tr ' ' '\n' | grep "^$1=" | tail -1 | cut -d= -f2; }

# every field the tool emits, in the order it emits them
FQ_KEYS="mode probe_pg_count subrequests_per_logical_query completed failures first_error \
missing_objects duplicate_entries seconds logical_qps logical_latency_us_avg logical_latency_us_p50 \
logical_latency_us_p95 logical_latency_us_p99 avg_matching_records_per_pg avg_distance_computations_per_pg \
avg_total_matching_records_per_query avg_total_distance_computations_per_query recall_at_top_k \
rerank_top_n rerank_unmapped avg_returned_results query_batch_size osd_requests osd_query_ops \
ops_per_request osd_requests_per_query io_depth avg_queue_wait_us avg_active_us avg_inflight max_inflight"

STATUS=OK
[ $RC = 0 ] || STATUS=FAILED
[ -n "$(fq recall_at_top_k)" ] || STATUS=FAILED
[ "$(fq failures)" = "0" ] || STATUS=FAILED

# row: context, then every FANOUT_QUERY field, then pivot model and CPU
{
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' "$WL" "$M" "$BATCH" "$CONC" "$REP" "$QUERY_LIMIT" "$ROUNDS"
  for k in $FQ_KEYS; do printf '\t%s' "$(v=$(fq "$k"); echo "${v:-NA}")"; done
  printf '\t%s\t%s\t%s\t%s\t%s' \
    "$(v=$(pm digest); echo "${v:-NA}")" "$(v=$(pm build_seconds); echo "${v:-NA}")" \
    "$(v=$(pm representative_count); echo "${v:-NA}")" \
    "$(v=$(pr sse); echo "${v:-NA}")" "$(v=$(pr max_cluster); echo "${v:-NA}")"
  printf '\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$OSD_CORES_USED" "$CLI_CORES_USED" "$CPUW" "$ELAPSED" "$START" "$END" "$GIT_COMMIT"
  } | tr -d '\000' > "$ROW.tmp"
printf '%s' "$(cat "$ROW.tmp")" > "$ROW"; printf '\t%s\n' "$STATUS" >> "$ROW"; rm -f "$ROW.tmp"

echo "$WL m=$M b=$BATCH c=$CONC rep=$REP -> $STATUS qps=$(fq logical_qps) recall=$(fq recall_at_top_k) cand=$(fq avg_total_matching_records_per_query) p50=$(fq logical_latency_us_p50) p99=$(fq logical_latency_us_p99) osd_cpu=$OSD_CORES_USED failures=$(fq failures) elapsed=${ELAPSED}s"
echo "  log=$LOG"
[ "$STATUS" = OK ] || echo "  (rc=$RC; left as FAILED, not retried)"
[ "$STATUS" = OK ]
