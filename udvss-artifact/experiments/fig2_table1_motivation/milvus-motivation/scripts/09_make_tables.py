#!/usr/bin/env python3
"""Step J. Turn results/runs.csv into the four report tables (Markdown + CSV) and
the figures for the paper.

    09_make_tables.py                       # everything, from $MM_RESULTS/runs.csv
    09_make_tables.py --runs other.csv --out-dir report-v2

Every table row carries the full condition (dataset, index, params, topK, NQ,
concurrency, warmup/query count) so a row is self-describing in the paper.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import OrderedDict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- chart styling
# Categorical slots 1..5 of the validated default palette (see dataviz skill
# references/palette.md). Slots are assigned in fixed order and never cycled.
# validate_palette.js: 3 slots all-pairs PASS; 5 slots adjacent PASS (light).
# The contrast WARN on aqua/yellow/magenta is relieved by direct labels + the
# CSV/Markdown table view emitted next to every figure.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e3e2de"


def style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.patch.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
        ax.spines[s].set_linewidth(1.0)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.xaxis.label.set_color(INK2)
    ax.yaxis.label.set_color(INK2)
    ax.title.set_color(INK)


# ---------------------------------------------------------------- io
def read_runs(path: str) -> List[Dict[str, str]]:
    with open(path) as fh:
        return list(csv.DictReader(fh))


def f(row: Dict[str, str], key: str, default=float("nan")) -> float:
    v = row.get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def md_table(headers: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def write_pair(out_dir: str, name: str, headers: List[str], rows: List[List[str]], title: str, note: str = "") -> str:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{name}.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)
    md = f"### {title}\n\n" + md_table(headers, rows)
    if note:
        md += f"\n{note}\n"
    with open(os.path.join(out_dir, f"{name}.md"), "w") as fh:
        fh.write(md)
    return md


# ---------------------------------------------------------------- tables
def table1(runs, out_dir) -> str:
    headers = ["dataset", "vectors", "dim", "metric", "index", "index params",
               "search params", "topK", "NQ", "concurrency", "warmup", "queries",
               "physical nodes", "Milvus", "chart"]
    seen = OrderedDict()
    for r in runs:
        key = tuple(r.get(k, "") for k in
                    ("dataset", "vectors", "dim", "metric", "index_type", "index_params",
                     "search_params", "topk", "nq", "concurrency", "warmup", "queries",
                     "physical_nodes", "milvus_version", "chart_version"))
        seen[key] = True
    rows = [list(k) for k in seen]
    note = ("Every row of Tables 2-4 was produced under one of these configurations; "
            "the S/N/M sweeps vary only shard count, QueryNode pod count and insert "
            "chunk count. NQ is 1 throughout.")
    if len(rows) > 1:
        note += ("\n\n**Warning: more than one configuration is present in runs.csv.** "
                 "Rows from different configurations are not comparable; filter before "
                 "putting them in one table.")
    return write_pair(out_dir, "table1_environment", headers, rows,
                      "Table 1. Environment / configuration", note)


def table2(runs, out_dir) -> str:
    """Column semantics are the supervisor's: N is per shard, M is per QueryNode per shard."""
    headers = ["S (set)", "N (set: QN pods)", "M (set: chunks)",
               "actual shards touched (S)",
               "actual QueryNodes per shard (N)",
               "actual segments per QueryNode (M)",
               "N_unique_total", "M_total_searched",
               "mean (ms)", "p50 (ms)", "p95 (ms)", "p99 (ms)",
               "RX KB/query", "TX KB/query", "label"]
    rows = []
    for r in sorted(runs, key=lambda x: (f(x, "param_S_num_shards"),
                                         f(x, "param_N_querynode_pods"),
                                         f(x, "param_M_insert_chunks"))):
        rows.append([
            r.get("param_S_num_shards", ""), r.get("param_N_querynode_pods", ""),
            r.get("param_M_insert_chunks", ""),
            r.get("actual_shards_touched", ""),
            r.get("actual_N_per_shard_avg", ""),
            r.get("actual_M_per_worker_per_shard", ""),
            r.get("actual_N_unique_total", ""),
            r.get("actual_M_total_searched", ""),
            f"{f(r,'mean_us')/1000:.3f}", f"{f(r,'p50_us')/1000:.3f}",
            f"{f(r,'p95_us')/1000:.3f}", f"{f(r,'p99_us')/1000:.3f}",
            f"{f(r,'rx_kb_per_query_total',0):.2f}", f"{f(r,'tx_kb_per_query_total',0):.2f}",
            r.get("label", ""),
        ])
    note = (
        "**Definitions.** S = shards a query touches. **N = QueryNodes that participate "
        "*per shard*.** **M = segments searched *per QueryNode, for one shard*.** "
        "`N_unique_total` (distinct QueryNode pods over the whole query) and "
        "`M_total_searched` (all segments searched anywhere for the query) are whole-query "
        "aggregates reported for completeness -- **they are not N and M**.\n\n"
        "**How the actuals are obtained.** A worker `SearchSegments` RPC covers exactly one "
        "(shard, worker) pair, so from Prometheus counter deltas over the run:\n\n"
        "* `S = delta milvus_querynode_sq_reduce_latency_count{reduce_level=\"shards\"} / queries`\n"
        "* `N = (delta milvus_querynode_sq_req_count{scope=\"FromLeader\"} / queries) / S`\n"
        "* `M = delta milvus_querynode_sq_segment_latency_count{segment_state=\"Sealed\"} / "
        "delta milvus_querynode_sq_req_count{scope=\"FromLeader\"}`\n\n"
        "*(set)* columns are what was configured -- in particular *N (set)* is a **pod count**, "
        "not a fan-out, and it is not expected to equal the measured N. Growing segments are "
        "searched on the delegator and are excluded from M (column "
        "`actual_growing_segments_per_query` in runs.csv). Per-shard min/max for N is available "
        "only from the control plane (`cp_N_per_shard_min`/`_max` in runs.csv): the Prometheus "
        "counters carry no channel label, so the measured N is an average over the S shards; "
        "when `cp_N_per_shard_min == cp_N_per_shard_max` that average is exact for every shard. "
        "RX/TX are per-pod netns byte deltas summed over all Milvus pods, idle baseline subtracted.")
    return write_pair(out_dir, "table2_snm", headers, rows,
                      "Table 2. S / N / M motivation", note)


