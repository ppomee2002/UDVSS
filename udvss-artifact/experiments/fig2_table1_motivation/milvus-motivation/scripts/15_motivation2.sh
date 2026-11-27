#!/usr/bin/env bash
# Motivation 2 -- per-query latency decomposition, and the index-read /
# vector-read split. See docs/04_motivation2_plan.md.
#
# Everything runs at NQ=1, concurrency 1, and writes rows labelled M2_*.
# It never touches existing runs.csv rows or results/report*/.
#
#   ./15_motivation2.sh check       # is it safe to run right now?
#   ./15_motivation2.sh ladders     # G4 baseline + G1 N ladder + G2 M ladder (trace OFF)
#   ./15_motivation2.sh traces      # same points with OTel on (proportions)
#   ./15_motivation2.sh outfields   # G3-A  output_fields A/B
#   ./15_motivation2.sh nprobe      # G3-B  IVF_FLAT nprobe regression
#   ./15_motivation2.sh layerab     # L0/L1/L2 placement A/B  (docs/06_layer_ab_plan.md)
#   ./15_motivation2.sh layerabtrace   # same three arms with OTel on
#   ./15_motivation2.sh layerab-restore  # put the cluster back on L0 after a crash
#   ./15_motivation2.sh all
set -uo pipefail
: "${MM_ROOT:?source env.sh and env.local.sh first}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

PY="$MM_VENV/bin/python"
VALUES="${VALUES:-$HOME/milvus-3node.experiment.yaml}"
TRACE_VALUES="${TRACE_VALUES:-$HOME/milvus-3node.trace.yaml}"
CONC=1                       # M2 is about ONE query's path. Do not raise this.
QUERIES="${QUERIES:-3000}"
WARMUP="${WARMUP:-500}"
TRACE_QUERIES="${TRACE_QUERIES:-600}"   # span export is expensive; keep traced runs short
TRACE_LIMIT="${TRACE_LIMIT:-300}"       # traces pulled out of Jaeger per condition
CHUNKS="${CHUNKS:-4}"
OUT="$MM_RESULTS/report_m2"
# M2 rows go to their OWN csv, so the existing runs.csv is never rewritten.
RUNS="$MM_RESULTS/runs_m2.csv"
mkdir -p "$OUT" "$MM_RESULTS/trace"

