#!/usr/bin/env python3
"""Paper-style figures for the FP32 vs SQ8 memory study, from measured rows only.

Style mirrors /home/dev_path/lsh-selective-results-20260827/plot_selective.py
(font 12 / ticks 10.5 / legend 9, 1.8pt lines, markersize 5, unboxed legends,
dashed y-grid, constrained layout, vector PDF + 300 dpi PNG; experimental
conditions live in figures_paper/README.md, not on the figures).

Stability rule (per user spec):
  stable  = failure empty AND pressure != thrashing AND a query QPS exists
  last stable N  = last stable point before the first unstable one
  censored (≥)   = no unstable point was measured (test upper bound reached)
Unstable reasons are kept distinct: OOMKilled, load_rejected, thrashing, bad_alloc.

Series:
  Milvus / Qdrant / Vespa : k3s pods, cgroup memory limit 4/8/16/32 GiB, 8 CPU
  Ceph (smp=2)            : crimson OSD, reference settings, crimson_memory 4 GiB,
                            OMAP on and OMAP off (per-vector OMAP identity key skipped)
  Ceph (smp=8)            : first Ceph run (OMAP on) — in the verification table only,
                            not plotted (different CPU/device configuration)
No value is interpolated or estimated.
"""
import csv, json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = "/home/dev_path/experiments/memfootprint-sq8"
OUT = f"{ROOT}/results/figures_paper"
os.makedirs(OUT, exist_ok=True)
TAG_OF = {4: "p3-4g", 8: "p2-8g", 16: "p4-16g", 32: "p1-32g"}

plt.rcParams.update({"font.size": 12, "axes.titlesize": 12, "axes.labelsize": 12,
                     "xtick.labelsize": 10.5, "ytick.labelsize": 10.5, "legend.fontsize": 9,
                     "pdf.fonttype": 42, "ps.fonttype": 42})

SPEC = {  # label, color, marker, linestyle
    "Milvus":            ("Milvus",              "#1f77b4", "o", "-"),
    "Qdrant":            ("Qdrant",              "#ff7f0e", "s", "-"),
    "Vespa":             ("Vespa",               "#2ca02c", "^", "-"),
    "Ceph smp2 OMAP on": ("Ceph (smp=2, OMAP on)",  "#9467bd", "D", "--"),
    "Ceph smp2 OMAP off": ("Ceph (smp=2, OMAP off)", "#d62728", "v", "-"),
}
REASON_MK = {"OOMKilled": "x", "load_rejected": "1", "thrashing": "d", "bad_alloc": "+"}

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def reason_of(kind):
    k = (kind or "").lower()
    if "oom" in k:
        return "OOMKilled"
    if "load_rejected" in k:
        return "load_rejected"
    if "thrash" in k:
        return "thrashing"
    if "bad_alloc" in k or k in ("osd_died", "osd_died_at_start", "malloc_failed"):
        return "bad_alloc"
    return kind or ""

# ------------------------------------------------------------------ data -----
pods = list(csv.DictReader(open(f"{ROOT}/results/final_table.csv")))

def pod_points(system, rep, limit):
    rs = [r for r in pods if r["tag"] == TAG_OF[limit] and r["system"] == system
          and r["representation"] == rep]
    pts = []
    for r in sorted(rs, key=lambda r: int(r["N"])):
        thr = r.get("pressure") == "thrashing"
        stable = (not r["failure"]) and (not thr) and f(r["qps"]) is not None
        reason = reason_of(r["failure"]) if r["failure"] else ("thrashing" if thr else "")
        ok_meas = not r["failure"]          # a failed run's QPS=0 is not a measurement
        pts.append(dict(N=int(r["N"]), qps=f(r["qps"]) if ok_meas else None,
                        p99=f(r["p99_ms"]) if ok_meas else None, stable=stable,
                        reason=reason, pressure_measured=r.get("majfault_per_s_query") not in ("", None)))
    return pts