def table3(runs, out_dir) -> str:
    headers = ["condition", "E2E (ms, client)", "Proxy (ms)", "Streaming/fanout (ms)",
               "QueryNode local (ms)", "reduce/other (ms)", "unattributed (ms)",
               "-- QN queue (ms)", "-- QN per-segment (ms)", "-- QN segcore (ms)"]
    rows = []
    for r in sorted(runs, key=lambda x: x.get("label", "")):
        e2e     = f(r, "mean_us") / 1000.0
        # Proxy: the two fine-grained proxy stages. proxy_e2e/proxy_wait_shards are
        # integer-ms and are deliberately NOT used to build the decomposition.
        proxy   = f(r, "proxy_reduce_ms", 0.0) + f(r, "proxy_decode_ms", 0.0)
        # Streaming/fanout: delegator-side pre-work before the workers are called.
        fanout  = f(r, "deleg_wait_tsafe_ms", 0.0)
        # QueryNode local: the whole worker-side SearchSegments RPC (queue + parallel
        # per-segment search + segment reduce), as the worker itself timed it.
        local   = f(r, "worker_rpc_ms", 0.0)
        reduce_ = f(r, "deleg_reduce_shards_ms", 0.0) + f(r, "worker_reduce_segs_ms", 0.0)
        known   = sum(x for x in (proxy, fanout, local, reduce_) if x == x)
        rows.append([
            r.get("label", ""), f"{e2e:.3f}", f"{proxy:.3f}", f"{fanout:.3f}",
            f"{local:.3f}", f"{reduce_:.3f}", f"{max(e2e - known, 0.0):.3f}",
            r.get("worker_queue_ms", ""), r.get("worker_seg_search_ms", ""),
            r.get("worker_segcore_ms", ""),
        ])
    note = ("Stage values are Prometheus histogram means over the run (`delta_sum/delta_count`), "
            "so they are per-observation means, not a decomposition of a single request: "
            "*QueryNode per-segment* is the mean cost of ONE segment and the M segments of "
            "an RPC run in parallel goroutines, which is why it is reported as a sub-column "
            "rather than multiplied into the total. *unattributed* = E2E minus the four "
            "stages, and absorbs client-side gRPC, the two network hops, and scheduling.\n\n"
            "**Quantisation:** `milvus_querynode_sq_req_latency` (the *QueryNode local* "
            "column) and the proxy's `sq_latency` / `sq_wait_result_latency` are observed "
            "with `.Milliseconds()` in Milvus 2.6.23 -- integer milliseconds. At single-digit-ms "
            "latencies that is a +-1 ms error on that column. For publication numbers turn on "
            "OpenTelemetry (`trace.exporter: otlp`, `trace.sampleFraction: 1`) and take the "
            "stage times from the spans; see docs/01_findings.md F9.")
    return write_pair(out_dir, "table3_breakdown", headers, rows,
                      "Table 3. Latency breakdown", note)


