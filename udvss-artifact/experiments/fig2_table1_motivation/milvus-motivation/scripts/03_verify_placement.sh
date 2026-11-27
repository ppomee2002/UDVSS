#!/usr/bin/env bash
# Read-only. Step A of the plan: pods / PVCs / node placement / metrics reachability.
set -uo pipefail
: "${MM_ROOT:?source env.sh first}"
OUT="${1:-$MM_RESULTS/placement/$(date +%Y%m%d-%H%M%S)}"; mkdir -p "$OUT"

echo "== pods ===================================================================="
kubectl -n "$MM_NS" get pods -o wide | tee "$OUT/pods.txt"

echo
echo "== placement vs intent ====================================================="
kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=milvus \
  -o custom-columns=COMPONENT:.metadata.labels.component,POD:.metadata.name,NODE:.spec.nodeName,IP:.status.podIP,PHASE:.status.phase \
  | tee "$OUT/placement.txt"
echo
echo "expected: proxy / mixcoord / datanode  -> $MM_NODE_PROXY"
echo "          streamingnode                -> $MM_NODE_STREAM   (delegator lives here)"
echo "          querynode                    -> $MM_NODE_QUERY    (sealed segments live here)"
echo "          indexnode                    -> MUST NOT EXIST    (no such role in v2.6.23;"
echo "                                          DataNode builds indexes -- docs/01_findings.md F12)"
if kubectl -n "$MM_NS" get pods -l component=indexnode --no-headers 2>/dev/null | grep -q .; then
  echo "  FAIL an indexnode pod exists -- set indexNode.enabled: false"
  kubectl -n "$MM_NS" get pods -l component=indexnode -o wide | sed 's/^/       /'
else
  echo "  OK   no indexnode pod"
fi

echo
echo "== woodpecker reality check ================================================"
if kubectl -n "$MM_NS" get sts -l component=woodpecker 2>/dev/null | grep -q woodpecker; then
  echo "  woodpecker StatefulSet EXISTS -- unexpected on 2.6.23, see docs/01_findings.md F2"
  kubectl -n "$MM_NS" get sts,pvc -l component=woodpecker
else
  echo "  no woodpecker StatefulSet (expected: WAL is embedded in streamingnode)"
  echo "  verifying the running config actually selected woodpecker as the MQ:"
  sn=$(kubectl -n "$MM_NS" get pod -l component=streamingnode -o jsonpath='{.items[0].metadata.name}')
  # NOTE: /milvus/configs/milvus.yaml is the IMAGE's built-in default and still says
  # `mq.type: default` -- the chart does not overwrite it. The chart mounts its own
  # generated config as default.yaml and extraConfigFiles as user.yaml, and Milvus
  # merges milvus.yaml < default.yaml < user.yaml < etcd. Read default.yaml.
  echo "    -- chart-generated default.yaml:"
  kubectl -n "$MM_NS" exec "$sn" -- sh -c \
    'grep -A2 "^mq:" /milvus/configs/default.yaml; grep "^messageQueue:" /milvus/configs/default.yaml' \
    2>/dev/null | sed 's/^/      /'
  echo "    -- authoritative: what the process actually initialised"
  kubectl -n "$MM_NS" logs "$sn" --tail=4000 2>/dev/null \
    | grep -iE 'mqType=|NewEmbedClient|Condition write detection|open wal manager' \
    | head -6 | sed 's/^/      /'
  echo "    (expect mqType=woodpecker and woodpecker.NewEmbedClient => embedded WAL)"
fi

echo
echo "== PVC / PV ================================================================"
kubectl -n "$MM_NS" get pvc -o wide | tee "$OUT/pvc.txt"
kubectl get pv -o custom-columns=NAME:.metadata.name,CAP:.spec.capacity.storage,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0] | tee "$OUT/pv.txt"
if kubectl -n "$MM_NS" get pvc --no-headers 2>/dev/null | grep -q woodpecker; then
  echo "  FAIL a woodpecker PVC exists -- embedded WAL must not claim storage"
else
  echo "  OK   no woodpecker PVC (embedded WAL persists into the MinIO bucket)"
fi

echo
echo "== storage distribution: etcd + MinIO are the ONLY tiers ==================="
# Embedded Woodpecker has no storage tier of its own; WAL capacity IS MinIO capacity.
echo "-- etcd (expect 3 replicas, one per node, hard anti-affinity)"
kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=etcd \
  -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase | tee "$OUT/etcd_spread.txt"
etcd_nodes=$(kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=etcd \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | sort -u | wc -l)
etcd_pods=$(kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=etcd --no-headers 2>/dev/null | wc -l)
[ "$etcd_pods" = "3" ] && [ "$etcd_nodes" = "3" ] \
  && echo "  OK   3 etcd pods across 3 distinct nodes" \
  || echo "  WARN $etcd_pods etcd pods across $etcd_nodes nodes (expected 3/3)"

echo "-- MinIO (expect 4 replicas distributed, soft anti-affinity, 30Gi each)"
kubectl -n "$MM_NS" get pods -l app=minio \
  -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase | tee "$OUT/minio_spread.txt"
minio_pods=$(kubectl -n "$MM_NS" get pods -l app=minio --no-headers 2>/dev/null | wc -l)
minio_nodes=$(kubectl -n "$MM_NS" get pods -l app=minio \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | sort -u | wc -l)
echo "  $minio_pods MinIO pods across $minio_nodes nodes (4 pods over 3 nodes means one node hosts two -- expected with soft anti-affinity)"
kubectl -n "$MM_NS" get pvc -l app=minio \
  -o custom-columns=PVC:.metadata.name,SIZE:.spec.resources.requests.storage,SC:.spec.storageClassName,STATUS:.status.phase 2>/dev/null | sed 's/^/  /'
echo "  NOTE: this bucket also holds the Woodpecker WAL -- size it for WAL + segments + index."

echo
echo "== metrics endpoints reachable from this host =============================="
kubectl -n "$MM_NS" get pods -l app.kubernetes.io/name=milvus \
  -o jsonpath='{range .items[*]}{.metadata.labels.component}{" "}{.metadata.name}{" "}{.status.podIP}{"\n"}{end}' \
| while read -r comp pod ip; do
    [ -z "${ip:-}" ] && continue
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$ip:9091/metrics" || echo ERR)
    printf '  %-16s %-42s %-15s %s\n' "$comp" "$pod" "$ip" "$code"
  done | tee "$OUT/metrics_endpoints.txt"

echo
echo "== proxy reachability from host 94 ========================================="
pip=$(kubectl -n "$MM_NS" get pod -l component=proxy -o jsonpath='{.items[0].status.podIP}')
cip=$(kubectl -n "$MM_NS" get svc "${MM_RELEASE}-milvus" -o jsonpath='{.spec.clusterIP}')
for target in "$pip:19530" "$cip:19530"; do
  timeout 3 bash -c "</dev/tcp/${target%%:*}/${target##*:}" 2>/dev/null \
    && echo "  OK   $target" || echo "  FAIL $target"
done
echo "  -> export MM_URI=http://$pip:19530"

echo
echo "saved to $OUT"