say () { printf '\n\033[1m%s\033[0m\n' "$*"; }
die () { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
# Jaeger all-in-one stores traces in RAM. A trace at S=16 carries hundreds of
# spans, and 600 of them OOMKilled the 8Gi pod on 2026-09-21, silently losing
# S16_Q1 and S16_Q4. Scale the traced run down with S; percentages converge long
# before 600 queries anyway.
trace_sizing () {   # $1 = S -> sets TQ, TL, TW
  if   [ "$1" -ge 16 ]; then TQ=$(( TRACE_QUERIES / 2 )); TL=$(( TRACE_LIMIT / 2 )); TW=800
  elif [ "$1" -ge 4  ]; then TQ=$(( TRACE_QUERIES / 2 )); TL=$TRACE_LIMIT;           TW=400
  else                       TQ=$TRACE_QUERIES;           TL=$TRACE_LIMIT;           TW=100
  fi
  [ "$TQ" -lt 200 ] && TQ=200
  [ "$TL" -lt 60 ]  && TL=60
}

# A Search issued while the collection is still loading returns in microseconds
# with an error. Those are real Search spans, so no operation filter removes
# them; at S=16 Q=1 they were 26 of 60 traces and dragged the reported mean to
# 1859us against a client-measured 4874us. Block until queries are actually
# being served before anything is traced.
wait_queryable () {   # $1 = collection
  local i
  for i in $(seq 1 60); do
    if $PY - "$1" <<'PYX' >/dev/null 2>&1
import os, sys, numpy as np
from pymilvus import MilvusClient
c = MilvusClient(uri=os.environ["MM_URI"])
v = np.random.rand(96).astype("float32").tolist()
for _ in range(20):
    c.search(sys.argv[1], data=[v], limit=10, anns_field="vector",
             search_params={"ef": 64})
PYX
    then echo "  $1 queryable after ${i}0s"; return 0; fi
    sleep 10
  done
  echo "  !! $1 never became reliably queryable"; return 1
}

# Resolve Jaeger's address fresh, and only from a pod that is actually Ready.
# A plain `get pod -l app=jaeger ... .items[0]` returns the TERMINATING pod right
# after a restart -- that handed us a dead 10.42.0.11 and every trace pull for
# S=16 died with "No route to host" while the benches ran fine.
jaeger_ip () {
  local ip
  ip=$(kubectl -n "$MM_NS" get pod -l app=jaeger \
       -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.deletionTimestamp}{"|"}{.status.podIP}{"|"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' 2>/dev/null \
       | awk -F'|' '$1=="" && $3=="True" && $2!="" {print $2; exit}')
  [ -n "$ip" ] || return 1
  printf 'http://%s:16686' "$ip"
}

# Bound Jaeger's in-memory ring. Two failure modes bracket this value:
#
#   too large (the 200000 default): a 600-query traced run at S=16, where one
#       trace carries hundreds of spans, OOMKilled the 8Gi pod (exitCode 137).
#   too small (2000, which I set to fix the above): the streamingnode alone emits
#       `run_sync` ~165 times a MINUTE, so the ring turns over in ~12 minutes and
#       evicts the Search traces before they can be pulled. Measured: 300 queries
#       -> 0 Search traces at 2000, 300 at 200000. This silently lost 5 trace
#       collections.
#
# 50000 sits between them: ~5 hours of background chatter, and a quarter of the
# memory of the setting that OOMed -- with trace_sizing() now shrinking the traced
# run as S grows, the S=16 case is well under what killed it.
jaeger_bound () {   # $1 = S, $2 = Q (optional; defaults to the small-trace case)
  # The ring has to be sized by SPANS, not traces. A trace at S=1 Q=1 carries a
  # few dozen spans; at S=16 Q=4 it carries hundreds (16 shards x 4 workers, each
  # with its own segcore subtree). A flat 50000 OOMKilled the 8Gi pod during
  # INGEST at S=16 Q=4 (exitCode 137, three restarts) and every pull then returned
  # 0 traces, which looked like a tracing-config problem and cost several reruns.
  # Scale it down as S*Q grows; the floor still holds far more than the 150-300
  # traces a condition needs.
  local sq=$(( ${1:-1} * ${2:-1} ))
  local want=$(( 200000 / (sq < 1 ? 1 : sq) ))
  [ "$want" -gt 50000 ] && want=50000
  [ "$want" -lt 2000 ]  && want=2000
  local cur
  cur=$(kubectl -n "$MM_NS" get deploy jaeger \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MEMORY_MAX_TRACES")].value}' 2>/dev/null)
  if [ "$cur" != "$want" ]; then
    echo "  jaeger MEMORY_MAX_TRACES -> $want (S*Q=$sq, was '${cur:-unset}')"
    kubectl -n "$MM_NS" set env deploy/jaeger "MEMORY_MAX_TRACES=$want" >/dev/null
    kubectl -n "$MM_NS" rollout status deploy/jaeger --timeout=180s | tail -1
    sleep 10
  else
    echo "  jaeger MEMORY_MAX_TRACES already $want"
  fi
}

# ---------------------------------------------------------------- guards
#
#   traffic (Motivation 1)  SAFE.  podnet.py reads /proc/net/dev inside each
#       Milvus pod's own network namespace, so a neighbour's bytes can never land
#       in those counters.
#   latency (Motivation 2)  FULLY EXPOSED.  CPU, memory bandwidth and the NIC are
#       shared, and the bench client, Proxy, DataNode and MixCoord all sit on
#       milvus-proxy94.
COTENANTS=""

cotenant_scan () {
  local found="" n ports pods
  COTENANTS=""
  n=$(pgrep -c -x crimson-osd 2>/dev/null || true); n=${n:-0}
  [ "$n" -gt 0 ] && found="$found ceph:crimson-osd=$n"
  for proc in qdrant vespa-proton-bin configserver; do
    n=$(pgrep -c -f "$proc" 2>/dev/null || true); n=${n:-0}
    [ "$n" -gt 0 ] && found="$found proc:$proc=$n"
  done
  n=$(pgrep -c -f "milvus run standalone" 2>/dev/null || true); n=${n:-0}
  [ "$n" -gt 0 ] && found="$found foreign-milvus:standalone=$n"
  ports=$(ss -ltnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' \
          | grep -E '^(6333|6334|19071|19050)$' | sort -u | tr '\n' ',' | sed 's/,$//')
  [ -n "$ports" ] && found="$found listen:$ports"
  pods=$(kubectl get pods -A -o json 2>/dev/null | $PY -c '
import json,sys
want={"milvus-proxy94","milvus-stream110","milvus-query109"}
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
bad=[]
for i in d.get("items",[]):
    sp,st=i.get("spec",{}),i.get("status",{})
    if sp.get("nodeName") not in want: continue
    ns=i["metadata"]["namespace"]
    if ns in ("milvus","kube-system"): continue
    if st.get("phase") not in ("Running","Pending"): continue
    bad.append(ns+"/"+i["metadata"]["name"])
print(",".join(sorted(bad)[:8]))
' 2>/dev/null)
  [ -n "$pods" ] && found="$found pods:$pods"
  COTENANTS="${found# }"
}

BUSY_CORES=""
busy_cores () {
  local a b at bt ai bi
  # idle counts idle + iowait: iowait is a core parked on an outstanding I/O, not
  # a core burning cycles. Counting it as busy reported 7.9 cores on a box whose
  # only neighbour measured 0.04 cores over two minutes.
  a=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $5+$6}' /proc/stat)
  sleep "${BUSY_WINDOW:-5}"
  b=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $5+$6}' /proc/stat)
  at=${a% *}; ai=${a#* }; bt=${b% *}; bi=${b#* }
  BUSY_CORES=$(awk -v at="$at" -v ai="$ai" -v bt="$bt" -v bi="$bi" -v n="$(nproc)" \
    'BEGIN{ d=bt-at; if(d<=0){print "0.0"; exit} v=n*(1-(bi-ai)/d); if(v<0)v=0; printf "%.1f", v }')
}

# `ps -eo pcpu` is a whole-lifetime average, not a rate: it showed a neighbour at
# 700% while a two-minute sample had it flat at 0.04 cores. Take a real delta.
top_consumers () {
  top -b -n 2 -d "${TOPC_WINDOW:-3}" 2>/dev/null \
    | awk '/^ *PID +USER/{n++} n==2 && $1 ~ /^[0-9]+$/ && $9+0 > 5 {printf "%s(%.0f%%) ", $12, $9; c++}
           END{ if(!c) printf "none >5%%" }'
}

guard () {
  local load over
  load=$(awk '{print $1}' /proc/loadavg)
  cotenant_scan
  busy_cores
  echo "  load average (1m) : $load   (of $(nproc) cores)"
  echo "  busy cores (meas.): $BUSY_CORES   (limit ${BUSY_LIMIT:-2.0})"
  echo "  neighbours present: ${COTENANTS:-none}"
  echo "  top consumers     : $(top_consumers)"
  # Presence is not the test: the neighbours here are pinned to cpuset 0-7 and sit
  # near idle. What matters is CPU actually being burned now.
  over=$(awk -v b="$BUSY_CORES" -v t="${BUSY_LIMIT:-2.0}" 'BEGIN{print (b+0>t)?1:0}')
  if [ "$over" = 1 ]; then
    die "REFUSING: $BUSY_CORES cores busy (limit ${BUSY_LIMIT:-2.0}).
    neighbours    : ${COTENANTS:-none}
    top consumers : $(top_consumers)

  Motivation 2 measures latency itself and the bench client, Proxy, DataNode and
  MixCoord all live on this machine. Do NOT kill anything you do not own; wait, or
  set M2_FORCE=1 to record anyway (rows get stamped POLLUTED in notes)."
  fi
  echo "  OK to run."
}

if [ "${M2_FORCE:-0}" = "1" ]; then
  guard () {
    cotenant_scan
    busy_cores
    echo "  !! M2_FORCE=1 -- running anyway."
    echo "     busy cores: $BUSY_CORES   neighbours: ${COTENANTS:-none}"
    return 0
  }
fi

refresh_uri () {
  local pip
  pip=$(kubectl -n "$MM_NS" get pod -l component=proxy -o jsonpath='{.items[0].status.podIP}')
  [ -n "$pip" ] || die "no proxy pod IP"
  sed -i "s|^export MM_URI=.*|export MM_URI=http://$pip:19530|" "$MM_ROOT/env.local.sh"
  export MM_URI="http://$pip:19530"
  echo "  MM_URI=$MM_URI"
}

wait_pods () {   # $1 = expected querynode count
  local want="$1" tot rdy qn
  for _ in $(seq 1 60); do
    tot=$(kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=milvus --no-headers 2>/dev/null | wc -l)
    rdy=$(kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=milvus --no-headers 2>/dev/null \
          | awk '{split($2,a,"/"); if(a[1]==a[2] && $3=="Running") c++} END{print c+0}')
    qn=$(kubectl -n "$MM_NS" get pods -l component=querynode --no-headers 2>/dev/null | wc -l)
    [ "$tot" -gt 0 ] && [ "$rdy" = "$tot" ] && [ "$qn" = "$want" ] && { echo "  pods Ready $rdy/$tot, querynode=$qn"; return 0; }
    sleep 10
  done
  echo "  !! pods did not settle (Ready $rdy/$tot, querynode=$qn, wanted $want)"; return 1
}

wait_quiet () {  # a baseline taken during WAL replay over-subtracts an entire run
  local sn a b kbs
  for _ in $(seq 1 30); do
    sn=$(kubectl -n "$MM_NS" get pod -l component=streamingnode -o jsonpath='{.items[0].metadata.name}')
    a=$(kubectl -n "$MM_NS" exec "$sn" -- sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    sleep 10
    b=$(kubectl -n "$MM_NS" exec "$sn" -- sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    kbs=$(( (b - a) / 10 / 1024 ))
    [ "$kbs" -lt 40 ] && { echo "  streamingnode idle ~${kbs} KB/s -- quiet"; return 0; }
  done
  echo "  !! never went quiet; the network columns of this block are suspect"
}

scale_q () {     # $1 = wanted QueryNode pods; $2 = values file
  local q="$1" vf="$2"
  # down to 0 first: a rolling update deadlocks under the hard podAntiAffinity
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" \
       -f "$vf" --set queryNode.replicas=0 2>&1 | grep -E "^REVISION|Error" || true
  for _ in $(seq 1 40); do
    [ "$(kubectl -n "$MM_NS" get pods -l component=querynode --no-headers 2>/dev/null | wc -l)" = 0 ] && break
    sleep 10
  done
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" \
       -f "$vf" --set queryNode.replicas="$q" 2>&1 | grep -E "^REVISION|Error" || true
  wait_pods "$q" || return 1
  refresh_uri
}

ensure_coll () { # $1 name  $2 shards  $3 chunks  [$4 index  $5 index-params]
  local name="$1" s="$2" c="$3" idx="${4:-$MM_INDEX}" ip="${5:-$MM_INDEX_PARAMS}"
  if ! MM_URI="$MM_URI" $PY - "$name" <<'PYX' >/dev/null 2>&1
import os, sys
from pymilvus import MilvusClient
sys.exit(0 if MilvusClient(uri=os.environ["MM_URI"]).has_collection(sys.argv[1]) else 1)
PYX
  then
    echo "  [load] $name (S=$s chunks=$c index=$idx) -- creating, this takes a while"
    $PY "$MM_ROOT/scripts/06_load_collection.py" --collection "$name" \
        --shards "$s" --chunks "$c" --replicas 1 \
        --index "$idx" --index-params "$ip" 2>&1 | tail -3
  fi
  $PY "$MM_ROOT/scripts/11_redistribute.py" --collections "$name" 2>&1 | grep -E "nodes=|FAIL|waiting"
}

# one measured row. $1 label  $2 collection  $3 S  $4 Q  $5 chunks  [$6..] extra bench args
#
# MEASURED 2026-09-21 on this box, same point back to back, box idle:
#     r1 3970.2us   r2 2866.6us   r3 2937.5us      <- r1 is +38% over r2
# r2/r3 match the 2026-09-20 archive (2919us) within 2%; r1 does not. A single
# run is therefore NOT a measurement here -- the first touch after an idle period
# is cold, and even after that the spread is wide enough to invent an effect the
# size of the ones M2 is looking for.
#
# So every point is: one DISCARDED prewarm pass, then $REPEATS real passes
# recorded as <label>_r1..<label>_rN. The report takes the MEDIAN and prints the
# coefficient of variation; a point with CV above $CV_LIMIT% is flagged, not used.
REPEATS="${REPEATS:-3}"
CV_LIMIT="${CV_LIMIT:-5}"
PREWARM_QUERIES="${PREWARM_QUERIES:-1000}"

row () {
  local label="$1" coll="$2" s="$3" q="$4" c="$5"; shift 5
  local args=() note="" skip=0 a before="$COTENANTS"
  # re-scan per row: a stage runs for 30-50 minutes and a neighbour can come up
  # in the middle of it. A single check at stage start would silently bless every
  # row after the one where they landed.
  cotenant_scan
  busy_cores
  local names="$COTENANTS"
  # POLLUTED is keyed on measured CPU, not on a neighbour merely existing:
  # the neighbours here are pinned to cpuset 0-7 and idle most of the time, so
  # stamping on presence would mark every single row and tell us nothing.
  COTENANTS=""
  awk -v b="$BUSY_CORES" -v t="${BUSY_LIMIT:-2.0}" 'BEGIN{exit !(b+0>t)}' \
    && COTENANTS="busy=${BUSY_CORES}cores${names:+ (${names})}"
  [ -n "$COTENANTS" ] && \
    printf '\033[31m  !! box busy during this point: %s\033[0m\n' "$COTENANTS"

  for a in "$@"; do
    if [ "$skip" = 1 ]; then note="$a"; skip=0; continue; fi
    if [ "$a" = "--notes" ]; then skip=1; continue; fi
    args+=("$a")
  done
  [ -n "$COTENANTS" ] && note="POLLUTED cotenants=$COTENANTS${note:+ | $note}"
  [ -n "$note" ] && args+=(--notes "$note")

  say "---- $label${COTENANTS:+   [POLLUTED: $COTENANTS]}"
  $PY "$MM_ROOT/scripts/07_probe_snm.py" --collection "$coll" --queries 200 2>&1 \
      | grep -E "S  shards touched|N  QueryNodes PER SHARD|M  segments PER QUERYNODE"

  # discarded prewarm: faults the index/data pages back in after an idle gap
  echo "  prewarm ($PREWARM_QUERIES queries, discarded) ..."
  $PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "PREWARM_$label" \
      --shards "$s" --querynode-pods "$q" --insert-chunks "$c" --concurrency "$CONC" \
      --warmup 200 --queries "$PREWARM_QUERIES" --runs-csv /dev/null \
      "${args[@]}" >/dev/null 2>&1

  local i means=()
  for i in $(seq 1 "$REPEATS"); do
    local out
    out=$($PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "${label}_r${i}" \
        --shards "$s" --querynode-pods "$q" --insert-chunks "$c" \
        --concurrency "$CONC" --warmup "$WARMUP" --queries "$QUERIES" --recall \
        --runs-csv "$RUNS" "${args[@]}" 2>&1)
    echo "$out" | grep -E "latency us|^  S |^  N |^  M |net/query|recall@|!!" | sed "s/^/  r$i /"
    means+=("$(echo "$out" | awk '/latency us/{print $4; exit}')")
  done

  # spread across the repeats -- the number that says whether this point is usable.
  # sort -n does the ordering because mawk (Ubuntu's default awk) has no asort().
  printf '%s\n' "${means[@]}" | sort -n | awk -v lim="$CV_LIMIT" '
    {n++; v[n]=$1; s+=$1; ss+=$1*$1}
    END{ if(n<2) exit
         # SAMPLE sd (n-1): dividing by n understates the spread by 0.816 at n=3,
         # which lets a true 6.1% CV slip through a 5% gate.
         m=s/n; sd=sqrt((ss - n*m*m)/(n-1)); cv=(m>0)?100*sd/m:0
         med=(n%2)?v[int(n/2)+1]:(v[n/2]+v[n/2+1])/2
         printf "  => median %.1f us   mean %.1f   CV %.1f%%%s\n", med, m, cv,
                (cv>lim ? "   <<< UNSTABLE, do not use this point" : "") }'
}

# ================================================================ stages
# Where does the nprobe slope gap live? The trace-OFF regression gave
# b(E2E)=0.0182 vs b(segcore)=0.0101 ms/nprobe. The 0.0081 residual was only ever
# NAMED "result movement"; this stage measures it. Tracing ON at nprobe in
# {4,16,32,64}, then a per-stage regression attributes the slope.
#
# Reuses the existing mm_deep1m_s1_c1_ivf -- no create, no reload. The trace-OFF
# nprobe rows are untouched; these are labelled M2NT_*.
stage_nprobe_trace () {
  say "########## nprobe stage attribution (tracing ON)"
  guard
  [ -f "$TRACE_VALUES" ] || die "no $TRACE_VALUES"
  local coll="mm_${MM_DATASET}_s1_c1_ivf" np JG
  scale_q 1 "$TRACE_VALUES" || return 1
  kubectl -n "$MM_NS" get deploy jaeger >/dev/null 2>&1 || die "jaeger missing"
  jaeger_bound
  wait_queryable "$coll"
  wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 120 2>&1 | grep -E "baseline written"

  for np in ${NT_NPROBES:-4 16 32 64}; do
    say "---- traced  nprobe=$np"
    # 2500 queries, warmup 800: at >=200 VALID traces after the fast-failure
    # filter we need headroom, and MEMORY_MAX_TRACES=2000 bounds the ring anyway.
    $PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "M2NT_NP${np}" \
        --shards 1 --querynode-pods 1 --insert-chunks 1 --concurrency 1 \
        --warmup "${NT_WARMUP:-800}" --queries "${NT_QUERIES:-2500}" --recall \
        --index IVF_FLAT --index-params '{"nlist":1024}' \
        --search-params "{\"nprobe\":$np}" --runs-csv "$RUNS" \
        --notes "nprobe stage attribution, tracing ON -- E2E INFLATED, stages only" 2>&1 \
        | grep -E "latency us|recall@"
    sleep 20            # exporter flush
    JG=$(jaeger_ip) || { echo "  !! no Ready jaeger"; continue; }
    if ! $PY "$MM_ROOT/scripts/12_trace_breakdown.py" --jaeger "$JG" \
        --label "M2NT_NP${np}" --limit "${NT_LIMIT:-400}" --lookback 20m --save-raw \
        --out-dir "$MM_RESULTS/trace" 2>&1 | tail -22; then
      printf '\033[31m  !! trace collection FAILED for nprobe=%s\033[0m\n' "$np"
    fi
  done

  say "tracing back OFF"
  scale_q 1 "$VALUES"
}
# S sweep (S={1,4,16}, N=1, M=4) and N sweep (S=16, (N,M)={(1,4),(2,2),(4,1)}),
# each measured twice: ID-only (output_fields empty) and +vector.
# Nothing else changes between the two -- same collection, same index, same
# search params, NQ=1, C=1, topK=10.
#
# M is not set directly: it follows from redistribution. At S=16 the 64 sealed
# segments spread over N workers, so N=1/2/4 gives M=4/2/1. Always confirmed by
# 07_probe_snm.py in row() before the number is used.
stage_snsweep () {
  say "########## S sweep + N sweep, ID-only vs +vector"
  guard

  local q s coll
  for q in 1 2 4; do
    # Q=1 covers the whole S sweep; Q=2 and Q=4 only add the S=16 N points.
    local shards="16"
    [ "$q" = 1 ] && shards="1 4 16"
    scale_q "$q" "$VALUES" || continue
    for s in $shards; do ensure_coll "mm_${MM_DATASET}_s${s}_c${CHUNKS}" "$s" "$CHUNKS"; done
    wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"

    for s in $shards; do
      coll="mm_${MM_DATASET}_s${s}_c${CHUNKS}"
      wait_queryable "$coll"
      # ID-only: S=1 and S=16 at Q=1 already exist as M2_N_*, but re-measuring
      # them here keeps every row of these two tables from one contiguous session,
      # so the ID-only and +vector halves are not separated by hours of drift.
      row "M2SN_ID_S${s}_Q${q}"  "$coll" "$s" "$q" "$CHUNKS" \
          --output-fields ""       --notes "S/N sweep, ID only"
      row "M2SN_VEC_S${s}_Q${q}" "$coll" "$s" "$q" "$CHUNKS" \
          --output-fields "vector" --notes "S/N sweep, + raw vector"
    done
  done
}
stage_ladders () {
  say "########## G4 baseline + G1 N ladder + G2 M ladder   (trace OFF)"
  guard

  # -- G2 first: M ladder needs Q=1 and three collections, all at S=1
  scale_q 1 "$VALUES" || return 1
  for c in 1 4 16; do ensure_coll "mm_${MM_DATASET}_s1_c${c}" 1 "$c"; done
  wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"

  # G4: the paper's first sentence
  row "M2_BASE_S1_Q1_M1"  "mm_${MM_DATASET}_s1_c1"  1 1 1  --notes "M2 G4 baseline"
  row "M2_M_S1_Q1_C4"     "mm_${MM_DATASET}_s1_c4"  1 1 4  --notes "M2 G2 M ladder"
  row "M2_M_S1_Q1_C16"    "mm_${MM_DATASET}_s1_c16" 1 1 16 --notes "M2 G2 M ladder"

  # -- G1: N ladder at two S points
  for s in 1 16; do ensure_coll "mm_${MM_DATASET}_s${s}_c${CHUNKS}" "$s" "$CHUNKS"; done
  for q in 1 2 4; do
    say "##### Q = $q"
    scale_q "$q" "$VALUES" || continue
    for s in 1 16; do ensure_coll "mm_${MM_DATASET}_s${s}_c${CHUNKS}" "$s" "$CHUNKS"; done
    wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"
    for s in 1 16; do
      row "M2_N_S${s}_Q${q}" "mm_${MM_DATASET}_s${s}_c${CHUNKS}" "$s" "$q" "$CHUNKS" \
          --notes "M2 G1 N ladder"
    done
  done
}

stage_traces () {
  say "########## same points with OpenTelemetry ON   (proportions only)"
  guard
  [ -f "$TRACE_VALUES" ] || die "no $TRACE_VALUES (needs the trace: otlp block + jaeger)"
  local JG
  kubectl -n "$MM_NS" get deploy jaeger >/dev/null 2>&1 || \
    die "jaeger deployment missing in ns $MM_NS"
  jaeger_bound
  JG=$(jaeger_ip) || die "no Ready jaeger pod"
  echo "  JAEGER=$JG"

  for q in ${TRACE_QS:-1 2 4}; do
    scale_q "$q" "$TRACE_VALUES" || continue
    for s in ${TRACE_SHARDS:-1 16}; do
      local coll="mm_${MM_DATASET}_s${s}_c${CHUNKS}"
      ensure_coll "$coll" "$s" "$CHUNKS"
      trace_sizing "$s"
      jaeger_bound "$s" "$q"
      wait_queryable "$coll"
      say "---- traced  S=$s Q=$q   (warmup=$TW, queries=$TQ, trace limit=$TL)"
      $PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "M2T_S${s}_Q${q}" \
          --shards "$s" --querynode-pods "$q" --insert-chunks "$CHUNKS" \
          --concurrency 1 --warmup "$TW" --queries "$TQ" --runs-csv "$RUNS" \
          --notes "M2 traced -- latency INFLATED by span export, use percentages only" 2>&1 \
          | grep -E "latency us"
      sleep 15   # let the exporter flush
      JG=$(jaeger_ip) || { echo "  !! no Ready jaeger pod; skipping"; continue; }
      if ! $PY "$MM_ROOT/scripts/12_trace_breakdown.py" --jaeger "$JG" \
          --label "M2T_S${s}_Q${q}" --limit "$TL" --lookback 15m --save-raw \
          --out-dir "$MM_RESULTS/trace" 2>&1 | tail -12; then
        printf '\033[31m  !! trace collection FAILED for S=%s Q=%s -- rerun this point\033[0m\n' "$s" "$q"
      fi
    done
  done
  # M ladder traces at Q=1 -- skipped when the stage is scoped to a redo
  [ -n "${TRACE_QS:-}${TRACE_SHARDS:-}" ] && { say "scoped rerun: skipping M ladder traces"; scale_q 1 "$VALUES"; return 0; }
  scale_q 1 "$TRACE_VALUES" || return 1
  for c in 1 16; do
    local coll="mm_${MM_DATASET}_s1_c${c}"
    ensure_coll "$coll" 1 "$c"
    $PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "M2T_M_S1_Q1_C${c}" \
        --shards 1 --querynode-pods 1 --insert-chunks "$c" --concurrency 1 \
        --warmup 100 --queries "$TRACE_QUERIES" --runs-csv "$RUNS" --notes "M2 traced M ladder" 2>&1 | grep -E "latency us"
    sleep 15
    JG=$(jaeger_ip) || { echo "  !! no Ready jaeger pod; skipping"; continue; }
    $PY "$MM_ROOT/scripts/12_trace_breakdown.py" --jaeger "$JG" \
        --label "M2T_M_S1_Q1_C${c}" --limit "$TRACE_LIMIT" --lookback 15m --save-raw \
        --out-dir "$MM_RESULTS/trace" 2>&1 | tail -12
  done

  say "turning tracing back OFF (it costs ~+21% latency)"
  scale_q 1 "$VALUES"
}

stage_outfields () {
  say "########## G3-A  index read vs vector read, output_fields A/B"
  guard
  scale_q 1 "$VALUES" || return 1
  local coll="mm_${MM_DATASET}_s1_c1"
  ensure_coll "$coll" 1 1
  wait_queryable "$coll"
  wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"
  # A0: ids + distances only -- the segment never touches the raw vector payload
  row "M2_OF_none" "$coll" 1 1 1 --output-fields ""       --notes "M2 G3-A: ids+distance only"
  # A1: forces the post-ANN raw-vector read and ships topk x dim floats back
  row "M2_OF_vec"  "$coll" 1 1 1 --output-fields "vector" --notes "M2 G3-A: + raw vector materialisation"
  echo
  echo "  delta = vector materialisation + transport. Cross-check with the spans:"
  echo "  ReduceHelper::FillEntryData must grow from ~1.5us to something large."
  echo "  If it does not, output_fields did not take effect and the row is void."
}

stage_nprobe () {
  say "########## G3-B  IVF_FLAT nprobe regression (the real index/vector split)"
  guard
  scale_q 1 "$VALUES" || return 1
  local coll="mm_${MM_DATASET}_s1_c1_ivf"
  ensure_coll "$coll" 1 1 "IVF_FLAT" '{"nlist":1024}'
  wait_queryable "$coll"
  wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"
  for np in ${NPROBES:-1 2 3 4 6 8 12 16 24 32 48 64}; do
    row "M2_NPROBE_${np}" "$coll" 1 1 1 \
        --index IVF_FLAT --index-params '{"nlist":1024}' \
        --search-params "{\"nprobe\":$np}" \
        --notes "M2 G3-B nlist=1024 nprobe=$np"
  done
  echo
  echo "  Fit BOTH:  E2E(nprobe) = c + b*nprobe      and   SegCoreSearch(nprobe) = a + b*nprobe"
  echo "    a     = index read   (coarse quantizer over 1024 centroids)"
  echo "    b*np  = vector read  (nprobe posting lists of raw vectors)"
  echo "    c - a = Milvus structural cost -- the number Motivation 2 exists to show"
  echo "  16_report_m2.py does the fit. Keep the recall column: nprobe changes recall."
}

# ================================================================ layer A/B
#
# L0/L1/L2 -- the SAME query (S=16, N=1, M=4, 64 sealed segments, NQ=1, C=1,
# topK=10, no output_fields) against three PLACEMENTS of the same 2.6.23 cluster.
# Nothing else moves: same collection, same index, same search params, same
# queryNode resource requests. See docs/06_layer_ab_plan.md.
#
#   L0  client+Proxy 94 | delegator 110 | worker 109    both hops inter-machine
#   L1  client+Proxy 94 | delegator 110 | worker 110    delegator->worker same host
#   L2  client+Proxy 94 | delegator  94 | worker  94    every hop same host
#
# The Proxy never moves. The bench client lives on 94, so moving the Proxy would
# turn client->Proxy into an inter-machine hop and the arms would no longer be
# the same path.
#
# WHAT THIS CAN AND CANNOT SAY. The streaming layer cannot be removed in v2.6.23:
# cmd/main.go calls streamingutil.SetStreamingServiceEnabled() unconditionally,
# and QueryCoord never places the streamingnode's embedded QueryNode
# (LabelStreamingNodeEmbeddedQueryNode) into a resource group, so a sealed
# segment can never live on the delegator (docs/01_findings.md F6.1). The layer
# stays; what these arms remove is its TRANSPORT:
#
#     E2E(L0) - E2E(L2) = the part of the layer cost that is network
#     E2E(L2) - segcore = the part that is software and survives at zero network
#
# Do NOT A/B the reduce columns. deleg_reduce_shards is already <= 0.0002 ms
# (docs/05 section 3); the layer's cost is the hop and the delegator stage, which
# only the OTel critical path resolves. The three reduce columns ride along as a
# control that must NOT move.
#
# CONFOUND, and it goes in every caption: co-location removes a NIC hop but adds
# CPU and memory-bandwidth contention, and L2 puts the delegator and the worker on
# the same box as the Proxy, DataNode, MixCoord and the bench client. If E2E(L2)
# is not clearly below E2E(L1), contention ate the saving -- a result, not a
# failure. row() stamps measured busy-cores on every row for this reason.
#
# Arms are INTERLEAVED (L0,L1,L2, L0,L1,L2, ...) not blocked: a block design
# cannot separate the arm from drift, and this box has produced 3970 vs 2867us on
# the same point twenty minutes apart (docs/04 principle 6).
#
# HARDWARE, measured 2026-09-23 -- the three nodes are NOT the same machine:
#
#   94  AMD Threadripper 7960X 24C   48 thr   1 socket / 1 NUMA   5364 MHz max
#   109 Intel Xeon Gold 6548N       128 thr   2 sockets / 2 NUMA  4100 MHz max
#   110 Intel Xeon Platinum 8470    208 thr   2 sockets / 2 NUMA  ~3800 MHz max
#
# A NQ=1 C=1 HNSW search is single-thread and memory-latency bound, so moving the
# WORKER between these nodes changes the measurement by more than the hop does:
# L1 (worker 109 -> 110) measured 4869us against L0's 3619us, i.e. co-locating
# made it 35% SLOWER because 8470 cores are slower than 6548N cores. L2 has the
# opposite bias -- 94 has the fastest cores and one NUMA node.
#
# Therefore L0-vs-L1 and L0-vs-L2 are NOT clean hop measurements; they are
# (hop removed) + (CPU changed). Two things address that and both are required:
#   - L1b, which removes the same hop with segcore pinned to 109 throughout
#   - the traced stage split, which reports segcore's ABSOLUTE ms per arm and so
#     quantifies the CPU term directly

LA_ARMS="${LA_ARMS:-L0 L1 L2}"
LA_ROUNDS="${LA_ROUNDS:-2}"
# Round numbers land in the row label (M2LA_<arm>_k<round>). A second invocation
# must not reuse k1/k2 or its passes pool into the first run's rounds and the
# drift table silently merges two sessions. Offset it instead.
LA_ROUND_OFFSET="${LA_ROUND_OFFSET:-0}"
LA_S="${LA_S:-16}"            # the representative Motivation 2 condition
LA_CHUNKS="${LA_CHUNKS:-4}"   # -> M=4 per worker per shard, 64 segments total

# arm -> nodeSelector values + the node each pod must actually land on.
# The milvus-role labels already exist on the three nodes; only the value moves.
arm_spec () {
  case "$1" in
    L0) ARM_SROLE=streaming; ARM_QROLE=query;     ARM_SNODE="$MM_NODE_STREAM"; ARM_QNODE="$MM_NODE_QUERY"  ;;
    L1) ARM_SROLE=streaming; ARM_QROLE=streaming; ARM_SNODE="$MM_NODE_STREAM"; ARM_QNODE="$MM_NODE_STREAM" ;;
    # L1b removes the SAME hop as L1 but by moving the delegator DOWN to the
    # worker's node instead of the worker UP to the delegator's. That keeps
    # segcore on 109 in both L0 and L1b, so the arm does not silently also change
    # which CPU runs the vector search -- see the hardware note below.
    L1b) ARM_SROLE=query;    ARM_QROLE=query;     ARM_SNODE="$MM_NODE_QUERY";  ARM_QNODE="$MM_NODE_QUERY"  ;;
    L2) ARM_SROLE=proxy;     ARM_QROLE=proxy;     ARM_SNODE="$MM_NODE_PROXY";  ARM_QNODE="$MM_NODE_PROXY"  ;;
    *)  echo "  !! unknown arm '$1'"; return 1 ;;
  esac
  ARM_PLACE="deleg=${ARM_SNODE} worker=${ARM_QNODE}"
}

# Which node is each component actually Running on, ignoring pods that are on
# their way out. A terminating pod still reports a nodeName, and blessing it is
# how you record L1 numbers that were really measured on the L0 topology.
placed_on () {   # $1 = component label -> comma-joined sorted node list
  kubectl -n "$MM_NS" get pods -l "component=$1" \
    -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}{"|"}{.spec.nodeName}{"|"}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk -F'|' '$1=="" && $3=="Running" && $2!="" {print $2}' | sort -u | tr '\n' ',' | sed 's/,$//'
}

