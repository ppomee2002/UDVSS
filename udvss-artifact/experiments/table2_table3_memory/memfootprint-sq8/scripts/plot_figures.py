#!/usr/bin/env python3
"""Three figures for the FP32 vs SQ8 memory study (all values read from raw rows).

 fig1_max_stable_N.png   Maximum Tested Stable Dataset Size under Memory Limit
 fig2_norm_qps.png       QPS / baseline QPS vs N   (rows: 4 GiB, 8 GiB; cols: FP32, SQ8)
 fig3_p99.png            p99 latency vs N (log)   (same layout)

Rules (fig1): stable = query ran with no failure and (Qdrant) no page-cache thrashing
(> 1000 major faults/s in the query window). The line stops at the last stable N.
If every tested N was stable the point is a lower bound, drawn hollow with a "≥".
Ceph has no single run covering every budget:
  * "Ceph smp8"  : first Ceph run, 8 reactors, OMAP on, seastar cap 16/8/4 GiB,
                   stable if ANY cache setting ran (default, ½, ¼ … of the per-reactor cap)
  * "Ceph smp2"  : reference settings at 4 GiB (RANDOM_BLOCK, cbjournal 1.2 GiB,
                   cache 500 MiB), OMAP on / OMAP off
Data tables used for the plots are written next to the figures as CSV.
"""
import csv, json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
matplotlib.rcParams["pdf.fonttype"] = 42      # embed TrueType: text stays selectable/editable
PDF_ALL = PdfPages(f"/home/dev_path/experiments/memfootprint-sq8/results/figures/figures_all.pdf")

ROOT = "/home/dev_path/experiments/memfootprint-sq8"
OUT = f"{ROOT}/results/figures"
os.makedirs(OUT, exist_ok=True)
TAG_OF = {4: "p3-4g", 8: "p2-8g", 16: "p4-16g", 32: "p1-32g"}
COL = {"Milvus": "#1f77b4", "Qdrant": "#d62728", "Vespa": "#2ca02c",
       "Ceph smp8": "#9467bd", "Ceph smp2 OMAP off": "#ff7f0e", "Ceph smp2 OMAP on": "#8c564b"}

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------- pods ------
pods = list(csv.DictReader(open(f"{ROOT}/results/final_table.csv")))

def pod_series(system, rep, budget):
    """ascending list of dicts: N, qps, p99, status(ok|thrash|fail), fail_kind"""
    rs = [r for r in pods if r["tag"] == TAG_OF[budget] and r["system"] == system
          and r["representation"] == rep]
    out = []
    for r in sorted(rs, key=lambda r: int(r["N"])):
        st = "fail" if r["failure"] else ("thrash" if r.get("pressure") == "thrashing" else "ok")
        out.append(dict(N=int(r["N"]), qps=f(r["qps"]), p99=f(r["p99_ms"]), status=st,
                        kind=r["failure"] or ("thrashing" if st == "thrash" else "")))
    return out

# ---------------------------------------------------------------- ceph ------
def ceph_rows(tag, arm):
    p = f"{ROOT}/ceph/results/{tag}/{arm}/rows.jsonl"
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

def ceph_smp2_series(omap, arm):
    tags = ["prof12"] if omap == "on" else ["noomap", "noomap_ext"]
    by = {}
    for t in tags:
        for d in ceph_rows(t, arm):
            if d["phase"] == "query" or d["failure"]:
                n = d["N"]
                ok = d["phase"] == "query" and not d["failure"] and f(d["qps"])
                if n not in by or ok:
                    by[n] = dict(N=n, qps=f(d["qps"]) if ok else None,
                                 p99=f(d["lat_p99_ms"]) if ok else None,
                                 status="ok" if ok else "fail", kind=d["failure"] or "")
    return [by[n] for n in sorted(by)]

def ceph_smp8_series(arm, cap, cache_pick=None):
    """per N: ok if any cache config produced a clean query at this cap (fig1);
    cache_pick restricts to one cache label (fig2/3 curves)."""
    by = {}
    for d in ceph_rows("main", arm):
        if d.get("cap") != cap:
            continue
        if cache_pick and not str(d.get("cache_pershard", "")).startswith(cache_pick):
            continue
        n = d["N"]
        ok = d["phase"] == "query" and not d["failure"] and f(d["qps"])
        if d["phase"] not in ("query", "stable") and not d["failure"]:
            continue
        cur = by.get(n)
        if ok:
            by[n] = dict(N=n, qps=f(d["qps"]), p99=f(d["lat_p99_ms"]), status="ok", kind="",
                         cache=d["cache_pershard"])
        elif cur is None:
            by[n] = dict(N=n, qps=None, p99=None, status="fail", kind=d["failure"] or "")
    return [by[n] for n in sorted(by)]