def ceph_points(tags, arm, cap=None, cache_any=False):
    by = {}
    for t in tags:
        p = f"{ROOT}/ceph/results/{t}/{arm}/rows.jsonl"
        if not os.path.exists(p):
            continue
        for d in map(json.loads, open(p)):
            if cap and d.get("cap") != cap:
                continue
            if d["phase"] != "query" and not d["failure"]:
                continue
            n = d["N"]
            ok = d["phase"] == "query" and not d["failure"] and f(d["qps"]) is not None
            if ok:
                by[n] = dict(N=n, qps=f(d["qps"]), p99=f(d["lat_p99_ms"]), stable=True, reason="")
            elif n not in by or (not by[n]["stable"] and not cache_any):
                by.setdefault(n, dict(N=n, qps=None, p99=None, stable=False,
                                      reason=reason_of(d["failure"])))
    return [by[n] for n in sorted(by)]

def summarize(pts):
    last, first_bad = None, None
    for p in pts:
        if p["stable"]:
            last = p
        else:
            first_bad = p
            break
    return dict(last=last["N"] if last else 0,
                first_unstable=first_bad["N"] if first_bad else None,
                reason=first_bad["reason"] if first_bad else "",
                censored=first_bad is None,
                max_tested=max(p["N"] for p in pts) if pts else 0)

SERIES = {}   # (series, rep, limit) -> points
for rep, arm in (("FP32", "fp32"), ("SQ8", "int8")):
    for sysn in ("Milvus", "Qdrant", "Vespa"):
        for lim in (4, 8, 16, 32):
            pts = pod_points(sysn.lower(), rep, lim)
            if pts:
                SERIES[(sysn, rep, lim)] = pts
    SERIES[("Ceph smp2 OMAP on", rep, 4)] = ceph_points(["prof12"], arm)
    SERIES[("Ceph smp2 OMAP off", rep, 4)] = ceph_points(["noomap", "noomap_ext"], arm)
CEPH8 = {}
for rep, arm in (("FP32", "fp32"), ("SQ8", "int8")):
    for lim, cap in ((2, "2G"), (4, "4G"), (8, "8G"), (16, "16G")):
        CEPH8[("Ceph smp8 OMAP on", rep, lim)] = ceph_points(["main"], arm, cap=cap, cache_any=True)

# ---------------------------------------------------- verification table -----
def mfmt(n):
    return "–" if n in (None, 0) else (f"{n/1e6:g}M")

table = []
for (s, rep, lim), pts in list(SERIES.items()) + list(CEPH8.items()):
    sm = summarize(pts)
    note = ""
    if s in ("Milvus", "Qdrant", "Vespa") and lim == 32:
        note = "32 GiB phase ran only up to 4M; thrashing not instrumented in this phase"
    if s.startswith("Ceph smp8"):
        note = "not plotted (smp=8, default SSD device); stable if any cache size ran"
    table.append(dict(System=SPEC.get(s, (s,))[0] if s in SPEC else "Ceph (smp=8, OMAP on)",
                      Representation=rep if rep == "FP32" else "SQ8/int8",
                      MemoryLimit=f"{lim} GiB", LastStableN=mfmt(sm["last"]),
                      FirstUnstableN=mfmt(sm["first_unstable"]),
                      UnstableReason=sm["reason"] or "–",
                      Censored="≥ (yes)" if sm["censored"] else "no",
                      MaxTestedN=mfmt(sm["max_tested"]), Note=note))
order = {"Milvus": 0, "Qdrant": 1, "Vespa": 2, "Ceph (smp=2, OMAP on)": 3,
         "Ceph (smp=2, OMAP off)": 4, "Ceph (smp=8, OMAP on)": 5}
