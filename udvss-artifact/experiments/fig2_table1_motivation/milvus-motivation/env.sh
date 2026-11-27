#!/usr/bin/env bash
# Source me:  . ~/milvus-motivation/env.sh
# Nothing here mutates the cluster.

export MM_ROOT="${MM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# ---- cluster identity ----------------------------------------------------
export MM_NS="${MM_NS:-milvus}"
export MM_RELEASE="${MM_RELEASE:-milvus}"
export MM_CHART="${MM_CHART:-zilliztech/milvus}"
export MM_CHART_VERSION="${MM_CHART_VERSION:-5.0.28}"
export MM_IMAGE_TAG="${MM_IMAGE_TAG:-v2.6.23}"
export MM_VALUES="${MM_VALUES:-$HOME/milvus-3node.yaml}"

# ---- nodes ---------------------------------------------------------------
export MM_NODE_PROXY="${MM_NODE_PROXY:-milvus-proxy94}"       # node94
export MM_NODE_STREAM="${MM_NODE_STREAM:-milvus-stream110}"   # node110
export MM_NODE_QUERY="${MM_NODE_QUERY:-milvus-query109}"      # node109

# ---- data ----------------------------------------------------------------
# fvecs datasets already on this host (see 05_prepare_dataset.py --list)
export MM_DATASET_DIR="${MM_DATASET_DIR:-/home/dev_path/datasets}"
export MM_DATASET="${MM_DATASET:-deep1m}"     # 1,000,000 x 96, L2
export MM_RESULTS="${MM_RESULTS:-$MM_ROOT/results}"

# ---- fixed experiment constants (recorded in every CSV row) --------------
export MM_METRIC="${MM_METRIC:-L2}"
export MM_INDEX="${MM_INDEX:-HNSW}"
export MM_INDEX_PARAMS="${MM_INDEX_PARAMS:-{\"M\":16,\"efConstruction\":200}}"
export MM_SEARCH_PARAMS="${MM_SEARCH_PARAMS:-{\"ef\":64}}"
export MM_TOPK="${MM_TOPK:-10}"
export MM_NQ="${MM_NQ:-1}"                 # NQ is 1 for the whole study. Do not change.
export MM_CONCURRENCY="${MM_CONCURRENCY:-1}"
export MM_WARMUP="${MM_WARMUP:-500}"
export MM_QUERIES="${MM_QUERIES:-3000}"

# ---- python --------------------------------------------------------------
export MM_VENV="${MM_VENV:-$HOME/.venv-milvus-motivation}"
export PYTHONPATH="$MM_ROOT/scripts:${PYTHONPATH:-}"

# ---- resolved endpoints (best effort; safe to fail before install) -------
mm_resolve() {
  local ip
  ip="$(kubectl -n "$MM_NS" get pod -l "app.kubernetes.io/name=milvus,component=proxy" \
        -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)"
  if [ -n "$ip" ]; then
    export MM_PROXY_POD_IP="$ip"
    # Pod IP, not ClusterIP: keeps kube-proxy DNAT out of the measured path.
    export MM_URI="http://${ip}:19530"
  fi
  local cip
  cip="$(kubectl -n "$MM_NS" get svc "${MM_RELEASE}-milvus" \
        -o jsonpath='{.spec.clusterIP}' 2>/dev/null)"
  [ -n "$cip" ] && export MM_CLUSTER_IP="$cip"
  echo "MM_URI=${MM_URI:-<unresolved>}  MM_CLUSTER_IP=${MM_CLUSTER_IP:-<unresolved>}"
}
export -f mm_resolve

echo "milvus-motivation env loaded:"
echo "  ns=$MM_NS release=$MM_RELEASE chart=$MM_CHART:$MM_CHART_VERSION image=$MM_IMAGE_TAG"
echo "  dataset=$MM_DATASET topK=$MM_TOPK NQ=$MM_NQ conc=$MM_CONCURRENCY warmup=$MM_WARMUP queries=$MM_QUERIES"
echo "  run 'mm_resolve' after install to fill MM_URI"
