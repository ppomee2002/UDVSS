"""Per-netns network counters, read out of each pod's own network namespace.

`kubectl exec <pod> -- cat /proc/net/dev` reads the counters of that pod's netns.

WHAT THE COUNTERS ARE -- and are not:
  * eth0 = the pod's veth end. Bytes that LEFT OR ENTERED THE POD, whether the peer
    is on another machine or on the same one. It is NOT the physical NIC: a hop
    between two pods on one host crosses both veths and never touches the NIC. So a
    sum of eth0 over pods is "sum of pod-veth bytes", never "physical traffic", and
    every hop between two measured pods is counted twice (sender tx + receiver rx).
  * lo = loopback inside the netns. Only non-zero for traffic between processes that
    share the netns -- in this study, the loopback arm's delegator->worker RPC.
    Every lo packet increments lo rx AND lo tx of the same netns, so the bytes
    that crossed loopback are lo tx (== lo rx), not their sum.
  * Two processes in one pod share one netns: read it once (Pod.netns_key), never
    once per process, or the pod's bytes are doubled.

Two caveats, both handled by the caller:
  * the counters include background traffic (etcd heartbeats, tSafe ticks, MinIO,
    gRPC keepalives). Subtract an idle baseline measured over an equal-length window.
  * `kubectl exec` itself costs a few KB on the *api-server* path, not on eth0 of the
    target pod's data path -- but sample strictly outside the measured window anyway.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .common import Pod, kubectl


@dataclass
class IfCounters:
    rx_bytes: int
    rx_packets: int
    tx_bytes: int
    tx_packets: int


def parse_proc_net_dev(text: str) -> Dict[str, IfCounters]:
    out: Dict[str, IfCounters] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        f = rest.split()
        if len(f) < 16:
            continue
        try:
            out[iface] = IfCounters(int(f[0]), int(f[1]), int(f[8]), int(f[9]))
        except ValueError:
            continue
    return out


IFACES = ("eth0", "lo")


def read_pod(pod: Pod, container: Optional[str] = None) -> Dict[str, IfCounters]:
    args = ["exec", pod.pod]
    container = container or pod.container
    if container:
        args += ["-c", container]
    args += ["--", "cat", "/proc/net/dev"]
    return parse_proc_net_dev(kubectl(*args, timeout=30))


@dataclass
class NetSnapshot:
    """Counters keyed by netns (see Pod.netns_key), NOT by process."""
    t: float
    per_pod: Dict[str, Dict[str, IfCounters]]

    @classmethod
    def take(cls, pods: Iterable[Pod], iface_hint: str = "eth0") -> "NetSnapshot":
        per_pod = {}
        for p in pods:
            k = p.netns_key
            if k in per_pod:
                continue          # shared netns, already read through its other process
            try:
                per_pod[k] = read_pod(p)
            except Exception as exc:
                print(f"  [warn] /proc/net/dev unavailable in {k}: {exc}")
                per_pod[k] = {}
        return cls(time.time(), per_pod)


def delta(before: NetSnapshot, after: NetSnapshot, iface: str = "eth0") -> Dict[str, Dict[str, float]]:
    """Per-pod {rx_bytes, tx_bytes, rx_packets, tx_packets, seconds} over the window."""
    dt = max(after.t - before.t, 1e-9)
    out: Dict[str, Dict[str, float]] = {}
    for pod_name, aif in after.per_pod.items():
        a = aif.get(iface)
        b = before.per_pod.get(pod_name, {}).get(iface)
        if a is None or b is None:
            continue
        out[pod_name] = {
            "rx_bytes": float(a.rx_bytes - b.rx_bytes),
            "tx_bytes": float(a.tx_bytes - b.tx_bytes),
            "rx_packets": float(a.rx_packets - b.rx_packets),
            "tx_packets": float(a.tx_packets - b.tx_packets),
            "seconds": dt,
        }
    return out


def per_query(net_delta: Dict[str, Dict[str, float]],
              idle_rate: Optional[Dict[str, Dict[str, float]]],
              n_queries: int,
              window_seconds: float,
              iface: str = "eth0") -> Dict[str, Dict[str, float]]:
    """Bytes attributable to the queries, per query, after removing the idle rate.

    idle_rate is a per-netns {rx_bytes_per_s, tx_bytes_per_s} measured with no load
    (eth0), plus lo_rx_bytes_per_s / lo_tx_bytes_per_s for iface="lo".
    Values are clamped at 0: a negative result means the idle baseline was measured
    under different conditions and should be re-taken, which the caller reports.
    """
    pre = "" if iface == "eth0" else f"{iface}_"
    out: Dict[str, Dict[str, float]] = {}
    for pod_name, d in net_delta.items():
        rx, tx = d["rx_bytes"], d["tx_bytes"]
        base_rx = base_tx = 0.0
        if idle_rate and pod_name in idle_rate:
            base_rx = idle_rate[pod_name].get(f"{pre}rx_bytes_per_s", 0.0) * window_seconds
            base_tx = idle_rate[pod_name].get(f"{pre}tx_bytes_per_s", 0.0) * window_seconds
        net_rx = rx - base_rx
        net_tx = tx - base_tx
        # A baseline taken while the cluster was still settling (WAL recovery, coord
        # resync after a rollout) can be 100x the true idle rate and then swallows most
        # of the measured traffic without ever going negative. Measured once: a
        # streamingnode baseline of 1399 KB/s against a true idle of ~13 KB/s. Flag it.
        rx_dom = base_rx > 0.5 * rx if rx > 0 else False
        tx_dom = base_tx > 0.5 * tx if tx > 0 else False
        out[pod_name] = {
            "rx_bytes_total": rx,
            "tx_bytes_total": tx,
            "rx_bytes_idle_est": base_rx,
            "tx_bytes_idle_est": base_tx,
            "rx_bytes_per_query": max(net_rx, 0.0) / n_queries,
            "tx_bytes_per_query": max(net_tx, 0.0) / n_queries,
            "rx_underflow": net_rx < 0,
            "tx_underflow": net_tx < 0,
            "rx_baseline_dominates": rx_dom,
            "tx_baseline_dominates": tx_dom,
        }
    return out


def idle_rate_from(before: NetSnapshot, after: NetSnapshot) -> Dict[str, Dict[str, float]]:
    """Per-netns idle rate: eth0 as rx/tx_bytes_per_s (the original keys, so old
    baselines stay readable) and lo as lo_rx/lo_tx_bytes_per_s."""
    out: Dict[str, Dict[str, float]] = {}
    for iface in IFACES:
        pre = "" if iface == "eth0" else f"{iface}_"
        for pod, v in delta(before, after, iface).items():
            r = out.setdefault(pod, {})
            r[f"{pre}rx_bytes_per_s"] = v["rx_bytes"] / v["seconds"]
            r[f"{pre}tx_bytes_per_s"] = v["tx_bytes"] / v["seconds"]
    return out
