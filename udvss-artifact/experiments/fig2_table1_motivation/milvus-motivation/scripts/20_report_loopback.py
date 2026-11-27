#!/usr/bin/env python3
"""Loopback A/B report: rows M2LB_<arm>_k<round>_r<rep> from runs_m2.csv.

Per arm: median E2E over all valid passes, the three reduce stages, and traffic
split by interface. A row is valid only if measured S=16, N=1, M=4 (07/08 measure
them from counters; they are not assumed) and it is not stamped POLLUTED.

Traffic vocabulary (lib/podnet.py): eth0 = pod veth bytes, lo = loopback bytes.
Neither is physical NIC traffic, and "sum_pod_eth0" counts every hop between two
measured pods twice. The table says so in its header on purpose.

  20_report_loopback.py [--runs results/runs_m2.csv] [--out results/report_m2/loopback_ab.md]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import statistics as st
from collections import defaultdict

ARMS = ("L0", "L1b", "LO")
PLACE = {"L0": "deleg 110 | worker 109 (2 pods)",
         "L1b": "deleg 109 | worker 109 (2 pods, veth)",
         "LO": "deleg+worker 109 (1 pod, 127.0.0.1)"}
LAT = ("mean_us", "p50_us", "p99_us")
RED = ("proxy_reduce_ms", "deleg_reduce_shards_ms", "worker_reduce_segs_ms", "worker_segcore_ms")
NET = ("eth0_rx_kb_per_query_snqn", "eth0_tx_kb_per_query_snqn",
       "rx_kb_per_query_streaming", "tx_kb_per_query_streaming",
       "rx_kb_per_query_query", "tx_kb_per_query_query",
       "lo_kb_per_query_snqn", "lo_kb_per_query_total", "sum_pod_eth0_rxtx_kb_per_query")


def fnum(r, k):
    try:
        return float(r.get(k, ""))
    except ValueError:
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def cv(xs):
    xs = [x for x in xs if x is not None]
    return 100 * st.stdev(xs) / st.mean(xs) if len(xs) > 1 and st.mean(xs) else None


def fmt(x, nd=3):
    return "" if x is None else f"{x:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    root = os.environ.get("MM_RESULTS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
    ap.add_argument("--runs", default=os.path.join(root, "runs_m2.csv"))
    ap.add_argument("--out", default=os.path.join(root, "report_m2", "loopback_ab.md"))
    a = ap.parse_args()

    by = defaultdict(list)
    dropped = []
    pat = re.compile(r"^M2LB_(L0|L1b|LO)_k(\d+)_r(\d+)$")
    for r in csv.DictReader(open(a.runs)):
        m = pat.match(r.get("label", ""))
        if not m:
            continue
        why = []
        if fnum(r, "actual_shards_touched") != 16: why.append(f"S={r.get('actual_shards_touched')}")
        if fnum(r, "actual_N_per_shard_avg") != 1: why.append(f"N={r.get('actual_N_per_shard_avg')}")
        if fnum(r, "actual_M_per_worker_per_shard") != 4: why.append(f"M={r.get('actual_M_per_worker_per_shard')}")
        if "POLLUTED" in r.get("notes", ""): why.append("POLLUTED")
        if why:
            dropped.append(f"{r['label']}: {', '.join(why)}")
            continue
        by[m.group(1)].append(r)

    L = []
    L.append("# Loopback A/B — delegator→worker transport (S=16, N=1, M=4, NQ=1, C=1)\n")
    L.append("Worker QueryNode on 109 in every arm, identical container resources. "
             "Medians over all valid passes (rounds × repeats).\n")
    L.append("| arm | placement | passes | E2E mean (ms) | CV % | p50 (ms) | p99 (ms) | recall@10 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    agg = {}
    for arm in ARMS:
        rs = by.get(arm, [])
        if not rs:
            L.append(f"| {arm} | {PLACE[arm]} | 0 | | | | | |"); continue
        g = {k: med([fnum(r, k) for r in rs]) for k in LAT + RED + NET + ("recall_at_k",)}
        g["cv"] = cv([fnum(r, "mean_us") for r in rs])
        agg[arm] = g
        L.append(f"| {arm} | {PLACE[arm]} | {len(rs)} | {fmt(g['mean_us']/1000)} | {fmt(g['cv'],1)} | "
                 f"{fmt(g['p50_us']/1000)} | {fmt(g['p99_us']/1000)} | {fmt(g['recall_at_k'],4)} |")

    L.append("\n## Differences (positive = LO faster)\n")
    L.append("| comparison | what it isolates | ΔE2E (ms) | Δ % |")
    L.append("|---|---|---:|---:|")
    for base, what in (("L1b", "pod network only (veth/bridge/CNI), same host, same CPUs"),
                       ("L0", "machine crossing + pod network (delegator CPU 110 vs 109 also differs)")):
        if base in agg and "LO" in agg:
            d = agg[base]["mean_us"] - agg["LO"]["mean_us"]
            L.append(f"| {base} − LO | {what} | {fmt(d/1000)} | {fmt(100*d/agg[base]['mean_us'],1)} |")

    L.append("\n## Reduce stages and segcore (ms, server metrics) — controls, should not move\n")
    L.append("| arm | " + " | ".join(RED) + " |")
    L.append("|---|" + "---:|" * len(RED))
    for arm in ARMS:
        if arm in agg:
            L.append(f"| {arm} | " + " | ".join(fmt(agg[arm][k], 4) for k in RED) + " |")

    L.append("\n## Traffic per query by interface (KB)\n")
    L.append("eth0 = pod veth, lo = loopback inside a netns. **Not physical NIC bytes.** "
             "`sum_pod_eth0` adds every measured pod's eth0 rx+tx, so each pod-to-pod hop is in it twice. "
             "In LO the delegator and worker share one netns (`snqn`), so their eth0 is only what left the pod "
             "(proxy↔delegator, control plane) and the delegator→worker RPC appears under lo.\n")
    L.append("| arm | snqn eth0 rx/tx | streaming eth0 rx/tx | query eth0 rx/tx | lo (snqn) | lo total | sum_pod_eth0 rx+tx |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for arm in ARMS:
        if arm in agg:
            g = agg[arm]
            L.append(f"| {arm} | {fmt(g['eth0_rx_kb_per_query_snqn'],2)}/{fmt(g['eth0_tx_kb_per_query_snqn'],2)} | "
                     f"{fmt(g['rx_kb_per_query_streaming'],2)}/{fmt(g['tx_kb_per_query_streaming'],2)} | "
                     f"{fmt(g['rx_kb_per_query_query'],2)}/{fmt(g['tx_kb_per_query_query'],2)} | "
                     f"{fmt(g['lo_kb_per_query_snqn'],2)} | {fmt(g['lo_kb_per_query_total'],2)} | "
                     f"{fmt(g['sum_pod_eth0_rxtx_kb_per_query'],2)} |")
    if "LO" in agg and "L1b" in agg:
        lo = agg["LO"]["lo_kb_per_query_snqn"] or 0
        hop = (agg["L1b"]["rx_kb_per_query_query"] or 0) + (agg["L1b"]["tx_kb_per_query_query"] or 0)
        L.append(f"\nCross-check: LO lo bytes {lo:.2f} KB/q vs L1b worker eth0 rx+tx {hop:.2f} KB/q "
                 "(same RPCs, different interface; they should be close).")
    if dropped:
        L.append("\n## Dropped rows\n")
        L += [f"- {d}" for d in dropped]

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w").write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