pending_why () {
  kubectl -n "$MM_NS" get pods -l 'component in (querynode,streamingnode)' \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{range .status.conditions[?(@.type=="PodScheduled")]}{.reason}{" "}{.message}{end}{"\n"}{end}' 2>/dev/null \
    | grep -v ' Running ' | head -4
}

# Headroom check before a move. Insufficient CPU on the target does not fail the
# helm upgrade -- it leaves the pod Pending forever, and without this you find out
# forty minutes later when wait_placed times out.
node_headroom () {  # $1 = node
  kubectl describe node "$1" 2>/dev/null \
    | awk '/Allocatable:/{a=1} a && /cpu:/{print "    allocatable cpu: " $2; a=0}
           /Allocated resources:/{b=1} b && /^  cpu /{print "    requested   cpu: " $2 " (" $3 ")"; b=0}'
}

wait_placed () {   # $1 = wanted streamingnode node, $2 = wanted querynode node
  local i gs gq
  for i in $(seq 1 60); do
    gs=$(placed_on streamingnode); gq=$(placed_on querynode)
    if [ "$gs" = "$1" ] && [ "$gq" = "$2" ]; then
      echo "  placed: delegator=$gs  worker=$gq"; return 0
    fi
    sleep 10
  done
  printf '\033[31m  !! placement never settled: delegator=%s (want %s)  worker=%s (want %s)\033[0m\n' \
    "${gs:-none}" "$1" "${gq:-none}" "$2"
  pending_why
  return 1
}

