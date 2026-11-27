#!/usr/bin/env python3
"""Paper format: x = latency gain (%) vs Tree with no ONode limit (B=5, M=128),
left y = Recall@10 loss (%p) vs Full PG scan, right y = candidate reduction (%) vs Full PG.
Each point is one ONode budget. Latency = median over 3 reps of the per-run mean latency
(query-concurrency 64, clean fig7-knee-94 sweep); x error bars = min-max of the 3 reps.
Also writes a two-panel variant (same data, no dual axis) and results/latency_gain.csv."""
import re, csv, statistics
import matplotlib.pyplot as plt
# 2026-10-14: larger text
plt.rcParams.update({"font.size": 16, "axes.labelsize": 16, "xtick.labelsize": 15, "ytick.labelsize": 15})
L = "/home/dev_path/experiments/latency-94"
FR, FC = 0.9461, 209481.708   # Full PG M=128 (fig7-knee-94/results/raw.txt fullpg_m128)
# Source: clean fig7-knee-94 sweep (10:38-11:12Z, host otherwise idle; Fig 7 conditions,
# query-concurrency 64). The concurrency-1 re-measure in results/raw.txt overlapped the Vespa
# 100M feed on the OSD cores and is NOT used.
K = "/home/dev_path/experiments/fig7-knee-94/results/raw.txt"
OSET = {64, 72, 76, 80, 84, 88, 92, 96, 100}
runs = {}
for ln in open(K):
    tag = ln.split()[0]
    m = re.match(r"b5_o(\d+)(_r\d)?$", tag) or re.match(r"b8_o(72|64)()$", tag)
    if not m: continue  # O72/O64 only measured at B=8 (B5 == B8 below O96: e.g. O80 182155 vs 182157)
    kv = dict(re.findall(r" ([a-z0-9_]+)=([-0-9.e]+)", ln))
    runs.setdefault(int(m[1]), []).append(dict(lat=float(kv["logical_latency_us_avg"]) / 1e3,
        p50=float(kv["logical_latency_us_p50"]) / 1e3, p99=float(kv["logical_latency_us_p99"]) / 1e3,
        R=float(kv["recall_at_top_k"]), C=float(kv["avg_total_matching_records_per_query"])))
# ONode >= 116 and unlimited run the identical search (same candidates/recall/results) -> baseline reps
base_runs = [r for o, rr in runs.items() for r in rr if abs(r["C"] - 205280.726) < 1e-3]
runs = {o: rr for o, rr in runs.items() if o in OSET}
runs[0] = base_runs
for o, rr in runs.items():
    assert len({(r["R"], r["C"]) for r in rr}) == 1, f"O{o} not deterministic"
base = statistics.median(r["lat"] for r in runs[0])
rows = []
for o, rr in sorted(runs.items(), key=lambda x: -(x[0] or 10**9)):
    lats = [r["lat"] for r in rr]
    med = statistics.median(lats)
    rows.append(dict(onode="unlimited (O>=116, 5 runs)" if o == 0 else o, reps=len(rr), lat_ms_median=round(med, 2),
        lat_ms_min=round(min(lats), 2), lat_ms_max=round(max(lats), 2),
        p50_ms_median=round(statistics.median(r["p50"] for r in rr), 2),
        p99_ms_median=round(statistics.median(r["p99"] for r in rr), 2),
        latency_gain_pct=round((base - med) / base * 100, 2),
        gain_lo=round((base - max(lats)) / base * 100, 2), gain_hi=round((base - min(lats)) / base * 100, 2),
        recall=rr[0]["R"], candidates=round(rr[0]["C"], 1),
        recall_loss_pp=round((FR - rr[0]["R"]) * 100, 2), cand_reduction_pct=round((FC - rr[0]["C"]) / FC * 100, 2)))
with open(f"{L}/results/latency_gain.csv", "w", newline="") as f:
    w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)
for r in rows: print(r)
x = [r["latency_gain_pct"] for r in rows]
xerr = [[r["latency_gain_pct"] - r["gain_lo"] for r in rows], [r["gain_hi"] - r["latency_gain_pct"] for r in rows]]
loss = [r["recall_loss_pp"] for r in rows]; red = [r["cand_reduction_pct"] for r in rows]
C1, C2 = "#1f77b4", "#ff7f0e"

