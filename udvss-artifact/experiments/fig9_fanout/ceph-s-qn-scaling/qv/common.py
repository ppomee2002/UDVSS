"""Shared harness for the Qdrant / Vespa counterparts of the Milvus S / QN figure.

Measurement rules copied from milvus-motivation/scripts/08_bench.py so the
lines can share an axis with Milvus:
  * deep1m, L2, topK 10, NQ=1, C=1, 500 warmup + 3000 measured, 3 reps
  * HNSW M=16, efConstruction=200, search ef=64
  * latency = client-side wall time of one search call
  * traffic = sum over the system's query-path pods of eth0 rx+tx
    (/proc/net/dev inside each pod's netns), minus the idle rate measured
    right before the run, divided by the measured query count
"""
import concurrent.futures as cf
import csv
import json
import os
import re
import subprocess
import time

import numpy as np

EXP = "/home/dev_path/experiments/ceph-s-qn-scaling"
QV = os.path.join(EXP, "qv")
# QV_CPU=unlimited: no CPU/memory limits (the Milvus QueryNodes' condition);
# its rows go to results_unlimited/ so the 4-CPU results stay where they are.
CPU_MODE = os.environ.get("QV_CPU", "4")
RES = os.path.join(QV, "results" if CPU_MODE == "4" else "results_unlimited")
DATA = "/home/dev_path/datasets/deep1m"
NS = "sqnexp"
NODE = "milvus-query109"           # every Milvus QueryNode ran here
# Per serving node: 4 CPUs (= UDVSS's 4 logical CPUs per OSD). Unlimited was
# tried first to match the Milvus runs (milvus-3node.experiment.yaml has no
# QueryNode resources block), but unlimited Qdrant on 109's 128 threads
# oversubscribes (S=16/1 peer: 7.3 ms vs 1.25 ms at 4 CPUs; kept in
# results/ref_unlimited). Memory request 4Gi so 16 pods fit, limit 16Gi.
CPU, MEM_REQ, MEM = ("4", "4Gi", "16Gi") if CPU_MODE == "4" else (None, None, None)
TOPK, HNSW_M, HNSW_EFC, EF = 10, 16, 200, 64
WARMUP, QUERIES, REPS = 500, 3000, 3
IDLE_S = 30
# SMOKE=1: tiny corpus and runs into results/smoke_*.csv, to test a harness
SMOKE = os.environ.get("SMOKE") == "1"
if SMOKE:
    WARMUP, QUERIES, REPS, IDLE_S = 20, 50, 1, 3


def log(*a):
    print(time.strftime("[%Y-%m-%dT%H:%M:%SZ]", time.gmtime()), *a, flush=True)


# ------------------------------------------------------------------ k8s
def kc(*args, stdin=None, check=True, timeout=600):
    """kubectl on the k3s control plane (the kubeconfig lives there)."""
    # one multiplexed connection: 16+ parallel `kubectl exec` over fresh ssh
    # sessions trip sshd's MaxStartups ("kex_exchange_identification: closed")
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-p", "31234",
           "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/sqn-kc-%r@%h:%p",
           "-o", "ControlPersist=600",
           "ubuntu@k8s-gw",
           "KUBECONFIG=/home/ubuntu/.kube/config kubectl " +
           " ".join(subprocess.list2cmdline([a]) for a in args)]
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()[:500]}")
    return r.stdout


def pods(selector):
    j = json.loads(kc("get", "pods", "-n", NS, "-l", selector, "-o", "json"))
    return [(p["metadata"]["name"], p["status"].get("podIP"), p["spec"].get("nodeName"),
             p["status"].get("phase"),
             all(c.get("ready") for c in p["status"].get("containerStatuses", [])))
            for p in j["items"]]


