#!/usr/bin/env bash
# S x Q sweep, where Q = number of QueryNode pods.
#
# Everything that bit us while building this is handled here:
#
#  1. `queryCoord.autoBalance` is false, so scaling queryNode.replicas moves NO
#     segments: a new pod gets nothing and the MEASURED N stays where it was.
#     Verified: two pods Running, actual_N_per_shard_avg still 1.0. Every Q change
#     therefore needs a release+load (11_redistribute.py).
#  2. A load issued right after a scale-up fails with
#     `resource group node not enough [currentNodeNum=0]` because the pod is Running
#     before QueryCoord registers it. 11_redistribute.py retries that case.
#  3. Scaling down then up (rather than a rolling update) avoids the deadlock you get
#     when a hard podAntiAffinity is in play.
#  4. An idle baseline taken while the delegator is replaying its WAL reads ~100x the
#     true rate and silently eats the whole network measurement. We wait for the
#     streamingnode to go quiet first.
#  5. N is MEASURED afterwards, never assumed from the pod count.
#
# Usage:
#   ./13_sweep_SxQ.sh                      # Q=1 2 4, S=1 4 16, concurrency 16
#   QS="1 2 4 8" SHARDS="1 4 16" CONC=16 ./13_sweep_SxQ.sh
#   CONC=1 ./13_sweep_SxQ.sh               # the old, unloaded operating point
#
# Concurrency matters: measured on this cluster, QPS saturates near C=16
# (357 -> 1283 -> 2417 -> 2504 QPS at C=1/4/16/64). At C=1 the cluster runs at
# ~0.5% CPU, so extra QueryNodes have no queue to drain and cannot show a benefit.
# Run the sweep at C near the knee if the question is "does Q help".
set -uo pipefail
: "${MM_ROOT:?source env.sh and env.local.sh first}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

QS="${QS:-1 2 4}"
SHARDS="${SHARDS:-1 4 16}"
CHUNKS="${CHUNKS:-4}"
CONC="${CONC:-16}"
QUERIES="${QUERIES:-4000}"
WARMUP="${WARMUP:-300}"
VALUES="${VALUES:-$HOME/milvus-3node.experiment.yaml}"
PY="$MM_VENV/bin/python"

say () { printf '\n\033[1m%s\033[0m\n' "$*"; }

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

wait_quiet () {  # delegator must stop replaying before we trust an idle baseline
  local sn a b kbs
  for _ in $(seq 1 30); do
    sn=$(kubectl -n "$MM_NS" get pod -l component=streamingnode -o jsonpath='{.items[0].metadata.name}')
    a=$(kubectl -n "$MM_NS" exec "$sn" -- sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    sleep 10
    b=$(kubectl -n "$MM_NS" exec "$sn" -- sh -c "awk '/eth0/{print \$2}' /proc/net/dev" 2>/dev/null)
    kbs=$(( (b - a) / 10 / 1024 ))
    [ "$kbs" -lt 40 ] && { echo "  streamingnode idle ~${kbs} KB/s -- quiet"; return 0; }
    echo "  streamingnode ${kbs} KB/s -- still settling"
  done
  echo "  !! never went quiet; the network columns of this block are suspect"
}

refresh_uri () {
  local pip
  pip=$(kubectl -n "$MM_NS" get pod -l component=proxy -o jsonpath='{.items[0].status.podIP}')
  sed -i "s|^export MM_URI=.*|export MM_URI=http://$pip:19530|" "$MM_ROOT/env.local.sh"
  export MM_URI="http://$pip:19530"
  echo "  MM_URI=$MM_URI"
}

echo "S x Q sweep   Q={$QS}  S={$SHARDS}  chunks=$CHUNKS  concurrency=$CONC  queries=$QUERIES"

for Q in $QS; do
  say "########## Q = $Q QueryNode pod(s)"
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" \
       -f "$VALUES" --set queryNode.replicas=0 2>&1 | grep -E "^REVISION|Error" || true
  for _ in $(seq 1 40); do
    [ "$(kubectl -n "$MM_NS" get pods -l component=querynode --no-headers 2>/dev/null | wc -l)" = 0 ] && break
    sleep 10
  done
  helm upgrade milvus zilliztech/milvus --version 5.0.28 -n "$MM_NS" \
       -f "$VALUES" --set queryNode.replicas="$Q" 2>&1 | grep -E "^REVISION|Error" || true
  wait_pods "$Q" || continue
  kubectl -n "$MM_NS" get pods -l component=querynode \
      -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName --no-headers | sed 's/^/    /'
  refresh_uri

  for S in $SHARDS; do
    COLL="mm_${MM_DATASET}_s${S}_c${CHUNKS}"
    say "---- S=$S  Q=$Q  ($COLL)"
    if ! $PY - "$COLL" <<'PYX' >/dev/null 2>&1
import os, sys
from pymilvus import MilvusClient
sys.exit(0 if MilvusClient(uri=os.environ["MM_URI"]).has_collection(sys.argv[1]) else 1)
PYX
    then
      echo "  [load] $COLL does not exist -- creating"
      $PY "$MM_ROOT/scripts/06_load_collection.py" --collection "$COLL" \
          --shards "$S" --chunks "$CHUNKS" --replicas 1 2>&1 | tail -3
    fi
    # trap 1+2: a Q change only reaches the data plane through release+load
    $PY "$MM_ROOT/scripts/11_redistribute.py" --collections "$COLL" 2>&1 | grep -E "nodes=|FAIL|waiting"
  done

  wait_quiet
  bash "$MM_ROOT/scripts/04_baseline_idle.sh" 180 2>&1 | grep -E "baseline written"

  for S in $SHARDS; do
    COLL="mm_${MM_DATASET}_s${S}_c${CHUNKS}"
    LABEL="C${CONC}_S${S}_Q${Q}"
    say "---- bench $LABEL"
    # trap 5: probe first, so the row is only trusted if measured N == Q
    $PY "$MM_ROOT/scripts/07_probe_snm.py" --collection "$COLL" --queries 200 2>&1 \
        | grep -E "N  QueryNodes PER SHARD|M  segments PER QUERYNODE|OK   N per shard|DIFF"
    $PY "$MM_ROOT/scripts/08_bench.py" --collection "$COLL" --label "$LABEL" \
        --shards "$S" --querynode-pods "$Q" --insert-chunks "$CHUNKS" \
        --concurrency "$CONC" --warmup "$WARMUP" --queries "$QUERIES" --recall \
        --notes "SxQ sweep at concurrency $CONC" 2>&1 \
        | grep -E "latency us|^  S |^  N |^  M |net/query|recall@|!!"
  done
done

say "done. tables:"
echo "  $MM_VENV/bin/python $MM_ROOT/scripts/09_make_tables.py --filter-label C${CONC}_"
echo
echo "CAVEAT for high concurrency: the S/N/M counters are read at the window edges, so"
echo "with thousands of requests in flight the measured M drifts slightly below its true"
echo "value (seen: 2.00 -> 1.99 -> 1.91 -> 1.62 at C=1/4/16/64). Treat S and N as exact"
echo "and M as approximate above C=16, or read M from a C=1 run of the same collection."