# dual-axis (paper format): candidate reduction = bars, recall loss = line.
# 2026-10-14: place points on evenly spaced slots 1 2 4 8 16 32 (%), each slot taking
# the measured point closest to it. Measured gains differ from the slot value (8 <- 6.8, 32 <- 25.9;
# O80/O76 at 12.5 are dropped) -> axis says "Target", actual values go in the caption / CSV.
# Both y axes use the same 0-32 scale so the two series are directly comparable.
SLOT = {1: 96, 2: 100, 4: 88, 8: 84, 16: 72, 32: 64}  # slot (%) -> ONode budget (72/64 measured at B=8)
byo = {r["onode"]: i for i, r in enumerate(rows)}
sel = [byo[o] for o in SLOT.values()]
with open(f"{L}/results/latency_gain_slots.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["slot_pct", "onode", "actual_latency_gain_pct", "recall_loss_pp", "cand_reduction_pct"])
    for t, i in zip(SLOT, sel): w.writerow([t, rows[i]["onode"], x[i], loss[i], red[i]])
pos = range(len(SLOT))
fig, ax = plt.subplots(figsize=(5.4, 3.6))
ax2 = ax.twinx()
ax.set_zorder(ax2.get_zorder() + 1); ax.patch.set_visible(False)  # line drawn above the bars
ax2.bar(pos, [red[i] for i in sel], width=0.55, color=C2, alpha=0.8, label="Candidate reduction (%)")
ax.plot(pos, [loss[i] for i in sel], color=C1, marker="o", ms=5.5, lw=1.8, label="Recall loss (%)")
ax.set_xticks(list(pos)); ax.set_xticklabels([str(t) for t in SLOT])
ax.set_xlabel("Target latency reduction (%)")
ax.set_ylabel("Recall@10 loss (%)")
ax2.set_ylabel("Candidate reduction (%)")
for z in (ax, ax2): z.set_ylim(0, 32); z.set_yticks(range(0, 31, 5))
ax.grid(axis="y", ls="--", alpha=0.35)
h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, frameon=False, loc="upper left", fontsize=13)
fig.subplots_adjust(left=0.15, right=0.85, bottom=0.2, top=0.96)
for e in ("pdf", "png"): plt.savefig(f"{L}/figures/latency_gain_dualaxis_target.{e}", dpi=300)

# two-panel variant (same data)
fig, (a, b) = plt.subplots(2, 1, figsize=(5.0, 5.6), sharex=True)
a.errorbar(x, loss, xerr=xerr, color=C1, marker="o", ms=5, lw=1.8, capsize=2, elinewidth=0.8)
b.plot(x, red, color=C2, marker="s", ms=5, lw=1.8)
a.set_ylabel("Recall loss (%)"); b.set_ylabel("Cand. reduction (%)")
b.set_xlabel("Latency Reduction (%)")
for z in (a, b): z.grid(axis="y", ls="--", alpha=0.35); z.set_ylim(bottom=0)
fig.subplots_adjust(left=0.18, right=0.97, bottom=0.12, top=0.97, hspace=0.12)
fig.align_ylabels((a, b))
for e in ("pdf", "png"): plt.savefig(f"{L}/figures/latency_gain_twopanel.{e}", dpi=300)
print("saved")

# swapped variant (2026-10-14: swap x and secondary y): x = candidate reduction slots
# 2 4 8 16 32 (closest measured point; no point near 1), left y = recall loss, right y = latency
# reduction bars, both y on the same 0-32 scale. Slot 2 = baseline (ONode unlimited: 2.0 %, latency 0).
SLOT2 = {2: "unlimited (O>=116, 5 runs)", 4: 96, 8: 88, 16: 76, 32: 64}
sel2 = [byo[o] for o in SLOT2.values()]
with open(f"{L}/results/latency_gain_swapped_slots.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["slot_pct", "onode", "actual_cand_reduction_pct", "recall_loss_pp", "latency_gain_pct"])
    for t, i in zip(SLOT2, sel2): w.writerow([t, rows[i]["onode"], red[i], loss[i], x[i]])
pos = range(len(SLOT2))
fig, ax = plt.subplots(figsize=(5.4, 3.6))
ax2 = ax.twinx()
ax.set_zorder(ax2.get_zorder() + 1); ax.patch.set_visible(False)
ax2.bar(pos, [x[i] for i in sel2], width=0.55, color=C2, alpha=0.8, label="Latency reduction (%)")
ax.plot(pos, [loss[i] for i in sel2], color=C1, marker="o", ms=5.5, lw=1.8, label="Recall loss (%)")
ax.set_xticks(list(pos)); ax.set_xticklabels([str(t) for t in SLOT2])
ax.set_xlabel("Candidate reduction (%)")
ax.set_ylabel("Recall@10 loss (%)")
ax2.set_ylabel("Latency reduction (%)")
for z in (ax, ax2): z.set_ylim(0, 32); z.set_yticks(range(0, 31, 5))
ax.grid(axis="y", ls="--", alpha=0.35)
h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, frameon=False, loc="upper left", fontsize=13)
fig.subplots_adjust(left=0.15, right=0.85, bottom=0.2, top=0.96)
for e in ("pdf", "png"): plt.savefig(f"{L}/figures/latency_gain_dualaxis.{e}", dpi=300)  # main figure (was _swapped)
