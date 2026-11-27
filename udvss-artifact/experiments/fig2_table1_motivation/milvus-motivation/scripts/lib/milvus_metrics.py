"""Scrape Milvus :9091/metrics and turn counter deltas into measured S / N / M.

Metric names, label names and label VALUES below were read out of milvus-io/milvus
at tag v2.6.23 (pkg/metrics/*.go plus the call sites). See docs/02_snm_semantics.md
for the file:line anchors. In particular:

  * segment_state values are "Sealed" / "Growing"  (capitalised)
  * scope value for the delegator->worker RPC is   "FromLeader"
    (the delegator's own entry uses "OnLeader" -- metrics.go:96-97)
  * status values are LOWERCASE: "success" / "fail" / "total" (metrics.go:29-33)
  * reduce_level / reduce_type values are          "shards" / "segments", "batch_reduce"
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .common import Pod

# name{k="v",k2="v2"} value
_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+([^\s]+)\s*$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

Sample = Tuple[str, Tuple[Tuple[str, str], ...], float]


def scrape(url: str, timeout: float = 10.0) -> Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    out: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
    for line in body.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, _, labelstr, val = m.groups()
        try:
            v = float(val)
        except ValueError:
            continue
        labels = tuple(sorted(_LABEL.findall(labelstr or "")))
        out[(name, labels)] = v
    return out


@dataclass
class Snapshot:
    """One scrape of every pod we care about."""
    per_pod: Dict[str, Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float]]
    pods: Dict[str, Pod]

    @classmethod
    def take(cls, pods: Iterable[Pod]) -> "Snapshot":
        per_pod = {}
        index = {}
        for p in pods:
            index[p.name] = p
            try:
                per_pod[p.name] = scrape(p.metrics_url)
            except Exception as exc:  # a pod without the metrics port must not kill a run
                per_pod[p.name] = {}
                print(f"  [warn] scrape failed for {p.name} ({p.metrics_url}): {exc}")
        return cls(per_pod, index)


def _match(labels: Tuple[Tuple[str, str], ...], want: Dict[str, str]) -> bool:
    d = dict(labels)
    return all(d.get(k) == v for k, v in want.items())


def delta_sum(before: Snapshot, after: Snapshot, pod: str, name: str,
              want: Optional[Dict[str, str]] = None) -> float:
    """Sum of (after-before) over every series of `name` on `pod` matching `want`."""
    want = want or {}
    b, a = before.per_pod.get(pod, {}), after.per_pod.get(pod, {})
    total = 0.0
    for key, av in a.items():
        n, labels = key
        if n != name or not _match(labels, want):
            continue
        total += av - b.get(key, 0.0)
    return total


def delta_by_pod(before: Snapshot, after: Snapshot, name: str,
                 want: Optional[Dict[str, str]] = None,
                 components: Optional[Iterable[str]] = None) -> Dict[str, float]:
    comps = set(components) if components else None
    out: Dict[str, float] = {}
    for pod_name, pod in after.pods.items():
        if comps is not None and pod.component not in comps:
            continue
        d = delta_sum(before, after, pod_name, name, want)
        if d:
            out[pod_name] = d
    return out


def histogram_mean_ms(before: Snapshot, after: Snapshot, pod: str, name: str,
                      want: Optional[Dict[str, str]] = None) -> Optional[float]:
    """Mean of a prometheus histogram over the window, in whatever unit it was observed in.

    Milvus observes all of these in milliseconds; three of them truncate to integer ms
    (see docs/02_snm_semantics.md F9). Returns None when no observations happened.
    """
    ds = delta_sum(before, after, pod, name + "_sum", want)
    dc = delta_sum(before, after, pod, name + "_count", want)
    if dc <= 0:
        return None
    return ds / dc


# --------------------------------------------------------------------------- S / N / M
SEARCH = {"query_type": "search"}


@dataclass
class SNM:
    """Measured S / N / M.

    Definitions follow the supervisor's model, which is per-shard, not per-query-total:

        S = shards a query touches
        N = QueryNodes that participate **per shard**
        M = segments searched **per QueryNode, for one shard**

    One worker `SearchSegments` RPC covers exactly one (shard, worker) pair
    (`organizeSubTask` emits one sub-task per SnapshotItem, i.e. per worker NodeID,
    inside a single-channel Search). So:

        worker_rpcs_per_query = S x N            ->  N = worker_rpcs_per_query / S
        sealed_searches       = worker_rpcs x M  ->  M = sealed_searches / worker_rpcs

    The whole-query aggregates (`N_unique_total`, `M_total_searched`) are kept as
    SECONDARY columns -- they answer a different question and must not be reported
    as N and M.
    """
    n_queries: int
    # ---- S
    shard_reduces_total: float
    S_actual: float
    shard_reduces_by_pod: Dict[str, float]
    # ---- N (primary = per shard)
    N_per_shard_avg: float
    # ---- N (secondary = whole query)
    N_unique_total: int
    N_worker_pods: List[str]
    worker_rpcs_by_pod: Dict[str, float]
    worker_rpcs_per_query: float
    # ---- M (primary = per worker, per shard)
    M_per_worker_per_shard: float
    M_per_worker_per_shard_by_pod: Dict[str, float]
    # ---- M (secondary = whole query)
    M_total_searched: float
    M_sealed_per_query: float
    M_growing_per_query: float
    sealed_segment_searches_by_pod: Dict[str, float]
    growing_segment_searches_by_pod: Dict[str, float]
    # ---- per-pod shard coverage (how many of the S shards each worker served)
    shards_served_by_pod: Dict[str, float]

    def as_row(self) -> Dict[str, object]:
        """Column names here are the contract shared by 08_bench.py and 09_make_tables.py."""
        cov = list(self.shards_served_by_pod.values())
        return {
            # --- primary: the supervisor's S / N / M ---
            "actual_shards_touched": round(self.S_actual, 4),
            "actual_N_per_shard_avg": round(self.N_per_shard_avg, 4),
            "actual_M_per_worker_per_shard": round(self.M_per_worker_per_shard, 4),
            # --- secondary: whole-query aggregates, NOT N and M ---
            "actual_N_unique_total": self.N_unique_total,
            "actual_M_total_searched": round(self.M_total_searched, 4),
            "actual_worker_rpcs_per_query": round(self.worker_rpcs_per_query, 4),
            "actual_sealed_segments_per_query": round(self.M_sealed_per_query, 4),
            "actual_growing_segments_per_query": round(self.M_growing_per_query, 4),
            # --- per-pod detail ---
            "actual_M_per_worker_by_pod": ";".join(
                f"{k.split('-')[-1]}={v:.2f}" for k, v in sorted(self.M_per_worker_per_shard_by_pod.items())
            ),
            "actual_shards_served_by_pod": ";".join(
                f"{k.split('-')[-1]}={v:.2f}" for k, v in sorted(self.shards_served_by_pod.items())
            ),
            "actual_shards_served_min": round(min(cov), 4) if cov else "",
            "actual_shards_served_max": round(max(cov), 4) if cov else "",
            "querynode_pods_hit": ";".join(sorted(self.N_worker_pods)),
        }


def measure_snm(before: Snapshot, after: Snapshot, n_queries: int) -> SNM:
    """Derive S, N and M from counter deltas. n_queries must be the measured-window count."""
    if n_queries <= 0:
        raise ValueError("n_queries must be > 0")

    # ---- S: one shard-level reduce per inbound Search RPC on a delegator.
    shard_reduces = delta_by_pod(
        before, after, "milvus_querynode_sq_reduce_latency_count",
        {**SEARCH, "reduce_level": "shards", "reduce_type": "batch_reduce"},
    )
    shard_total = sum(shard_reduces.values())
    S_actual = shard_total / n_queries

    # ---- worker RPCs: one per (shard, worker-with-segments).
    worker_rpcs_all = delta_by_pod(
        before, after, "milvus_querynode_sq_req_count",
        {**SEARCH, "scope": "FromLeader", "status": "success"},
    )
    # A delegator also serves its own growing segments through a FromLeader-scoped
    # sub-task, so restrict the worker side to component=querynode.
    worker_rpcs = {p: v for p, v in worker_rpcs_all.items()
                   if after.pods[p].component == "querynode"}
    worker_pods = [p for p, v in worker_rpcs.items() if v > 0]
    worker_rpc_total = sum(worker_rpcs.values())

    # ---- segment searches: one observation per segment actually searched.
    sealed_all = delta_by_pod(before, after, "milvus_querynode_sq_segment_latency_count",
                              {**SEARCH, "segment_state": "Sealed"})
    growing_all = delta_by_pod(before, after, "milvus_querynode_sq_segment_latency_count",
                               {**SEARCH, "segment_state": "Growing"})
    sealed_workers = {p: v for p, v in sealed_all.items()
                      if after.pods[p].component == "querynode"}
    sealed_worker_total = sum(sealed_workers.values())

    # ---- primary N and M (per shard / per worker-per-shard)
    N_per_shard = (worker_rpc_total / shard_total) if shard_total > 0 else float("nan")
    M_per_worker = (sealed_worker_total / worker_rpc_total) if worker_rpc_total > 0 else float("nan")

    m_by_pod = {p: sealed_workers[p] / worker_rpcs[p]
                for p in worker_rpcs if worker_rpcs[p] > 0 and p in sealed_workers}
    # how many of the S shards each worker served, per query
    cov_by_pod = {p: (v / n_queries) / S_actual for p, v in worker_rpcs.items()
                  if v > 0 and S_actual > 0}

    return SNM(
        n_queries=n_queries,
        shard_reduces_total=shard_total,
        S_actual=S_actual,
        shard_reduces_by_pod=shard_reduces,
        N_per_shard_avg=N_per_shard,
        N_unique_total=len(worker_pods),
        N_worker_pods=worker_pods,
        worker_rpcs_by_pod=worker_rpcs,
        worker_rpcs_per_query=worker_rpc_total / n_queries,
        M_per_worker_per_shard=M_per_worker,
        M_per_worker_per_shard_by_pod=m_by_pod,
        M_total_searched=(sum(sealed_all.values()) + sum(growing_all.values())) / n_queries,
        M_sealed_per_query=sum(sealed_all.values()) / n_queries,
        M_growing_per_query=sum(growing_all.values()) / n_queries,
        sealed_segment_searches_by_pod=sealed_all,
        growing_segment_searches_by_pod=growing_all,
        shards_served_by_pod=cov_by_pod,
    )


# --------------------------------------------------------------------------- breakdown
# (metric, label filter, component, "coarse" flag). coarse => integer-millisecond (F9).
BREAKDOWN_SPEC = [
    ("proxy_e2e",            "milvus_proxy_sq_latency",              SEARCH,                                      "proxy",         True),
    ("proxy_wait_shards",    "milvus_proxy_sq_wait_result_latency",  SEARCH,                                      "proxy",         True),
    ("proxy_reduce",         "milvus_proxy_sq_reduce_result_latency", SEARCH,                                     "proxy",         False),
    ("proxy_decode",         "milvus_proxy_sq_decode_result_latency", SEARCH,                                     "proxy",         False),
    ("deleg_wait_tsafe",     "milvus_querynode_sq_wait_tsafe_latency", SEARCH,                                    "streamingnode", False),
    ("deleg_reduce_shards",  "milvus_querynode_sq_reduce_latency",   {**SEARCH, "reduce_level": "shards"},        "streamingnode", False),
    ("worker_rpc",           "milvus_querynode_sq_req_latency",      {**SEARCH, "scope": "FromLeader"},           "querynode",     True),
    ("worker_queue",         "milvus_querynode_sq_queue_latency",    SEARCH,                                      "querynode",     False),
    ("worker_seg_search",    "milvus_querynode_sq_segment_latency",  {**SEARCH, "segment_state": "Sealed"},       "querynode",     False),
    ("worker_segcore",       "milvus_querynode_sq_core_latency",     SEARCH,                                      "querynode",     False),
    ("worker_reduce_segs",   "milvus_querynode_sq_reduce_latency",   {**SEARCH, "reduce_level": "segments"},      "querynode",     False),
]


def measure_breakdown(before: Snapshot, after: Snapshot) -> Dict[str, Optional[float]]:
    """Mean stage latency in ms over the window, aggregated across pods of a component.

    Aggregation is sum(delta_sum)/sum(delta_count), i.e. an observation-weighted mean,
    which is the right thing when several pods of the same component took part.
    Keys ending in `_coarse` flag the integer-ms metrics (F9).
    """
    out: Dict[str, Optional[float]] = {}
    for key, name, want, comp, coarse in BREAKDOWN_SPEC:
        tot_sum = 0.0
        tot_cnt = 0.0
        for pod_name, pod in after.pods.items():
            if pod.component != comp:
                continue
            tot_sum += delta_sum(before, after, pod_name, name + "_sum", want)
            tot_cnt += delta_sum(before, after, pod_name, name + "_count", want)
        out[f"{key}_ms"] = (tot_sum / tot_cnt) if tot_cnt > 0 else None
        out[f"{key}_obs"] = tot_cnt
        if coarse:
            out[f"{key}_coarse"] = True
    return out


# --------------------------------------------------------------------------- app bytes
def measure_app_bytes(before: Snapshot, after: Snapshot) -> Dict[str, float]:
    """Application-level byte counters. Incomplete on purpose -- see docs/02 'Network'."""
    rx = sum(delta_sum(before, after, p, "milvus_proxy_receive_bytes_count", {"msg_type": "search"})
             for p, pod in after.pods.items() if pod.component == "proxy")
    tx = sum(delta_sum(before, after, p, "milvus_proxy_send_bytes_count")
             for p, pod in after.pods.items() if pod.component == "proxy")
    deleg = sum(delta_sum(before, after, p, "milvus_querynode_execute_bytes_counter", {"msg_type": "search"})
                for p, pod in after.pods.items() if pod.component == "streamingnode")
    return {
        "app_client_to_proxy_bytes": rx,
        "app_proxy_to_client_bytes": tx,
        "app_search_req_bytes_at_delegator": deleg,
    }