# Move the delegator and the worker, then prove they moved.
#
# queryNode goes down to 0 first for the same reason scale_q() does it: a rolling
# update deadlocks under the hard podAntiAffinity. streamingNode is left to roll
# on its own -- it is moving to a DIFFERENT node, so anti-affinity is satisfiable
# and the WAL (embedded Woodpecker, persisted to MinIO) recovers on restart.
place () {   # $1 = arm, $2 = values file
  local arm="$1" vf="$2"
  arm_spec "$arm" || return 1
  say "---- placing $arm   ($ARM_PLACE)"
  echo "  target node headroom:"; node_headroom "$ARM_QNODE"

  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" -f "$vf" \
       --set streamingNode.nodeSelector.milvus-role="$ARM_SROLE" \
       --set queryNode.nodeSelector.milvus-role="$ARM_QROLE" \
       --set queryNode.replicas=0 2>&1 | grep -E "^REVISION|Error" || true
  local i
  for i in $(seq 1 40); do
    [ "$(kubectl -n "$MM_NS" get pods -l component=querynode --no-headers 2>/dev/null | wc -l)" = 0 ] && break
    sleep 10
  done
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" -f "$vf" \
       --set streamingNode.nodeSelector.milvus-role="$ARM_SROLE" \
       --set queryNode.nodeSelector.milvus-role="$ARM_QROLE" \
       --set queryNode.replicas=1 2>&1 | grep -E "^REVISION|Error" || true

  wait_pods 1 || { [ "${LA_FORCE:-0}" = 1 ] || return 1; }
  wait_placed "$ARM_SNODE" "$ARM_QNODE" || { [ "${LA_FORCE:-0}" = 1 ] || return 1; }
  refresh_uri
}

