#!/usr/bin/env python3
"""S x Q report, kept separate from the main one.

Reads ONLY the rows whose label matches ^C(1|16)_S(1|4|16)_Q(1|2|4)$ -- i.e. this
sweep and nothing else. The earlier concurrency ladder used labels ending in
`_N2_M2` and is deliberately excluded, as are every S*_N*_M* row from the first
matrix. runs.csv is read-only here and results/report/ is never touched; output
goes to results/report_sxq/.

    14_report_sxq.py [--runs ...] [--out-dir ...]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LABEL = re.compile(r"^C(1|16)_S(1|4|16)_Q(1|2|4)$")
S_VALS = [1, 4, 16]
Q_VALS = [1, 2, 4]

# Sequential blue ramp (dataviz reference palette, steps 100..700), light -> dark.
# Sequential = one hue for magnitude; never a rainbow.
BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
        "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
SERIES = ["#2a78d6", "#eb6834"]      # categorical slots 1, 2
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de"


def load(path):
    out = {}
    for r in csv.DictReader(open(path)):
        m = LABEL.match(r["label"])
        if m:
            out[(int(m.group(1)), int(m.group(2)), int(m.group(3)))] = r
    return out


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.figure.patch.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID); ax.spines[s].set_linewidth(1.0)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.xaxis.label.set_color(INK2); ax.yaxis.label.set_color(INK2)
    ax.title.set_color(INK)


def heatmap(ax, grid, title, unit, fmt="%.2f"):
    """grid[q][s]. Cells carry their value: the ramp's light end sits below 3:1
    contrast on this surface, so direct labels are required, not optional."""
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    cmap = LinearSegmentedColormap.from_list("seq_blue", BLUE)
    a = np.array(grid, dtype=float)
    norm = Normalize(vmin=float(np.nanmin(a)), vmax=float(np.nanmax(a)))
    ax.imshow(a, cmap=cmap, norm=norm, aspect="auto", origin="lower")
    for i, q in enumerate(Q_VALS):
        for j, s in enumerate(S_VALS):
            v = a[i, j]
            if v != v:
                continue
            # white ink once the cell is dark enough for it to be the readable choice
            col = "#ffffff" if norm(v) > 0.55 else INK
            ax.text(j, i, fmt % v, ha="center", va="center", color=col,
                    fontsize=11)
    ax.set_xticks(range(len(S_VALS))); ax.set_xticklabels(S_VALS)
    ax.set_yticks(range(len(Q_VALS))); ax.set_yticklabels(Q_VALS)
    ax.set_xlabel("S  (shards)"); ax.set_ylabel("Q  (QueryNode pods)")
    ax.set_title(title, fontsize=11, loc="left")
    style(ax)
    ax.grid(False)
    ax.text(1.0, -0.19, unit, transform=ax.transAxes, ha="right",
            color=INK2, fontsize=9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=os.path.join(os.environ.get("MM_RESULTS", "."), "runs.csv"))
    ap.add_argument("--out-dir", default=os.path.join(os.environ.get("MM_RESULTS", "."), "report_sxq"))
    a = ap.parse_args()

    d = load(a.runs)
    want = [(c, s, q) for c in (1, 16) for s in S_VALS for q in Q_VALS]
    missing = [k for k in want if k not in d]
    print(f"matched {len(d)} rows (expected {len(want)})")
    if missing:
        print(f"  missing: {missing}")
    os.makedirs(a.out_dir, exist_ok=True)

    def g(c, key, scale=1.0):
        return [[(float(d[(c, s, q)][key]) * scale) if (c, s, q) in d else float("nan")
                 for s in S_VALS] for q in Q_VALS]

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib missing; tables only")
        plt = None

    made = []
    if plt:
        for c in (1, 16):
            fig, ax = plt.subplots(figsize=(4.8, 4.0))
            heatmap(ax, g(c, "mean_us", 1 / 1000),
                    f"Mean latency, concurrency {c}", "ms  ·  darker = slower")
            fig.tight_layout()
            p = os.path.join(a.out_dir, f"fig_heat_latency_C{c}.png")
            fig.savefig(p, dpi=200, facecolor=SURFACE); plt.close(fig); made.append(p)

        fig, ax = plt.subplots(figsize=(4.8, 4.0))
        heatmap(ax, g(16, "qps"), "Throughput, concurrency 16", "QPS  ·  darker = faster", fmt="%.0f")
        fig.tight_layout()
        p = os.path.join(a.out_dir, "fig_heat_qps_C16.png")
        fig.savefig(p, dpi=200, facecolor=SURFACE); plt.close(fig); made.append(p)

        # traffic = RX + TX per query, summed over every Milvus pod
        for c in (1, 16):
            grid = [[(float(d[(c, s, q)]["rx_kb_per_query_total"]) +
                      float(d[(c, s, q)]["tx_kb_per_query_total"]))
                     if (c, s, q) in d else float("nan") for s in S_VALS] for q in Q_VALS]
            fig, ax = plt.subplots(figsize=(4.8, 4.0))
            heatmap(ax, grid, f"Traffic per query, concurrency {c}",
                    "KB (RX+TX)  ·  darker = more", fmt="%.1f")
            fig.tight_layout()
            p = os.path.join(a.out_dir, f"fig_heat_traffic_C{c}.png")
            fig.savefig(p, dpi=200, facecolor=SURFACE); plt.close(fig); made.append(p)

        # Q=1 S-sweep, C1 vs C16. Two series -> categorical slots 1 and 2, legend AND
        # direct labels (identity never by colour alone).
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.6))
        for i, c in enumerate((1, 16)):
            y = [float(d[(c, s, 1)]["mean_us"]) / 1000 if (c, s, 1) in d else float("nan")
                 for s in S_VALS]
            ax1.plot(S_VALS, y, color=SERIES[i], lw=2.0, marker="o", ms=8,
                     mec=SURFACE, mew=2, label=f"C={c}", zorder=3)
            ax1.annotate(f"C={c}", (S_VALS[-1], y[-1]), textcoords="offset points",
                         xytext=(6, 0), color=INK2, fontsize=9, va="center")
            y2 = [float(d[(c, s, 1)]["qps"]) if (c, s, 1) in d else float("nan") for s in S_VALS]
            ax2.plot(S_VALS, y2, color=SERIES[i], lw=2.0, marker="o", ms=8,
                     mec=SURFACE, mew=2, label=f"C={c}", zorder=3)
            ax2.annotate(f"C={c}", (S_VALS[-1], y2[-1]), textcoords="offset points",
                         xytext=(6, 0), color=INK2, fontsize=9, va="center")
        for ax, yl, t in ((ax1, "mean latency (ms)", "Latency vs S at Q=1"),
                          (ax2, "QPS", "Throughput vs S at Q=1")):
            style(ax); ax.grid(True, axis="y", color=GRID, lw=0.8, zorder=0); ax.set_axisbelow(True)
            ax.set_xscale("log", base=2); ax.set_xticks(S_VALS)
            ax.set_xticklabels(S_VALS); ax.set_xlabel("S  (shards)"); ax.set_ylabel(yl)
            ax.set_title(t, fontsize=11, loc="left"); ax.margins(x=0.16)
            ax.legend(frameon=False, fontsize=9, labelcolor=INK2)
        fig.tight_layout()
        p = os.path.join(a.out_dir, "fig_line_Q1_C1_vs_C16.png")
        fig.savefig(p, dpi=200, facecolor=SURFACE); plt.close(fig); made.append(p)

    # ---- tables
    hdr = ["C", "S (set)", "Q (set)", "S meas", "Q meas (N/shard)", "M meas",
           "mean (ms)", "p50", "p95", "p99", "QPS", "RX KB/q", "TX KB/q", "recall", "err"]
    rows = []
    for c in (1, 16):
        for s in S_VALS:
            for q in Q_VALS:
                r = d.get((c, s, q))
                if not r:
                    continue
                rows.append([c, s, q, r["actual_shards_touched"], r["actual_N_per_shard_avg"],
                             r["actual_M_per_worker_per_shard"],
                             "%.3f" % (float(r["mean_us"]) / 1000),
                             "%.3f" % (float(r["p50_us"]) / 1000),
                             "%.3f" % (float(r["p95_us"]) / 1000),
                             "%.3f" % (float(r["p99_us"]) / 1000),
                             "%.0f" % float(r["qps"]),
                             r["rx_kb_per_query_total"], r["tx_kb_per_query_total"],
                             r["recall_at_k"], r["errors"]])
    with open(os.path.join(a.out_dir, "table_sxq.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(hdr); w.writerows(rows)

    def md(h, rr):
        o = ["| " + " | ".join(map(str, h)) + " |", "|" + "|".join("---" for _ in h) + "|"]
        o += ["| " + " | ".join(map(str, x)) + " |" for x in rr]
        return "\n".join(o) + "\n"

    # Q effect at fixed (C, S), normalised to Q=1
    eff = []
    for c in (1, 16):
        for s in S_VALS:
            base = d.get((c, s, 1))
            if not base:
                continue
            for q in Q_VALS:
                r = d.get((c, s, q))
                if not r:
                    continue
                eff.append([c, s, q,
                            "%.3f" % (float(r["mean_us"]) / 1000),
                            "%.2fx" % (float(r["mean_us"]) / float(base["mean_us"])),
                            "%.0f" % float(r["qps"]),
                            "%.2fx" % (float(r["qps"]) / float(base["qps"])),
                            "%.2f" % (float(r["rx_kb_per_query_total"]) + float(r["tx_kb_per_query_total"])),
                            "%.2fx" % ((float(r["rx_kb_per_query_total"]) + float(r["tx_kb_per_query_total"])) /
                                       (float(base["rx_kb_per_query_total"]) + float(base["tx_kb_per_query_total"])))])
    with open(os.path.join(a.out_dir, "REPORT_SXQ.md"), "w") as fh:
        fh.write("# S x Q sweep (Q = QueryNode pods)\n\n")
        fh.write("Source: `results/runs.csv`, rows matching `^C(1|16)_S(1|4|16)_Q(1|2|4)$` only.\n"
                 "The earlier concurrency ladder (`*_N2_M2`) and the first S/N/M matrix\n"
                 "(`S*_N*_M*`) are excluded. runs.csv was not modified.\n\n")
        fh.write("Fixed: deep1m 1M x 96 L2, HNSW M=16/efConstruction=200, ef=64, topK=10,\n"
                 "NQ=1, 4000 measured queries after 300 warmup, chunks=4.\n\n")
        fh.write("## Table A. every cell\n\n" + md(hdr, rows))
        fh.write("\n`Q meas` is the measured QueryNodes-per-shard, not the pod count; "
                 "`M meas` drifts below its true value at C=16 because the counters are read "
                 "at the window edges with thousands of requests in flight.\n")
        fh.write("\n## Table B. effect of Q, normalised to Q=1 within each (C, S)\n\n")
        fh.write(md(["C", "S", "Q", "mean (ms)", "vs Q=1", "QPS", "vs Q=1",
                     "traffic KB/q", "vs Q=1"], eff))
        if made:
            fh.write("\n## Figures\n\n")
            for p in made:
                fh.write(f"![{os.path.basename(p)}]({os.path.basename(p)})\n\n")
    print("wrote:")
    for p in made + [os.path.join(a.out_dir, x) for x in ("table_sxq.csv", "REPORT_SXQ.md")]:
        print("  " + p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