# -------------------------------------------------------- fig1 data --------
def max_stable(series):
    last, cut = 0, None
    for s in series:
        if s["status"] == "ok":
            last = s["N"]
        else:
            cut = s
            break
    tested = max((s["N"] for s in series), default=0)
    lower_bound = cut is None
    return last, lower_bound, (cut["kind"] if cut else ""), tested

fig1 = []
for rep, ceph_arm in (("FP32", "fp32"), ("SQ8", "int8")):
    for system in ("Milvus", "Qdrant", "Vespa"):
        for b in (4, 8, 16, 32):
            ser = pod_series(system.lower(), rep, b)
            if ser:
                n, lb, kind, tested = max_stable(ser)
                fig1.append(dict(rep=rep, series=system, budget=b, N=n, lower_bound=lb,
                                 cut_by=kind, max_tested=tested))
    for b, cap in ((4, "4G"), (8, "8G"), (16, "16G")):
        ser = ceph_smp8_series(ceph_arm, cap)
        if ser:
            n, lb, kind, tested = max_stable(ser)
            fig1.append(dict(rep=rep, series="Ceph smp8", budget=b, N=n, lower_bound=lb,
                             cut_by=kind, max_tested=tested))
    for omap in ("on", "off"):
        ser = ceph_smp2_series(omap, ceph_arm)
        n, lb, kind, tested = max_stable(ser)
        fig1.append(dict(rep=rep, series=f"Ceph smp2 OMAP {omap}", budget=4, N=n, lower_bound=lb,
                         cut_by=kind, max_tested=tested))
with open(f"{OUT}/fig1_data.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(fig1[0])); w.writeheader(); w.writerows(fig1)

# ------------------------------------------------------------- fig1 --------
JIT = {"Milvus": -0.10, "Qdrant": -0.035, "Vespa": 0.035, "Ceph smp8": 0.10,
       "Ceph smp2 OMAP on": -0.16, "Ceph smp2 OMAP off": 0.16}
fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), sharey=True)
handles = {}
LBL = {}
for ax, rep in zip(axes, ("FP32", "SQ8")):
    for series in ("Milvus", "Qdrant", "Vespa", "Ceph smp8", "Ceph smp2 OMAP on", "Ceph smp2 OMAP off"):
        pts = sorted([p for p in fig1 if p["rep"] == rep and p["series"] == series],
                     key=lambda p: p["budget"])
        if not pts:
            continue
        # a lower bound below an already-reached N (32 GiB phase tested only to 4M) carries no
        # information about the trend: draw it isolated, not connected
        line, iso, best = [], [], 0
        for p in pts:
            if p["lower_bound"] and p["N"] < best:
                iso.append(p)
            else:
                line.append(p); best = max(best, p["N"])
        X = lambda p: p["budget"] * 2 ** JIT[series]
        if len(line) > 1:
            h, = ax.plot([X(p) for p in line], [p["N"] / 1e6 for p in line], "-",
                         color=COL[series], lw=1.8, alpha=0.85)
        mk = "s" if series.startswith("Ceph smp2") else "o"
        for p in line + iso:
            y = p["N"] / 1e6
            h2, = ax.plot(X(p), y, mk, ms=8, color=COL[series],
                          mfc="white" if p["lower_bound"] else COL[series], mew=2,
                          alpha=0.45 if p in iso else 1.0)
            handles.setdefault(series, h2)
            txt = ("≥" if p["lower_bound"] else "") + f"{y:g}M" + ("*" if p in iso else "")
            LBL.setdefault((rep, p["budget"], txt), []).append((series, X(p), y))
    SHORT = {"Milvus": "Mil", "Qdrant": "Qd", "Vespa": "Ves", "Ceph smp8": "Ceph8",
             "Ceph smp2 OMAP on": "Ceph2 on", "Ceph smp2 OMAP off": "Ceph2 off"}
    for (r_, b_, txt), members in LBL.items():
        if r_ != rep:
            continue
        xr = max(m[1] for m in members); y = members[0][2]
        if len(members) == 1:
            ax.annotate(txt, (xr, y), textcoords="offset points", xytext=(6, -3),
                        fontsize=7.5, color=COL[members[0][0]])
        else:
            ax.annotate(txt + " (" + ", ".join(SHORT[m[0]] for m in members) + ")", (xr, y),
                        textcoords="offset points", xytext=(6, 4), fontsize=7.2, color="black")
    ax.set_xscale("log", base=2); ax.set_xticks([4, 8, 16, 32]); ax.set_xticklabels(["4", "8", "16", "32"])
    ax.set_xlim(3.0, 50)
    ax.set_yscale("log"); ax.set_ylim(0.18, 16)
    ax.set_yticks([0.25, 0.5, 1, 2, 4, 6, 10]); ax.set_yticklabels(["0.25", "0.5", "1", "2", "4", "6", "10"])
    ax.set_xlabel("Memory budget (GiB)"); ax.grid(True, which="major", alpha=0.3)
    ax.set_title("FP32" if rep == "FP32" else "SQ8 / int8")