def table4(runs, out_dir) -> str:
    headers = ["parameter varied", "value", "mean latency (ms)", "traffic/query (KB)",
               "relative latency overhead", "relative traffic overhead", "label"]
    groups = {
        "S (shards)": ("param_S_num_shards", ("param_N_querynode_pods", "param_M_insert_chunks")),
        "N (QueryNode pods)": ("param_N_querynode_pods", ("param_S_num_shards", "param_M_insert_chunks")),
        "M (insert chunks)": ("param_M_insert_chunks", ("param_S_num_shards", "param_N_querynode_pods")),
    }
    rows = []
    for gname, (varied, held) in groups.items():
        # a one-factor-at-a-time slice = rows where the other two knobs are constant
        buckets: Dict[tuple, List[Dict[str, str]]] = {}
        for r in runs:
            buckets.setdefault(tuple(r.get(h, "") for h in held), []).append(r)
        best = max(buckets.values(), key=len) if buckets else []
        if len(best) < 2:
            continue
        best = sorted(best, key=lambda r: f(r, varied))
        base_lat = f(best[0], "mean_us") / 1000.0
        base_tr = f(best[0], "rx_kb_per_query_total", 0) + f(best[0], "tx_kb_per_query_total", 0)
        for r in best:
            lat = f(r, "mean_us") / 1000.0
            tr = f(r, "rx_kb_per_query_total", 0) + f(r, "tx_kb_per_query_total", 0)
            rows.append([
                gname, r.get(varied, ""), f"{lat:.3f}", f"{tr:.2f}",
                f"{lat/base_lat:.2f}x" if base_lat else "-",
                f"{tr/base_tr:.2f}x" if base_tr else "-",
                r.get("label", ""),
            ])
    note = ("Each block holds the other two knobs constant (the largest such slice found "
            "in runs.csv) and normalises against that block's smallest value.")
    return write_pair(out_dir, "table4_scaling", headers, rows,
                      "Table 4. Scaling overhead", note)


