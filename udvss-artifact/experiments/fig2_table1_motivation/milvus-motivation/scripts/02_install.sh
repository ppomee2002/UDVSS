#!/usr/bin/env bash
# THE ONLY SCRIPT HERE THAT MUTATES THE CLUSTER.
# It refuses to run while the Ceph workload is up, and asks for confirmation.
set -euo pipefail
: "${MM_ROOT:?source env.sh first}"

echo "This will run: helm upgrade --install $MM_RELEASE $MM_CHART --version $MM_CHART_VERSION"
echo "              namespace=$MM_NS values=$MM_VALUES image=$MM_IMAGE_TAG"
echo

bash "$MM_ROOT/scripts/00_preflight.sh" || { echo "preflight failed -- aborting"; exit 1; }
bash "$MM_ROOT/scripts/01_render_and_check.sh" || { echo "render check failed -- aborting"; exit 1; }

echo
read -r -p "Proceed with the install? type INSTALL to continue: " a
[ "$a" = "INSTALL" ] || { echo "aborted"; exit 1; }

kubectl get ns "$MM_NS" >/dev/null 2>&1 || kubectl create ns "$MM_NS"

helm upgrade --install "$MM_RELEASE" "$MM_CHART" \
  --version "$MM_CHART_VERSION" \
  --namespace "$MM_NS" \
  -f "$MM_VALUES" \
  --wait --timeout 20m

echo
echo "install returned; waiting for every milvus pod to be Ready ..."
kubectl -n "$MM_NS" wait --for=condition=Ready pod \
  -l app.kubernetes.io/name=milvus --timeout=15m || true
kubectl -n "$MM_NS" get pods -o wide
