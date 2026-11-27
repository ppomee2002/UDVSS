"""Shared helpers: kubectl wrappers, config capture, CSV writing.

Nothing in here mutates the cluster: only `get`, `exec ... cat`, and HTTP GET.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional

NS = os.environ.get("MM_NS", "milvus")
RELEASE = os.environ.get("MM_RELEASE", "milvus")


# --------------------------------------------------------------------------- kubectl
def kubectl(*args: str, timeout: int = 60) -> str:
    cmd = ["kubectl", "-n", NS, *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> rc={p.returncode}\n{p.stderr.strip()}")
    return p.stdout


@dataclass(frozen=True)
class Pod:
    """One Milvus PROCESS. Usually one pod; the loopback arm packs two into one pod.

    name       unique key used everywhere (metrics snapshots, per-pod json)
    kube_name  the Kubernetes pod to `kubectl exec` into ("" = same as name)
    container  container to exec into ("" = the pod's only container)
    netns      network-namespace key. Two processes in one pod share it, so their
               /proc/net/dev is ONE set of counters and must be read once.
    """
    name: str
    component: str
    node: str
    ip: str
    metrics_port: int = 9091
    kube_name: str = ""
    container: str = ""
    netns: str = ""

    @property
    def metrics_url(self) -> str:
        return f"http://{self.ip}:{self.metrics_port}/metrics"

    @property
    def pod(self) -> str:
        return self.kube_name or self.name

    @property
    def netns_key(self) -> str:
        return self.netns or self.pod


# Components that expose search-path metrics we care about.
MILVUS_COMPONENTS = ("proxy", "querynode", "streamingnode", "mixcoord", "datanode", "indexnode")

# The loopback arm (docs/07_loopback_ab_plan.md): StreamingNode and the worker
# QueryNode as two containers of ONE pod, sharing its network namespace. The pod
# carries component=snqn-loop, so it is expanded here into the two processes the
# rest of the harness expects. The worker's metrics port is moved off 9091 because
# both processes cannot bind it in one netns.
LOOP_COMPONENT = "snqn-loop"
LOOP_CONTAINERS = (("streamingnode", "streamingnode", 9091),
                   ("querynode", "querynode", int(os.environ.get("MM_LOOP_QN_METRICS_PORT", 9092))))


def list_pods(components: Iterable[str] = MILVUS_COMPONENTS) -> List[Pod]:
    raw = json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/name=milvus", "-o", "json"))
    want = set(components)
    out: List[Pod] = []
    for item in raw.get("items", []):
        if item["metadata"].get("deletionTimestamp"):
            continue
        labels = item["metadata"].get("labels", {})
        comp = labels.get("component", "")
        status = item.get("status", {})
        if status.get("phase") != "Running":
            continue
        ip = status.get("podIP")
        if not ip:
            continue
        name, node = item["metadata"]["name"], item["spec"].get("nodeName", "?")
        if comp == LOOP_COMPONENT:
            for container, c, port in LOOP_CONTAINERS:
                if c in want:
                    out.append(Pod(f"{name}/{container}", c, node, ip, port,
                                   kube_name=name, container=container, netns=name))
            continue
        if comp not in want:
            continue
        out.append(Pod(name, comp, node, ip))
    return sorted(out, key=lambda p: (p.component, p.name))


def net_group(pods: Iterable[Pod]) -> Dict[str, str]:
    """netns key -> traffic group: the component, or 'snqn' for a shared netns.

    A netns shared by a streamingnode and a querynode cannot be split between them
    from /proc/net/dev -- its eth0 is whatever left the POD. It gets its own group
    instead of being silently booked under either component.
    """
    comps: Dict[str, set] = {}
    for p in pods:
        comps.setdefault(p.netns_key, set()).add(p.component)
    return {k: (next(iter(v)) if len(v) == 1 else "snqn") for k, v in comps.items()}


def pods_by_component(pods: Iterable[Pod]) -> Dict[str, List[Pod]]:
    d: Dict[str, List[Pod]] = {}
    for p in pods:
        d.setdefault(p.component, []).append(p)
    return d


# --------------------------------------------------------------------------- run config
@dataclass
class RunConfig:
    """Everything that must be identical across rows of one table.

    Written verbatim into every output CSV so a row is self-describing.
    """
    label: str
    dataset: str
    vectors: int
    dim: int
    metric: str
    index_type: str
    index_params: str
    search_params: str
    topk: int
    nq: int
    concurrency: int
    warmup: int
    queries: int
    collection: str
    num_shards: int
    replica_number: int
    querynode_pods: int
    insert_chunks: int
    milvus_version: str
    chart_version: str
    physical_nodes: int
    notes: str = ""
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- csv
def write_csv(path: str, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> str:
    if not rows:
        raise ValueError(f"refusing to write empty csv: {path}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if fieldnames is None:
        seen: List[str] = []
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.append(k)
        fieldnames = seen
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def append_csv(path: str, row: Dict[str, Any], fieldnames: List[str]) -> None:
    """Append one row, MIGRATING the file if the schema has grown since it was created.

    The naive version writes the header only when the file is empty, so adding a
    column later produces rows with more values than the header. csv.DictReader then
    buckets the surplus under the key None and any rewrite raises
    "dict contains fields not in fieldnames: None". That happened once here; this
    rewrites the file under the new header instead, filling absent columns with "".
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0

    if exists:
        with open(path, newline="") as fh:
            old_header = next(csv.reader(fh), [])
        if old_header != fieldnames:
            with open(path, newline="") as fh:
                old_rows = list(csv.DictReader(fh, restkey="__extra__"))
            for r in old_rows:
                r.pop("__extra__", None)
            tmp = path + ".tmp"
            with open(tmp, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                for r in old_rows:
                    w.writerow({k: r.get(k, "") for k in fieldnames})
            os.replace(tmp, path)
            print(f"  [note] {os.path.basename(path)}: schema changed, migrated "
                  f"{len(old_rows)} existing row(s) to the new {len(fieldnames)}-column header")

    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})


def write_json(path: str, obj: Any) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
    return path


# --------------------------------------------------------------------------- stats
def percentile(sorted_vals: List[float], q: float) -> float:
    """Nearest-rank percentile on an already-sorted list. q in [0,100]."""
    if not sorted_vals:
        return float("nan")
    if q <= 0:
        return sorted_vals[0]
    if q >= 100:
        return sorted_vals[-1]
    import math
    rank = math.ceil(q / 100.0 * len(sorted_vals))
    return sorted_vals[min(max(rank, 1), len(sorted_vals)) - 1]


def summarize(latencies_us: List[float]) -> Dict[str, float]:
    if not latencies_us:
        return {}
    s = sorted(latencies_us)
    n = len(s)
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    return {
        "count": n,
        "mean_us": mean,
        "stddev_us": var ** 0.5,
        "min_us": s[0],
        "p50_us": percentile(s, 50),
        "p90_us": percentile(s, 90),
        "p95_us": percentile(s, 95),
        "p99_us": percentile(s, 99),
        "max_us": s[-1],
    }