# ---------------------------------------------------------------- figures
def figures(runs, out_dir) -> List[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[skip] matplotlib not installed; tables written, figures skipped")
        return []

    os.makedirs(out_dir, exist_ok=True)
    made = []

    knobs = [("param_S_num_shards", "S (shards)", "fig1_latency_vs_S"),
             ("param_N_querynode_pods", "N (QueryNode pods)", "fig2_latency_vs_N"),
             ("param_M_insert_chunks", "M (insert chunks -> segments)", "fig3_latency_vs_M")]

    for key, xlabel, fname in knobs:
        held = [k for k, _, _ in knobs if k != key]
        buckets: Dict[tuple, List[Dict[str, str]]] = {}
        for r in runs:
            buckets.setdefault(tuple(r.get(h, "") for h in held), []).append(r)
        best = max(buckets.values(), key=len) if buckets else []
        if len(best) < 2:
            continue
        best = sorted(best, key=lambda r: f(r, key))
        x = [f(r, key) for r in best]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.6))
        # --- latency: 3 series, categorical slots 1..3, fixed order, all direct-labeled
        for i, (col, lbl) in enumerate([("mean_us", "mean"), ("p95_us", "p95"), ("p99_us", "p99")]):
            y = [f(r, col) / 1000.0 for r in best]
            ax1.plot(x, y, color=SERIES[i], linewidth=2.0, marker="o", markersize=8,
                     markeredgecolor=SURFACE, markeredgewidth=2, label=lbl, zorder=3)
            ax1.annotate(lbl, (x[-1], y[-1]), textcoords="offset points", xytext=(6, 0),
                         color=INK2, fontsize=9, va="center")
        style_axes(ax1)
        ax1.margins(x=0.14)          # room for the direct labels at the right edge
        ax1.set_xlabel(xlabel); ax1.set_ylabel("latency (ms)")
        ax1.set_title("End-to-end latency, NQ=1", fontsize=11, loc="left")
        ax1.set_xticks(x)
        ax1.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="upper left")

        # --- traffic per query, per hop
        hops = [("proxy", "Proxy"), ("streaming", "StreamingNode"), ("query", "QueryNode")]
        for i, (k, lbl) in enumerate(hops):
            y = [f(r, f"rx_kb_per_query_{k}", 0) + f(r, f"tx_kb_per_query_{k}", 0) for r in best]
            ax2.plot(x, y, color=SERIES[i], linewidth=2.0, marker="o", markersize=8,
                     markeredgecolor=SURFACE, markeredgewidth=2, label=lbl, zorder=3)
        style_axes(ax2)
        ax2.set_xlabel(xlabel); ax2.set_ylabel("KB per query (RX+TX)")
        ax2.set_title("Per-hop traffic", fontsize=11, loc="left")
        ax2.set_xticks(x)
        ax2.legend(frameon=False, fontsize=9, labelcolor=INK2)

        fig.tight_layout()
        p = os.path.join(out_dir, f"{fname}.png")
        fig.savefig(p, dpi=200, facecolor=SURFACE)
        plt.close(fig)
        made.append(p)

    # --- stacked latency breakdown, one bar per condition
    if runs:
        stages = [("proxy_reduce_ms", "Proxy reduce+decode"),
                  ("deleg_wait_tsafe_ms", "Delegator wait tSafe"),
                  ("worker_seg_search_ms", "QueryNode segment search"),
                  ("worker_reduce_segs_ms", "QueryNode reduce"),
                  ("deleg_reduce_shards_ms", "Delegator reduce")]
        labels = [r.get("label", "") for r in runs]
        fig, ax = plt.subplots(figsize=(max(6.4, 0.9 * len(labels) + 3), 3.8))
        bottom = [0.0] * len(runs)
        for i, (col, lbl) in enumerate(stages):
            vals = [f(r, col, 0) or 0.0 for r in runs]
            ax.bar(labels, vals, bottom=bottom, color=SERIES[i % len(SERIES)],
                   label=lbl, width=0.62, zorder=3,
                   edgecolor=SURFACE, linewidth=2)   # 2px surface gap between segments
            bottom = [b + v for b, v in zip(bottom, vals)]
        style_axes(ax)
        ax.set_ylabel("mean stage latency (ms)")
        ax.set_title("Where the time goes (metrics-derived means)", fontsize=11, loc="left")
        ax.tick_params(axis="x", labelrotation=30)
        if max(bottom) > 0:
            ax.set_ylim(0, max(bottom) * 1.08)
        # legend below the plot: a legend inside would sit on top of the stacks
        ax.legend(frameon=False, fontsize=9, labelcolor=INK2, ncol=3,
                  loc="upper center", bbox_to_anchor=(0.5, -0.30))
        fig.tight_layout()
        p = os.path.join(out_dir, "fig4_latency_breakdown.png")
        fig.savefig(p, dpi=200, facecolor=SURFACE)
        plt.close(fig)
        made.append(p)

    return made


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=os.path.join(os.environ.get("MM_RESULTS", "."), "runs.csv"))
    ap.add_argument("--out-dir", default=os.path.join(os.environ.get("MM_RESULTS", "."), "report"))
    ap.add_argument("--filter-label", default=None, help="only rows whose label contains this")
    ap.add_argument("--exclude-label", nargs="*", default=None,
                    help="drop rows whose label matches any of these exactly. Use it for repeat "
                         "measurements of a condition that is already in the sweep (e.g. BASELINE), "
                         "which would otherwise appear twice in the one-factor-at-a-time blocks.")
    a = ap.parse_args()

    if not os.path.exists(a.runs):
        print(f"no runs file: {a.runs}", file=sys.stderr)
        return 1
    runs = read_runs(a.runs)
    if a.filter_label:
        runs = [r for r in runs if a.filter_label in r.get("label", "")]
    if a.exclude_label:
        drop = set(a.exclude_label)
        kept = [r for r in runs if r.get("label", "") not in drop]
        print(f"excluded {len(runs) - len(kept)} row(s): {sorted(drop)}")
        runs = kept
    if not runs:
        print("no rows", file=sys.stderr)
        return 1
    print(f"{len(runs)} rows from {a.runs}")

    parts = [table1(runs, a.out_dir), table2(runs, a.out_dir),
             table3(runs, a.out_dir), table4(runs, a.out_dir)]
    figs = figures(runs, a.out_dir)

    report = os.path.join(a.out_dir, "REPORT.md")
    with open(report, "w") as fh:
        fh.write("# Milvus motivation experiment -- results\n\n")
        fh.write(f"Generated from `{a.runs}` ({len(runs)} runs).\n\n")
        fh.write("\n\n".join(parts))
        if figs:
            fh.write("\n\n## Figures\n\n")
            for p in figs:
                fh.write(f"![{os.path.basename(p)}]({os.path.basename(p)})\n\n")
    print(f"wrote {report}")
    for p in figs:
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