axes[0].set_ylabel("Max tested stable N (M vectors, log)")
labels = {"Ceph smp2 OMAP on": "Ceph smp2 OMAP on (4 GiB only)", "Ceph smp2 OMAP off": "Ceph smp2 OMAP off (4 GiB only)"}
fig.legend([handles[k] for k in handles], [labels.get(k, k) for k in handles],
           loc="lower center", ncol=6, fontsize=8.5, bbox_to_anchor=(0.5, -0.02), frameon=False)
fig.suptitle("Maximum Tested Stable Dataset Size under Memory Limit  (cohere 768-d)", fontsize=12)
fig.text(0.5, -0.12,
         "Filled = last stable N before thrashing / load_rejected / OOM / bad_alloc;  hollow + '≥' = no failure up to the largest tested N.  "
         "* faded = 32 GiB phase was only run up to 4M (not a ceiling).\n"
         "Pods (Milvus/Qdrant/Vespa): cgroup limit, 8 CPU, HNSW M=30.  Ceph: seastar cap (crimson_memory), pivot ll3; smp8 = 8 reactors, OMAP on, best cache size;  "
         "smp2 = reference config (RANDOM_BLOCK, cbjournal 1.2 GiB, cache 500 MiB).\n"
         "Step sizes differ (pods 0.25M@4G, 0.5M@8G, 1M@16G; Ceph 0.25/0.5/1/2/4/6/8M), so the true limit lies between the plotted point and the next step.  "
         "Markers are offset slightly in x per system so equal values stay visible.",
         ha="center", fontsize=7.5)
fig.tight_layout(rect=(0, 0.06, 1, 1))
fig.savefig(f"{OUT}/fig1_max_stable_N.png", dpi=160, bbox_inches="tight")
fig.savefig(f"{OUT}/fig1_max_stable_N.pdf", bbox_inches="tight")
PDF_ALL.savefig(fig, bbox_inches="tight")

# -------------------------------------------------------- fig2/3 data -------
def curves(budget, rep, ceph_arm):
    cs = {}
    for system in ("Milvus", "Qdrant", "Vespa"):
        cs[system] = pod_series(system.lower(), rep, budget)
    if budget == 4:
        cs["Ceph smp2 OMAP off"] = ceph_smp2_series("off", ceph_arm)
        cs["Ceph smp2 OMAP on"] = ceph_smp2_series("on", ceph_arm)
    if budget == 8:
        cs["Ceph smp8"] = ceph_smp8_series(ceph_arm, "8G", cache_pick="0.500G")
    return cs

rows23 = []
for b in (4, 8):
    for rep, arm in (("FP32", "fp32"), ("SQ8", "int8")):
        for name, ser in curves(b, rep, arm).items():
            base = next((s["qps"] for s in ser if s["status"] == "ok" and s["qps"]), None)
            for s in ser:
                rows23.append(dict(budget=b, rep=rep, series=name, N=s["N"], status=s["status"],
                                   kind=s["kind"], qps=s["qps"], p99_ms=s["p99"],
                                   norm_qps=(s["qps"] / base) if (s["qps"] and base) else None))