# Put the cluster back on L0. Every later experiment assumes it, so this runs
# even when the stage aborts -- a half-finished A/B that silently leaves the
# worker on 94 would poison the N ladder, the M ladder and the nprobe fit alike.
layerab_restore () {
  say "restoring L0 placement (delegator=$MM_NODE_STREAM, worker=$MM_NODE_QUERY)"
  place L0 "${1:-$VALUES}" || printf '\033[31m  !! RESTORE FAILED -- do not run other stages until placement is L0\033[0m\n'
}

stage_layerab () {
  say "########## layer A/B: L0 / L1 / L2 placement   (trace OFF, absolute ms)"
  guard
  local coll="mm_${MM_DATASET}_s${LA_S}_c${LA_CHUNKS}"
  local arm i rc=0
  trap 'layerab_restore "$VALUES"' INT TERM

  for i in $(seq 1 "$LA_ROUNDS"); do
    local k=$(( i + LA_ROUND_OFFSET ))
    say "##### round $k  ($i / $LA_ROUNDS this invocation)"
    for arm in $LA_ARMS; do
      place "$arm" "$VALUES" || { rc=1; echo "  !! skipping $arm round $k"; continue; }
      ensure_coll "$coll" "$LA_S" "$LA_CHUNKS"
      wait_queryable "$coll" || { rc=1; continue; }
      wait_quiet; bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"
      # N=1 and M=4 are ASSERTED by 07_probe_snm.py inside row(), not assumed:
      # a move that failed to redistribute would otherwise be recorded as an arm
      # effect. Check the printed S/N/M before believing any row.
      row "M2LA_${arm}_k${k}" "$coll" "$LA_S" 1 "$LA_CHUNKS" \
          --output-fields "" \
          --notes "layer A/B arm=$arm round=$k $ARM_PLACE"
    done
  done

  trap - INT TERM
  layerab_restore "$VALUES"
  echo
  echo "  read it as:  E2E(L0)-E2E(L2) = network part of the layer cost"
  echo "               E2E(L2)-segcore = software part, survives at zero network"
  echo "  the three reduce columns and traffic KB/q are CONTROLS: they must not move."
  return $rc
}

