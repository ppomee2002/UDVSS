#!/usr/bin/env bash
# Step G/H driver. Runs every cell for the CURRENT queryNode.replicas value.
# N is a cluster-side knob, so this script does NOT change it -- it reads the live
# pod count, runs the S x M cells, and tells you what to do next.
set -uo pipefail
: "${MM_ROOT:?source env.sh first}"
: "${MM_URI:?run mm_resolve first}"
PY="${MM_VENV}/bin/python"

SHARDS="${SHARDS:-1 4 16}"
CHUNKS="${CHUNKS:-4 16}"
DRY="${DRY:-0}"

N=$(kubectl -n "$MM_NS" get pods -l component=querynode --field-selector=status.phase=Running --no-headers | wc -l)
echo "live querynode pods: N=$N"
echo "sweeping S={$SHARDS} x chunks={$CHUNKS}"
[ "$DRY" = "1" ] && echo "(DRY RUN -- printing commands only)"

for S in $SHARDS; do
  for C in $CHUNKS; do
    COLL="mm_${MM_DATASET}_s${S}_c${C}"
    LABEL="S${S}_N${N}_M${C}"
    if ! "$PY" - "$COLL" <<'PY' >/dev/null 2>&1
import os, sys
from pymilvus import MilvusClient
sys.exit(0 if MilvusClient(uri=os.environ["MM_URI"]).has_collection(sys.argv[1]) else 1)
PY
    then
      echo "  [load] $COLL missing -> 06_load_collection.py --shards $S --chunks $C"
      [ "$DRY" = "1" ] || "$PY" "$MM_ROOT/scripts/06_load_collection.py" \
          --collection "$COLL" --shards "$S" --chunks "$C" --replicas 1 || continue
    fi

    echo "  [probe] $LABEL"
    [ "$DRY" = "1" ] || "$PY" "$MM_ROOT/scripts/07_probe_snm.py" \
        --collection "$COLL" --queries 200 >/dev/null || true

    echo "  [bench] $LABEL"
    [ "$DRY" = "1" ] || "$PY" "$MM_ROOT/scripts/08_bench.py" \
        --collection "$COLL" --label "$LABEL" \
        --shards "$S" --querynode-pods "$N" --insert-chunks "$C" \
        --queries "$MM_QUERIES" --warmup "$MM_WARMUP" --recall
  done
done

cat <<TXT

done for N=$N.

To move to the next N:
  helm upgrade $MM_RELEASE $MM_CHART --version $MM_CHART_VERSION -n $MM_NS \\
       -f $MM_VALUES --set queryNode.replicas=<next N> --reuse-values
  kubectl -n $MM_NS rollout status deploy -l component=querynode
  $MM_ROOT/scripts/04_baseline_idle.sh 300        # pod set changed -> new idle baseline
  # force the segments to spread over the new pod set, then CONFIRM it:
  $MM_VENV/bin/python $MM_ROOT/scripts/07_probe_snm.py --collection <a collection> --queries 200
  $MM_ROOT/scripts/10_run_matrix.sh

When every N is done:
  $MM_VENV/bin/python $MM_ROOT/scripts/09_make_tables.py
TXT