with open(f"{OUT}/fig2_fig3_data.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows23[0])); w.writeheader(); w.writerows(rows23)

def draw(metric, fname, ylabel, title, logy, note):
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), sharex=False)
    for i, b in enumerate((4, 8)):
        for j, rep in enumerate(("FP32", "SQ8")):
            ax = axes[i][j]
            names = []
            for r in rows23:
                if r["budget"] == b and r["rep"] == rep and r["series"] not in names:
                    names.append(r["series"])
            for name in names:
                pts = [r for r in rows23 if r["budget"] == b and r["rep"] == rep and r["series"] == name]
                ok = [r for r in pts if r[metric] is not None]
                ls = "--" if name == "Ceph smp2 OMAP on" else "-"
                lab = name + (" (per 32-query batch)" if (metric == "p99_ms" and name.startswith("Ceph")) else "")
                ax.plot([r["N"] / 1e6 for r in ok], [r[metric] for r in ok], ls, marker="o", ms=4,
                        color=COL[name], label=lab, lw=1.6)
                th = [r for r in ok if r["status"] == "thrash"]
                if th:
                    ax.plot([r["N"] / 1e6 for r in th], [r[metric] for r in th], "o", ms=8,
                            mfc="none", mec=COL[name], mew=1.5)
                fl = [r for r in pts if r["status"] == "fail"]
                if fl and ok:
                    ybot = ax.get_ylim()[0] if not logy else min(r[metric] for r in ok)
                    ax.plot([r["N"] / 1e6 for r in fl[:1]], [ok[-1][metric] if logy else 0][:1], "x",
                            ms=10, mew=2.5, color=COL[name])
            ax.set_xscale("log", base=2)
            ax.set_xticks([0.25, 0.5, 1, 2, 4, 6, 8, 10])
            ax.set_xticklabels(["0.25", "0.5", "1", "2", "4", "6", "8", "10"])
            if logy:
                ax.set_yscale("log")
            else:
                vals = [r[metric] for r in rows23 if r["budget"] == b and r["rep"] == rep and r[metric] is not None]
                ax.set_ylim(-0.05, max(1.3, max(vals) * 1.08))
                ax.axhline(1.0, color="grey", lw=0.8, ls=":")
                # 1/N reference from the smallest Ceph N: a full-PG scan does ~N/1024*M rows
                # per query, so Ceph QPS falls ~1/N even without memory pressure
                cephs = [r for r in rows23 if r["budget"] == b and r["rep"] == rep and r["series"].startswith("Ceph")]
                if cephs:
                    n0 = min(r["N"] for r in cephs) / 1e6
                    xs = [x for x in (0.25, 0.5, 1, 2, 4, 6, 8) if x >= n0]
                    ax.plot(xs, [n0 / x for x in xs], ":", color="black", lw=1.2,
                            label="1/N (Ceph full-PG scan cost)")
            ax.grid(True, alpha=0.3)
            ax.set_title(f"{b} GiB budget — {rep}")
            ax.set_xlabel("N (M vectors)"); ax.set_ylabel(ylabel)
            ax.legend(fontsize=7, loc="best")
    fig.suptitle(title, fontsize=12)
    fig.text(0.5, -0.01, note, ha="center", va="top", fontsize=8)
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(f"{OUT}/{fname}", dpi=160, bbox_inches="tight")
    fig.savefig(f"{OUT}/{fname[:-4]}.pdf", bbox_inches="tight")
    PDF_ALL.savefig(fig, bbox_inches="tight")

COMMON_NOTE = ("x = first failing N (OOM / load_rejected / bad_alloc).  Open circle = Qdrant page-cache thrashing (>1000 major faults/s).\n"
               "Ceph smp2 = reference config at 4 GiB (smp 2); Ceph smp8 at 8 GiB = first Ceph run with cache = 1/2 of the per-reactor cap.\n")
NOTE2 = COMMON_NOTE + (
    "Ceph scans every row of the 16 probed PGs, so its work per query grows ~linearly with N: compare Ceph curves with the dotted 1/N line\n"
    "(below it = loss beyond scan cost, e.g. cache misses / GC). HNSW (pods) grows ~log N.  x is drawn at y=0.\n"
    "Pod QPS is noisy: query phases were serialized, but other pods could be building indexes at the same time, so a baseline taken under\n"
    "contention can make later points exceed 1 (e.g. Vespa SQ8 8 GiB). The 8 GiB pod series start at 0.5M (no 0.25M step).")
NOTE3 = COMMON_NOTE + (
    "x is drawn at the previous point's p99.  Pods: latency of one query (16 concurrent clients).\n"
    "Ceph: latency of one 32-query fan-out batch at concurrency 40 on 2 (smp2) or 8 (smp8) reactors, so its absolute p99 is NOT comparable\n"
    "with the pods; read its slope (growth with N) instead.")

draw("norm_qps", "fig2_norm_qps.png", "QPS / QPS at smallest N",
     "Normalized QPS vs Dataset Size under a fixed memory budget", logy=False, note=NOTE2)
draw("p99_ms", "fig3_p99.png", "p99 latency (ms, log)",
     "p99 Latency vs Dataset Size under a fixed memory budget", logy=True, note=NOTE3)
PDF_ALL.close()
print("\n".join(f"{p['rep']:4} {p['series']:20} {p['budget']:>2}G  {'≥' if p['lower_bound'] else ' '}{p['N']/1e6:g}M  cut_by={p['cut_by']}  max_tested={p['max_tested']/1e6:g}M" for p in fig1))