stage_layerab_trace () {
  say "########## layer A/B: L0 / L1 / L2 with OpenTelemetry ON   (stage shares)"
  guard
  [ -f "$TRACE_VALUES" ] || die "no $TRACE_VALUES"
  kubectl -n "$MM_NS" get deploy jaeger >/dev/null 2>&1 || die "jaeger deployment missing in ns $MM_NS"
  local coll="mm_${MM_DATASET}_s${LA_S}_c${LA_CHUNKS}" arm JG rc=0
  trap 'layerab_restore "$VALUES"' INT TERM

  # One round only: percentages converge long before absolute ms do, and a traced
  # run is +21% latency anyway (docs/04 principle 2) so it can never be the source
  # of an absolute number.
  for arm in $LA_ARMS; do
    place "$arm" "$TRACE_VALUES" || { rc=1; continue; }
    ensure_coll "$coll" "$LA_S" "$LA_CHUNKS"
    trace_sizing "$LA_S"
    jaeger_bound "$LA_S" 1
    wait_queryable "$coll" || { rc=1; continue; }
    say "---- traced  $arm   ($ARM_PLACE, warmup=$TW, queries=$TQ, trace limit=$TL)"
    $PY "$MM_ROOT/scripts/08_bench.py" --collection "$coll" --label "M2LAT_${arm}" \
        --shards "$LA_S" --querynode-pods 1 --insert-chunks "$LA_CHUNKS" \
        --concurrency 1 --warmup "$TW" --queries "$TQ" --output-fields "" \
        --runs-csv "$RUNS" \
        --notes "layer A/B traced arm=$arm $ARM_PLACE -- E2E INFLATED, stages only" 2>&1 \
        | grep -E "latency us"
    sleep 15   # exporter flush
    JG=$(jaeger_ip) || { echo "  !! no Ready jaeger pod; skipping $arm"; rc=1; continue; }
    if ! $PY "$MM_ROOT/scripts/12_trace_breakdown.py" --jaeger "$JG" \
        --label "M2LAT_${arm}" --limit "$TL" --lookback 15m --save-raw \
        --out-dir "$MM_RESULTS/trace" 2>&1 | tail -14; then
      printf '\033[31m  !! trace collection FAILED for %s -- rerun this arm\033[0m\n' "$arm"
      rc=1
    fi
  done

  trap - INT TERM
  say "tracing back OFF, restoring L0"
  layerab_restore "$VALUES"
  return $rc
}

# 19_loopback_ab.sh sources this file for row/place/ensure_coll; stop before dispatch.
[ "${M2_SOURCE_ONLY:-0}" = 1 ] && return 0

case "${1:-all}" in
  check)     guard ;;
  ladders)   stage_ladders ;;
  traces)    stage_traces ;;
  outfields) stage_outfields ;;
  nprobe)    stage_nprobe ;;
  snsweep)   stage_snsweep ;;
  nprobetrace) stage_nprobe_trace ;;
  layerab)   stage_layerab ;;
  layerabtrace) stage_layerab_trace ;;
  layerab-restore) layerab_restore "$VALUES" ;;
  all)       stage_ladders && stage_outfields && stage_nprobe && stage_traces ;;
  *)         die "usage: $0 {check|ladders|traces|outfields|nprobe|snsweep|nprobetrace|layerab|layerabtrace|layerab-restore|all}" ;;
esac

say "done."
echo "  rows   : $RUNS   (the main runs.csv is left untouched)"
echo "  traces : $MM_RESULTS/trace/TRACE_M2T_*"
echo "  report : $MM_VENV/bin/python $MM_ROOT/scripts/16_report_m2.py"