def wait_pods(selector, n, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ps = pods(selector)
        if len(ps) == n and all(r for *_, r in ps):
            return sorted(ps)
        time.sleep(5)
    raise RuntimeError(f"pods {selector} not ready: {pods(selector)}")


def delete_selector(selector):
    kc("delete", "statefulset,deployment,service,pod", "-n", NS, "-l", selector,
       "--ignore-not-found", "--wait=true", timeout=900)
    t0 = time.time()
    while pods(selector) and time.time() - t0 < 600:
        time.sleep(3)


# ------------------------------------------------------------------ data
def fvecs(path, limit=None):
    a = np.fromfile(path, dtype=np.int32)
    d = a[0]
    a = a.reshape(-1, d + 1)[:, 1:].view(np.float32)
    return a[:limit] if limit else a


def ivecs(path):
    a = np.fromfile(path, dtype=np.int32)
    k = a[0]
    return a.reshape(-1, k + 1)[:, 1:]


def dataset():
    base = fvecs(f"{DATA}/deep1m_base.fvecs")
    q = fvecs(f"{DATA}/deep1m_query.fvecs")
    gt = ivecs(f"{DATA}/deep1m_groundtruth.ivecs")
    if SMOKE:
        base = base[:20000]
    return base, q, gt


def csv_path(name):
    os.makedirs(RES, exist_ok=True)
    return os.path.join(RES, ("smoke_" if SMOKE else "") + name)


# ------------------------------------------------------------------ traffic
def _netdev(pod):
    out = kc("exec", "-n", NS, pod, "--", "cat", "/proc/net/dev", timeout=60)
    for line in out.splitlines():
        if line.strip().startswith("eth0:"):
            f = line.split(":", 1)[1].split()
            return int(f[0]), int(f[8])
    raise RuntimeError(f"no eth0 in {pod}")


def net_snapshot(pod_names):
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        vals = dict(zip(pod_names, ex.map(_netdev, pod_names)))
    return time.time(), vals


def traffic_per_query(idle0, idle1, b, e, nq):
    """KB/query over the measured window, idle-rate subtracted (per pod, both directions)."""
    rate_t = idle1[0] - idle0[0]
    win = e[0] - b[0]
    rx = tx = 0.0
    for p in b[1]:
        irx = (idle1[1][p][0] - idle0[1][p][0]) / rate_t * win
        itx = (idle1[1][p][1] - idle0[1][p][1]) / rate_t * win
        rx += max(0.0, e[1][p][0] - b[1][p][0] - irx)
        tx += max(0.0, e[1][p][1] - b[1][p][1] - itx)
    return rx / nq / 1024, tx / nq / 1024


# ------------------------------------------------------------------ one row
def run_row(label, search, qv, gt, path_pods, meta, csv_path):
    """search(vec) -> list of ids. Same shape as Milvus's 08_bench.py row."""
    for i in range(WARMUP):
        search(qv[i % len(qv)])
    idle0 = net_snapshot(path_pods)
    time.sleep(IDLE_S)
    idle1 = net_snapshot(path_pods)
    b = net_snapshot(path_pods)
    lat, hits = [], 0
    for i in range(QUERIES):
        v = qv[i % len(qv)]
        t0 = time.perf_counter_ns()
        ids = search(v)
        lat.append((time.perf_counter_ns() - t0) / 1000.0)
        hits += len(set(int(x) for x in ids[:TOPK]) & set(gt[i % len(qv)][:TOPK].tolist()))
    e = net_snapshot(path_pods)
    rx, tx = traffic_per_query(idle0, idle1, b, e, QUERIES)
    lat = np.array(lat)
    row = dict(label=label, **meta, queries=QUERIES,
               mean_us=round(float(lat.mean()), 2), p50_us=round(float(np.percentile(lat, 50)), 2),
               p99_us=round(float(np.percentile(lat, 99)), 2),
               rx_kb_per_query=round(rx, 4), tx_kb_per_query=round(tx, 4),
               traffic_kb_per_query=round(rx + tx, 4),
               recall=round(hits / (QUERIES * TOPK), 4), window_s=round(e[0] - b[0], 2),
               path_pods=";".join(path_pods), ts=time.strftime("%Y%m%d-%H%M%S"))
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    log(f"  {label}: {row['mean_us']/1000:.3f} ms  {row['traffic_kb_per_query']:.2f} KB/q  "
        f"recall {row['recall']}")
    return row


def done_labels(csv_path):
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path) as f:
        return {r["label"] for r in csv.DictReader(f)}
