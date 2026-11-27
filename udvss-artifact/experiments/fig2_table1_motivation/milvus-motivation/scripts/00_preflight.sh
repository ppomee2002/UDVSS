#!/usr/bin/env bash
# Read-only. Verifies the box is ready AND that the Ceph experiment is finished.
# Exits non-zero if anything is unsafe. Run this first, every time.
set -uo pipefail
: "${MM_ROOT:?source env.sh first}"

fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }

echo "== 1. Ceph workload guard =================================================="
# The vector experiment on 94 runs crimson-osd + ceph_vector_bench. Milvus install,
# data load and benchmarking all contend for the same 24 cores and the same NVMe.
ceph_procs=$(pgrep -a -f 'crimson-osd|ceph-osd|ceph_vector_bench|ceph-vector-ben' 2>/dev/null)
if [ -n "$ceph_procs" ]; then
  bad "Ceph workload still running -- DO NOT PROCEED:"
  echo "$ceph_procs" | sed 's/^/        /'
else
  ok "no crimson-osd / ceph-osd / ceph_vector_bench process found"
fi
# mon/mgr alone are cheap, but flag them so the decision is explicit.
if pgrep -a -f 'ceph-mon|ceph-mgr' >/dev/null 2>&1; then
  warn "ceph-mon/ceph-mgr still up (low CPU, but the cluster is not fully down)"
fi

echo
echo "== 2. tooling =============================================================="
for c in kubectl helm python3; do
  if command -v "$c" >/dev/null 2>&1; then ok "$c -> $(command -v $c)"; else bad "$c not found"; fi
done
[ -x "$(command -v helm)" ] && helm version --short 2>/dev/null | sed 's/^/       /'

echo
echo "== 3. k3s cluster =========================================================="
if kubectl get nodes >/dev/null 2>&1; then
  kubectl get nodes -o custom-columns=\
NAME:.metadata.name,STATUS:.status.conditions[-1].type,ROLE:.metadata.labels.milvus-role,\
STORAGE:.metadata.labels.milvus-storage,CPU:.status.capacity.cpu,MEM:.status.capacity.memory | sed 's/^/       /'
  n_ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -cw Ready)
  [ "$n_ready" -eq 3 ] && ok "3 nodes Ready" || bad "expected 3 Ready nodes, got $n_ready"
  for role in proxy streaming query; do
    c=$(kubectl get nodes -l "milvus-role=$role" --no-headers 2>/dev/null | wc -l)
    [ "$c" -ge 1 ] && ok "label milvus-role=$role present ($c node)" || bad "no node labelled milvus-role=$role"
  done
else
  bad "kubectl cannot reach the cluster"
fi

echo
echo "== 4. storage class ========================================================"
if kubectl get sc local-path >/dev/null 2>&1; then
  ok "StorageClass local-path exists"
  kubectl get sc -o custom-columns=NAME:.metadata.name,DEFAULT:.metadata.annotations.'storageclass\.kubernetes\.io/is-default-class',PROVISIONER:.provisioner | sed 's/^/       /'
else
  bad "StorageClass local-path missing"
fi

echo
echo "== 5. free disk (local-path lives on the node rootfs) ======================"
df -h / /var/lib 2>/dev/null | sed 's/^/       /'
avail_g=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "${avail_g:-0}" -ge 150 ] && ok "${avail_g}G free on /" || warn "only ${avail_g}G free on / (etcd 3x + MinIO 4x30Gi + segments + WAL)"

echo
echo "== 6. helm repo ============================================================"
if helm repo list 2>/dev/null | grep -q zilliztech; then
  ok "zilliztech repo configured"
  helm search repo "$MM_CHART" --version "$MM_CHART_VERSION" 2>/dev/null | sed 's/^/       /'
else
  bad "helm repo zilliztech not added (helm repo add zilliztech https://zilliztech.github.io/milvus-helm/)"
fi

echo
echo "== 7. values file =========================================================="
if [ -f "$MM_VALUES" ]; then
  ok "values file: $MM_VALUES"
else
  bad "values file not found: $MM_VALUES (set MM_VALUES)"
fi

echo
echo "== 8. datasets ============================================================="
if [ -d "$MM_DATASET_DIR/$MM_DATASET" ]; then
  ok "dataset dir $MM_DATASET_DIR/$MM_DATASET"
  ls -la "$MM_DATASET_DIR/$MM_DATASET" | sed 's/^/       /'
else
  bad "dataset $MM_DATASET not found under $MM_DATASET_DIR"
fi

echo
echo "== 9. python venv =========================================================="
if [ -x "$MM_VENV/bin/python" ]; then
  ok "venv $MM_VENV"
  "$MM_VENV/bin/python" -c "import pymilvus,sys;print('       pymilvus',pymilvus.__version__)" 2>/dev/null \
    || warn "pymilvus not installed in the venv yet (see runbook step C)"
else
  warn "venv missing: python3 -m venv $MM_VENV && $MM_VENV/bin/pip install 'pymilvus==2.6.*' numpy"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "PREFLIGHT FAILED -- fix the FAIL lines above before continuing."
  exit 1
fi
echo "PREFLIGHT OK"