table.sort(key=lambda r: (order[r["System"]], r["Representation"], int(r["MemoryLimit"].split()[0])))
with open(f"{OUT}/capacity_table.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(table[0])); w.writeheader(); w.writerows(table)
md = ["| System | Representation | Memory limit | Last stable N | First unstable N | Unstable reason | Censored(≥) | Max tested N | Note |",
      "|---|---|---|---|---|---|---|---|---|"]
for r in table:
    md.append("| " + " | ".join(r[k] for k in ("System", "Representation", "MemoryLimit", "LastStableN",
              "FirstUnstableN", "UnstableReason", "Censored", "MaxTestedN", "Note")) + " |")
open(f"{OUT}/capacity_table.md", "w").write("\n".join(md) + "\n")
print("\n".join(md))

# ------------------------------------------------------------- helpers -------
def save(fig, name):
    fig.savefig(f"{OUT}/{name}.pdf")
    fig.savefig(f"{OUT}/{name}.png", dpi=300)
    plt.close(fig)
    print(f"Saved {OUT}/{name}.pdf / .png")

def legend(ax, **kw):
    return ax.legend(frameon=False, handlelength=1.5, handletextpad=.4, labelspacing=.25,
                     borderaxespad=.2, **kw)

def gridy(ax, xl, yl):
    ax.set_xlabel(xl); ax.set_ylabel(yl); ax.grid(axis="y", linestyle="--", alpha=.35)

NTICKS = [0.25, 0.5, 1, 2, 4, 6, 10]
def nticks_x(ax, hi=10):
    t = [x for x in NTICKS if x <= hi]
    ax.set_xscale("log", base=2); ax.set_xticks(t, labels=[f"{x:g}" for x in t]); ax.minorticks_off()

reason_handles = [Line2D([], [], color="black", marker=REASON_MK[k], linestyle="None", markersize=6,
                         markerfacecolor="none" if k == "thrashing" else "black", label=k)
                  for k in ("load_rejected", "OOMKilled", "thrashing", "bad_alloc")]

# --------------------------------------------------------------- Figure 1 ----
JIT = {"Milvus": -0.09, "Qdrant": -0.03, "Vespa": 0.03, "Ceph smp2 OMAP on": -0.12, "Ceph smp2 OMAP off": 0.12}
fig, axes = plt.subplots(1, 2, figsize=(6.8, 4.3), layout="constrained", sharey=True)
for ax, rep, title in zip(axes, ("FP32", "SQ8"), ("(a) FP32", "(b) SQ8 / int8")):
    labels = {}
    for s, (lab, col, mk, ls) in SPEC.items():
        lims = sorted(l for (ss, r, l) in SERIES if ss == s and r == rep)
        if not lims:
            continue
        sums = {l: summarize(SERIES[(s, rep, l)]) for l in lims}
        X = lambda l: l * 2 ** JIT[s]
        # a censored point BELOW an N already reached at a smaller limit (32 GiB phase was
        # only run up to 4M) is not a capacity: shown faded, unconnected, marked '*'
        line, iso, best = [], [], 0
        for l in lims:
            sm = sums[l]
            if not sm["last"]:
                continue
            if sm["censored"] and sm["last"] <= best:
                iso.append(l)
            else:
                line.append(l); best = max(best, sm["last"])
        ax.plot([X(l) for l in line], [sums[l]["last"] / 1e6 for l in line], color=col,
                linestyle=ls if len(line) > 1 else "None", linewidth=1.8, marker=mk, markersize=5, label=lab)
        for l in line + iso:
            sm = sums[l]; y = sm["last"] / 1e6
            if sm["censored"]:
                ax.plot(X(l), y, marker=mk, markersize=7, color=col, markerfacecolor="white",
                        linestyle="None", alpha=.4 if l in iso else 1)
                labels.setdefault((l, f"≥{y:g}M" + ("*" if l in iso else "")), []).append((col, X(l), y))
            elif sm["first_unstable"]:
                ax.plot(X(l), sm["first_unstable"] / 1e6, marker=REASON_MK.get(sm["reason"], "x"),
                        markersize=5.5, color=col, linestyle="None", markeredgewidth=1.4,
                        markerfacecolor="none" if sm["reason"] == "thrashing" else col)
    for (l, txt), mem in labels.items():
        xr = max(m[1] for m in mem)
        ax.annotate(txt, (xr, mem[0][2]), textcoords="offset points",
                    xytext=(5, 4) if txt.endswith("*") else (5, -10), fontsize=7.5,
                    color=mem[0][0] if len(mem) == 1 else "black")
    ax.set_xscale("log", base=2); ax.set_xticks([4, 8, 16, 32], labels=["4", "8", "16", "32"]); ax.minorticks_off()
    ax.set_xlim(3.3, 48)
    ax.set_yscale("log", base=2); ax.set_yticks(NTICKS, labels=[f"{t:g}" for t in NTICKS]); ax.minorticks_off()
    ax.set_ylim(0.2, 14)
    gridy(ax, "Memory limit (GiB)", "Max tested stable N (M vectors)" if rep == "FP32" else "")
    ax.set_title(title)
h, l = axes[0].get_legend_handles_labels()
cens = Line2D([], [], color="grey", marker="o", markerfacecolor="white", linestyle="None",
              label="≥ no failure up to max tested N")
fig.legend(h + reason_handles + [cens], l + [r.get_label() for r in reason_handles] + [cens.get_label()],
           loc="outside lower center", ncol=4, frameon=False, fontsize=7.5, handlelength=1.5,
           columnspacing=1.0, handletextpad=.4)
save(fig, "fig1_capacity_vs_memory_limit")

# ------------------------------------------------------- Figure 2 / 3 --------
def perf_figure(metric, name, ylabel, logy):
    fig, axes = plt.subplots(2, 3, figsize=(10.2, 5.8), layout="constrained", sharex=True)
    letters = iter("abcdef")
    for i, rep in enumerate(("FP32", "SQ8")):
        for j, lim in enumerate((4, 8, 16)):
            ax = axes[i][j]
            ymax = 0
            for s, (lab, col, mk, ls) in SPEC.items():
                pts = SERIES.get((s, rep, lim))
                if not pts:
                    continue
                base = next((p for p in pts if p["stable"]), None)
                if base is None:
                    continue
                key = "qps" if metric == "norm" else "p99"
                meas = [p for p in pts if p[key] is not None]   # only measured values; no gaps filled
                if metric == "norm":
                    ys = [100 * p["qps"] / base["qps"] for p in meas]
                else:
                    ys = [p["p99"] for p in meas]
                lab2 = lab + (" †" if (metric == "p99" and s.startswith("Ceph")) else "")
                ax.plot([p["N"] / 1e6 for p in meas], ys, color=col, linestyle=ls, linewidth=1.8,
                        marker=mk, markersize=4, label=lab2)
                ymax = max([ymax] + ys)
                thr = [(p, y) for p, y in zip(meas, ys) if p["reason"] == "thrashing"]
                if thr:   # first thrashing point gets the marker; later ones are drawn hollow
                    p0, y0 = thr[0]
                    ax.plot(p0["N"] / 1e6, y0, marker="d", markersize=9, markerfacecolor="none",
                            markeredgecolor="black", linestyle="None")
                    ax.plot([p["N"] / 1e6 for p, _ in thr], [y for _, y in thr], linestyle="None",
                            marker=mk, markersize=4, markerfacecolor="white", markeredgecolor=col)
                bad = next((p for p in pts if not p["stable"] and p["reason"] != "thrashing"), None)
                if bad:
                    yb = (0 if metric == "norm" else ys[-1])
                    ax.plot(bad["N"] / 1e6, yb, marker=REASON_MK.get(bad["reason"], "x"), markersize=7,
                            color=col, linestyle="None", markeredgewidth=1.8)
            nticks_x(ax)
            if logy:
                import matplotlib.ticker as mt
                ax.set_yscale("log")
                ax.yaxis.set_major_locator(mt.LogLocator(base=10, subs=(1, 2, 5)))
                ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 1 else f"{v:g}"))
                ax.yaxis.set_minor_locator(mt.NullLocator())
            else:
                ax.set_ylim(-5, max(130, ymax * 1.08)); ax.axhline(100, color="grey", lw=.8, ls=":")
            gridy(ax, "Dataset size (M vectors)" if i == 1 else "",
                  ylabel if j == 0 else "")
            ax.set_title(f"({next(letters)}) {rep if rep == 'FP32' else 'SQ8 / int8'}, {lim} GiB")
    h, l = [], []
    for ax in axes.flat:
        for hh, ll in zip(*ax.get_legend_handles_labels()):
            if ll not in l:
                h.append(hh); l.append(ll)
    extra = [Line2D([], [], color="black", marker="d", markerfacecolor="none", markersize=8, linestyle="None",
                    label="thrashing onset")] + [r for r in reason_handles if r.get_label() != "thrashing"]
    fig.legend(h + extra, l + [e.get_label() for e in extra], loc="outside lower center", ncol=5,
               frameon=False, fontsize=8.5, handlelength=1.5, columnspacing=1.2)
    save(fig, name)

perf_figure("norm", "fig2_normalized_qps", "Normalized QPS (%)", logy=False)
perf_figure("p99", "fig3_p99_latency", "p99 latency (ms)", logy=True)
