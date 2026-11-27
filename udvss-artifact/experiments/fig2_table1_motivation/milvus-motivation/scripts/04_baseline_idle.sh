#!/usr/bin/env bash
# Read-only. Step B: idle CPU / memory / network baseline for all three machines,
# plus the per-pod idle network rate that the benchmark subtracts from its counters.
#
# Usage: 04_baseline_idle.sh [seconds]        (default 120)
set -uo pipefail
: "${MM_ROOT:?source env.sh first}"
DUR="${1:-120}"
OUT="$MM_RESULTS/baseline/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$OUT"
echo "idle baseline for ${DUR}s -> $OUT"

# ---- node level, via a DaemonSet-free approach: read from each node over ssh if
# configured, otherwise read the k8s node metrics + this host's own counters.
node_sample() {  # $1=tag
  {
    echo "### tag=$1 t=$(date -u +%FT%TZ)"
    echo "--- /proc/stat"; head -1 /proc/stat
    echo "--- /proc/meminfo"; grep -E 'MemTotal|MemAvailable|MemFree|Cached' /proc/meminfo
    echo "--- /proc/net/dev"; cat /proc/net/dev
    echo "--- loadavg"; cat /proc/loadavg
  } > "$OUT/host94_$1.txt"
}

echo "[1/4] host 94 (this machine) sample A"
node_sample A

echo "[2/4] cluster-wide node snapshot"
kubectl top nodes 2>/dev/null | tee "$OUT/kubectl_top_nodes_A.txt" \
  || echo "  (metrics-server not installed; node CPU/mem via kubectl top unavailable)" | tee "$OUT/kubectl_top_nodes_A.txt"
kubectl -n "$MM_NS" top pods 2>/dev/null | tee "$OUT/kubectl_top_pods_A.txt" >/dev/null || true

echo "[3/4] per-pod netns counters, sample A -> wait ${DUR}s -> sample B"
"${MM_VENV}/bin/python" - "$OUT" "$DUR" <<'PY'
import json, os, sys, time
sys.path.insert(0, os.path.join(os.environ["MM_ROOT"], "scripts"))
from lib.common import list_pods, write_json
from lib import podnet

out_dir, dur = sys.argv[1], float(sys.argv[2])
pods = list_pods()
print(f"  {len(pods)} milvus pods: " + ", ".join(f"{p.component}/{p.node}" for p in pods))
a = podnet.NetSnapshot.take(pods)
time.sleep(dur)
b = podnet.NetSnapshot.take(pods)
rate = podnet.idle_rate_from(a, b)
detail = {p.name: {"component": p.component, "node": p.node, "ip": p.ip, "netns": p.netns_key} for p in pods}
write_json(os.path.join(out_dir, "idle_net_rate.json"),
           {"window_seconds": b.t - a.t, "pods": detail, "rate_bytes_per_s": rate})
print(f"  wrote idle_net_rate.json (window {b.t-a.t:.1f}s)")
for pod, r in sorted(rate.items()):
    print(f"    {pod:<46} eth0 rx {r['rx_bytes_per_s']/1024:8.1f} tx {r['tx_bytes_per_s']/1024:8.1f} KB/s"
          f"   lo {r.get('lo_tx_bytes_per_s', 0)/1024:8.1f} KB/s")
PY

echo "[4/4] host 94 sample B"
node_sample B
kubectl top nodes 2>/dev/null | tee "$OUT/kubectl_top_nodes_B.txt" >/dev/null || true
kubectl -n "$MM_NS" top pods 2>/dev/null | tee "$OUT/kubectl_top_pods_B.txt" >/dev/null || true

# make the newest baseline easy to find
ln -sfn "$OUT" "$MM_RESULTS/baseline/latest"
echo
echo "baseline written to $OUT (symlinked as $MM_RESULTS/baseline/latest)"
echo "08_bench.py picks up \$MM_RESULTS/baseline/latest/idle_net_rate.json automatically."
echo
echo "NOTE: nodes 110 and 109 are sampled through their pods' netns, which is what the"
echo "      per-hop accounting needs. For whole-machine idle CPU/mem on 110/109, run"
echo "      'ssh <node> \"vmstat 1 5; cat /proc/net/dev\"' and drop the output in $OUT."
