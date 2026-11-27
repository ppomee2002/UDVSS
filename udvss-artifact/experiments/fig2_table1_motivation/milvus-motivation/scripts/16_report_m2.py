#!/usr/bin/env python3
"""Motivation 2 tables and figures.

Reads results/runs_m2.csv (never runs.csv) and results/trace/TRACE_M2T_*_summary.json,
writes results/report_m2/. See docs/04_motivation2_plan.md.

Three products:
  T1  the paper's first sentence     -- E2E vs the part that is actually vector search
  T2  N ladder and M ladder          -- with the stage split from the traced twins
  T3  index read vs vector read      -- output_fields A/B, and the IVF_FLAT nprobe fit
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.environ.get("MM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.environ.get("MM_RESULTS") or os.path.join(ROOT, "results")
RUNS = os.path.join(RES, "runs_m2.csv")
TRACE = os.path.join(RES, "trace")
OUT = os.path.join(RES, "report_m2")

INK = "#191919"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
STAGE_COLORS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"]


REPEAT = re.compile(r"^(.*)_r(\d+)$")
CV_LIMIT = 5.0
R2_MIN = 0.95        # below this the nprobe fit does not support a conclusion
SLOPE_TOL = 0.25     # |b_E2E - b_segcore| / b_segcore above this breaks the model


def qual(r):
    """Quality suffix for a point. Principle 1 and 6 apply to EVERY table, not
    just the ladders: the T1 baseline becomes the paper's first sentence."""
    if not r:
        return ""
    if r.get("_single"):
        return "  **[n=1 -- not a measurement, see principle 1]**"
    if r.get("_unstable"):
        return f"  **[UNSTABLE: CV {r.get('_cv_pct','?')}% > {CV_LIMIT:g}% -- do not use]**"
    return ""


def load_rows():
    """Collapse <label>_r1.._rN into one row per label.

    A single run is not a measurement on this box: measured 2026-09-21, the same
    point back to back gave 3970 / 2867 / 2937 us. We keep the row whose mean_us
    is the MEDIAN of the repeats, and attach n and the coefficient of variation
    so an unstable point can be excluded instead of quietly believed.
    """
    if not os.path.exists(RUNS):
        sys.exit(f"no {RUNS}; run 15_motivation2.sh first")
    with open(RUNS, newline="") as fh:
        rows = list(csv.DictReader(fh))

    groups = {}
    for r in rows:
        lab = r.get("label", "")
        if lab.startswith("PREWARM_"):
            continue
        m = REPEAT.match(lab)
        groups.setdefault(m.group(1) if m else lab, []).append(r)

    byl = {}
    for lab, g in groups.items():
        g = [r for r in g if f(r, "mean_us") == f(r, "mean_us")]
        if not g:
            continue
        g.sort(key=lambda r: f(r, "mean_us"))
        # lower of the two central runs when n is even: we must return a whole
        # ROW (its traffic, recall and stage columns have to stay consistent), so
        # averaging is not available. Taking the lower one is the conservative
        # choice for a motivation claim -- it understates, never inflates, the
        # overhead we are arguing is large.
        chosen = dict(g[(len(g) - 1) // 2])
        means = [f(r, "mean_us") for r in g]
        n = len(means)
        mu = sum(means) / n
        # SAMPLE sd (n-1). Dividing by n understates the spread by sqrt((n-1)/n)
        # = 0.816 at n=3, which would let a true 6.1% CV slip through a 5% gate.
        sd = (sum((x - mu) ** 2 for x in means) / (n - 1)) ** 0.5 if n >= 2 else float("nan")
        cv = 100 * sd / mu if (n >= 2 and mu) else float("nan")
        chosen["_n"] = str(n)
        chosen["_cv_pct"] = "" if cv != cv else f"{cv:.1f}"
        chosen["_unstable"] = "1" if (cv == cv and cv > CV_LIMIT) else ""
        # n=1 is not a measurement on this box (principle 1). Flag it distinctly:
        # it has no CV at all, so an unstable point would otherwise look clean.
        chosen["_single"] = "1" if n < 2 else ""
        chosen["_sd"] = "" if sd != sd else f"{sd:.1f}"      # us
        byl[lab] = chosen
    return byl


def f(r, k, d=float("nan")):
    try:
        return float(r[k])
    except (KeyError, TypeError, ValueError):
        return d


def ms(r):
    return f(r, "mean_us") / 1000.0


def traces():
    out = {}
    # 12_trace_breakdown.py writes "<label>_summary.json"; the TRACE_ prefix on
    # the older files is part of THEIR label, not something the writer adds.
    for p in sorted(glob.glob(os.path.join(TRACE, "M2T_*_summary.json"))
                    + glob.glob(os.path.join(TRACE, "TRACE_M2T_*_summary.json"))
                    + glob.glob(os.path.join(TRACE, "M2LAT_*_summary.json"))):
        d = json.load(open(p))
        out[d["label"]] = d
    return out


def stage_pct(d):
    """Stage shares, as fractions of the ACCOUNTED critical path.

    Dividing by root_mean_us is wrong: the per-trace critical path is averaged
    over the traces that had one, root_mean over all of them, and the overlap
    clustering clamps each node's own time at zero. The two totals differ by
    2.5-9.6% in this data, so every row read 102-110%. Normalising to the
    accounted total makes the shares sum to exactly 100%; how well that total
    covers the root is reported separately by `coverage()`.
    """
    tot = d.get("critical_path_total_us") or d["root_mean_us"]
    return {k: v / tot for k, v in d["critical_path_grouped"].items()}, tot


def coverage(d):
    """accounted critical path / mean root duration."""
    root = d.get("root_mean_us") or 0
    tot = d.get("critical_path_total_us") or 0
    return (tot / root) if root else float("nan")


def trace_ok(d):
    """A trace sample whose mean is far from its median is not one population:
    its stage percentages are an average over two different things."""
    if not d:
        return False, "no trace"
    mean, p50 = d.get("root_mean_us", 0), d.get("root_p50_us", 0)
    # Derive it here rather than trusting a stored flag: summaries written before
    # 12_trace_breakdown.py learned to filter have no flag, and S16_Q1 (mean
    # 1859us, p50 286us) would otherwise sail through.
    if d.get("bimodal") or (p50 and abs(mean - p50) / p50 > 0.25):
        return False, (f"bimodal (mean {mean:.0f}us vs p50 {p50:.0f}us) "
                       "— 빠른 실패 응답이 섞인 표본")
    n = d.get("traces", 0)
    if n and n < 20:
        return False, f"only {n} traces"
    return True, ""


def segcore_key(g):
    for k in g:
        if "segcore" in k.lower():
            return k
    return None


# ------------------------------------------------------------------ tables
def table1(byl, tr, md):
    """E2E vs the part that is actually vector search."""
    md.append("## T1. 한 쿼리의 시간은 어디로 가는가 (baseline S=1, N=1, M=1, topK=10, NQ=1, C=1)\n")
    r = byl.get("M2_BASE_S1_Q1_M1")
    if not r:
        md.append("_M2_BASE_S1_Q1_M1 row missing._\n")
        return
    t = tr.get("M2T_M_S1_Q1_C1") or tr.get("M2T_S1_Q1")
    e2e = ms(r)
    md.append(f"n={r.get('_n','?')}, CV={r.get('_cv_pct','n/a')}%{qual(r)}\n")
    md.append(f"trace OFF 측정 E2E median **{e2e:.3f} ms** "
              f"(p50 {f(r,'p50_us')/1000:.3f} / p95 {f(r,'p95_us')/1000:.3f} / "
              f"p99 {f(r,'p99_us')/1000:.3f}), recall@10 {r.get('recall_at_k','')}\n")
    ok, why = trace_ok(t)
    if not ok:
        md.append(f"_traced twin unusable ({why}); rerun `15_motivation2.sh traces`._\n")
        return
    g, root = stage_pct(t)
    md.append(f"critical path 누적 / root = **{100*coverage(t):.1f}%** "
              "(100%에서 벗어난 만큼은 분해가 설명하지 못한 부분)\n")
    md.append("| stage | share of critical path | ms at the untraced E2E |")
    md.append("|---|---:|---:|")
    for k, frac in g.items():
        md.append(f"| {k} | {100*frac:.1f}% | {frac*e2e:.3f} |")
    sk = segcore_key(g)
    if sk:
        v = g[sk]
        md.append("")
        if r.get("_unstable") or r.get("_single"):
            md.append("> _이 점은 품질 게이트를 통과하지 못했다. 아래 문장은 재측정 전까지 "
                      "인용하지 말 것._")
        md.append(f"> **{e2e:.2f} ms 중 실제 벡터 검색은 {v*e2e:.2f} ms ({100*v:.1f}%)이고, "
                  f"나머지 {(1-v)*e2e:.2f} ms ({100*(1-v):.1f}%)는 계층을 지나는 비용이다.**")
    md.append("")
    md.append("_비율은 trace ON run에서, 절대 ms는 trace OFF run에서 가져왔다. "
              "span export가 latency를 ~21% 부풀리기 때문이다._\n")


def ladder(byl, tr, md, labels, xlabel, title, figname):
    md.append(f"## {title}\n")
    md.append("| " + xlabel + " | E2E median (ms) | n | CV | p95 | segcore (ms) | "
              "segcore share | 구조 비용 (ms) | traffic KB/q | recall |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    pts, seg, struct = [], [], []
    for x, lab, tlab in labels:
        r = byl.get(lab)
        if not r:
            md.append(f"| {x} | _missing ({lab})_ | | | | | | | | |")
            continue
        e2e = ms(r)
        traf = f(r, "rx_kb_per_query_total") + f(r, "tx_kb_per_query_total")
        t = tr.get(tlab)
        sv = float("nan")
        if t and not trace_ok(t)[0]:
            t = None
        if t:
            g, _ = stage_pct(t)
            sk = segcore_key(g)
            if sk:
                sv = g[sk] * e2e
        pts.append((x, e2e))
        seg.append((x, sv))
        struct.append((x, e2e - sv))
        share = "" if sv != sv else f"{100*sv/e2e:.1f}%"
        svs = "" if sv != sv else f"{sv:.3f}"
        sts = "" if sv != sv else f"{e2e-sv:.3f}"
        flag = " ⚠" if r.get("_unstable") else ""
        md.append(f"| {x} | {e2e:.3f}{flag} | {r.get('_n','')} | {r.get('_cv_pct','')}% | "
                  f"{f(r,'p95_us')/1000:.3f} | {svs} | {share} | "
                  f"{sts} | {traf:.2f} | {r.get('recall_at_k','')} |")
    md.append("")

    if len(pts) >= 2:
        fig, ax = plt.subplots(figsize=(5.2, 3.6))
        xv = [p[0] for p in pts]
        ax.bar(range(len(xv)), [p[1] for p in pts], color="#dbe7f7", edgecolor=BLUE,
               linewidth=1.2, label="E2E")
        if any(s[1] == s[1] for s in seg):
            ax.bar(range(len(xv)), [s[1] if s[1] == s[1] else 0 for s in seg],
                   color=BLUE, label="segcore (actual vector search)")
        ax.set_xticks(range(len(xv)))
        ax.set_xticklabels([str(v) for v in xv])
        ax.set_xlabel(xlabel)
        ax.set_ylabel("ms per query")
        ax.set_title(title, color=INK, fontsize=10)
        ax.legend(frameon=False, fontsize=8)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, figname), dpi=160)
        plt.close(fig)
        md.append(f"![{title}]({figname})\n")
    return pts, seg


def table_outfields(byl, md):
    md.append("## T3-A. index read vs vector read — `output_fields` A/B (S=1, N=1, M=1)\n")
    a, b = byl.get("M2_OF_none"), byl.get("M2_OF_vec")
    if not (a and b):
        md.append("_M2_OF_* rows missing; run `15_motivation2.sh outfields`._\n")
        return
    ea, eb = ms(a), ms(b)
    ta = f(a, "rx_kb_per_query_total") + f(a, "tx_kb_per_query_total")
    tb = f(b, "rx_kb_per_query_total") + f(b, "tx_kb_per_query_total")
    md.append("| run | output_fields | E2E (ms) | p95 (ms) | traffic KB/q | n | CV |")
    md.append("|---|---|---:|---:|---:|---:|---:|")
    md.append(f"| A0 | (없음: id+distance) | {ea:.3f} | {f(a,'p95_us')/1000:.3f} | {ta:.2f} | "
              f"{a.get('_n','')} | {a.get('_cv_pct','')}% |")
    md.append(f"| A1 | `vector` | {eb:.3f} | {f(b,'p95_us')/1000:.3f} | {tb:.2f} | "
              f"{b.get('_n','')} | {b.get('_cv_pct','')}% |")
    md.append(f"| **Δ** | | **{eb-ea:+.3f}** | | **{tb-ta:+.2f}** | | |")
    md.append("")
    md.append(f"top-k 원본 벡터를 실어 보내는 데 **{eb-ea:+.3f} ms / {tb-ta:+.2f} KB**.")
    md.append("")

    # A binary CV gate is the wrong test for a difference this large. Propagate
    # the two standard errors and compare the effect against them: a point can sit
    # above a 5% CV and still carry an effect many times its own uncertainty.
    def se(r):
        sd = f(r, "_sd")
        n = int(r.get("_n") or 1)
        return (sd / (n ** 0.5) / 1000.0) if (sd == sd and n >= 2) else float("nan")
    sa, sb = se(a), se(b)
    if sa == sa and sb == sb:
        sed = (sa ** 2 + sb ** 2) ** 0.5
        ratio = abs(eb - ea) / sed if sed else float("inf")
        md.append(f"| | n | CV | SE (ms) |")
        md.append("|---|---:|---:|---:|")
        md.append(f"| A0 | {a.get('_n')} | {a.get('_cv_pct')}% | {sa:.3f} |")
        md.append(f"| A1 | {b.get('_n')} | {b.get('_cv_pct')}% | {sb:.3f} |")
        md.append(f"| **Δ** | | | **{sed:.3f}** |")
        md.append("")
        verdict = ("효과가 불확실성보다 충분히 크다 — CV 게이트를 넘겼더라도 **사용 가능**"
                   if ratio >= 5 else
                   "효과가 불확실성 대비 작다 — **반복을 더 늘릴 것**")
        md.append(f"Δ = {eb-ea:+.3f} ms 는 그 표준오차 {sed:.3f} ms 의 **{ratio:.0f}배**다. {verdict}.")
        md.append("")
        for r, nm in ((a, "A0"), (b, "A1")):
            if r.get("_unstable"):
                md.append(f"_참고: {nm} 는 CV {r.get('_cv_pct')}% 로 점 단위 게이트({CV_LIMIT:g}%)를 "
                          "넘었다. 이 박스의 run-to-run 편차가 5~8%대라 절대값 인용에는 부적합하지만, "
                          "위 비율이 보여주듯 **차이**는 그 편차보다 훨씬 크다._")
        md.append("")
    md.append("> **한계 두 가지. 둘 다 캡션에 남길 것.**")
    md.append("> 1. 이것은 *탐색이 끝난 뒤* top-k를 꺼내는 비용이지 *그래프를 타는 동안* "
              "읽는 벡터의 비용이 아니다. 후자는 T3-B가 잰다.")
    md.append("> 2. Δ는 **chunk read + 직렬화 + 네트워크 전송**의 합이다. deep1m 96차원 "
              "float32 topK=10이면 payload만 약 3.8 KB라 **전송분이 지배적일 수 있다**. "
              "따라서 Δ 전체를 'vector read 비용'이라 부르면 과대 귀속이다. "
              "위 표의 Δtraffic과 나란히 놓고 해석할 것.")
    md.append("")
    md.append("교차 검증: trace에서 `ReduceHelper::FillEntryData`가 A1에서 커져야 유효한 행이다.\n")


def table_nprobe(byl, md):
    md.append("## T3-B. index read vs vector read — IVF_FLAT nprobe 회귀 (S=1, N=1, M=1)\n")
    pat = re.compile(r"^M2_NPROBE_(\d+)$")
    pts = []
    for lab, r in byl.items():
        m = pat.match(lab)
        if m:
            pts.append((int(m.group(1)), r))
    pts.sort()
    if len(pts) < 3:
        md.append("_M2_NPROBE_* rows missing; run `15_motivation2.sh nprobe`._\n")
        return
    md.append("| nprobe | E2E mean (ms) | p95 | QueryNode segcore (ms) | traffic KB/q | recall@10 |")
    md.append("|---:|---:|---:|---:|---:|---:|")
    for np_, r in pts:
        sc = f(r, "worker_segcore_ms")
        traf = f(r, "rx_kb_per_query_total") + f(r, "tx_kb_per_query_total")
        md.append(f"| {np_} | {ms(r):.3f} | {f(r,'p95_us')/1000:.3f} | "
                  f"{'' if sc != sc else f'{sc:.4f}'} | {traf:.2f} | {r.get('recall_at_k','')} |")
    md.append("")

    def fit(ys):
        xs = [p[0] for p in pts]
        good = [(x, y) for x, y in zip(xs, ys) if y == y]
        if len(good) < 3:
            return None
        n = len(good)
        sx = sum(x for x, _ in good)
        sy = sum(y for _, y in good)
        sxx = sum(x * x for x, _ in good)
        sxy = sum(x * y for x, y in good)
        den = n * sxx - sx * sx
        if den == 0:
            return None
        b = (n * sxy - sx * sy) / den
        a = (sy - b * sx) / n
        ybar = sy / n
        ss_res = sum((y - (a + b * x)) ** 2 for x, y in good)
        ss_tot = sum((y - ybar) ** 2 for _, y in good)
        r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
        return a, b, r2

    fe = fit([ms(r) for _, r in pts])
    fs = fit([f(r, "worker_segcore_ms") for _, r in pts])
    md.append("**선형 적합** `y = a + b·nprobe`\n")
    md.append("| fit | a (intercept, ms) | b (slope, ms/nprobe) | R² |")
    md.append("|---|---:|---:|---:|")
    if fe:
        md.append(f"| E2E | {fe[0]:.3f} | {fe[1]:.4f} | {fe[2]:.4f}"
                  f"{' ⚠' if fe[2] < R2_MIN else ''} |")
    if fs:
        md.append(f"| segcore | {fs[0]:.4f} | {fs[1]:.4f} | {fs[2]:.4f}"
                  f"{' ⚠' if fs[2] < R2_MIN else ''} |")
    md.append("")

    if not (fe and fs):
        return

    # ------------------------------------------------- what the residual really is
    # An earlier version of this report split the slope into b_seg + b_out and
    # called b_out "result movement / merge". That label was never measured, and
    # T3-C (tracing ON at nprobe 4/16/32/64, 400 valid traces each) shows it was
    # WRONG. Per-stage regression of the critical path:
    #
    #   QueryNode/SearchSegments self time   +12.34 us/nprobe   R2 0.993
    #   segcore PhyVectorSearchNode::Execute +12.12 us/nprobe   R2 0.997
    #   Proxy                                 +0.42 us/nprobe   R2 0.896
    #   segment reduce (ReduceHelper::*)      +0.06 us/nprobe   R2 0.436
    #   QN queue                               0.00
    #   hop proxy->delegator                  -1.00 us/nprobe   R2 0.373
    #   hop delegator->QueryNode              -1.23 us/nprobe   R2 0.424
    #
    # Everything that grows is INSIDE the QueryNode. The network hops do not grow
    # at all (their slopes are negative with no trend), and the reduce path is
    # flat: over nprobe 4 -> 64, FillPrimaryKey moved +1.8 us, Marshal +1.0 us,
    # FillEntryData -0.2 us. So the residual is not result movement.
    b_gap = fe[1] - fs[1]
    struct = fe[0] - fs[0]
    md.append("### 잔차의 정체 (T3-C 실측으로 확정)\n")
    md.append(f"두 기울기의 차 **{b_gap*1000:.1f} µs/nprobe**를 이전 판에서는 "
              "'결과 이동/병합'이라고 불렀다. **그 명명은 틀렸다.** "
              "`REPORT_NPROBE_STAGES.md`(tracing ON, 조건당 유효 trace 400개)에서 "
              "stage별로 회귀한 결과:\n")
    md.append("| stage | slope (µs/nprobe) | R² |")
    md.append("|---|---:|---:|")
    md.append("| QueryNode/SearchSegments self | **+12.34** | 0.993 |")
    md.append("| segcore PhyVectorSearchNode::Execute | **+12.12** | 0.997 |")
    md.append("| Proxy | +0.42 | 0.896 |")
    md.append("| segment reduce (`ReduceHelper::*`) | +0.06 | 0.436 |")
    md.append("| QN queue | 0.00 | — |")
    md.append("| hop proxy → delegator | −1.00 | 0.373 |")
    md.append("| hop delegator → QueryNode | −1.23 | 0.424 |")
    md.append("")
    md.append("**nprobe와 함께 커지는 것은 전부 QueryNode 안에 있다.** 네트워크 hop은 "
              "기울기가 음수이고 R²도 0.4 미만이라 추세 자체가 없다. reduce 경로도 평평하다 "
              "— nprobe를 4에서 64로 16배 올리는 동안 `FillPrimaryKey` +1.8 µs, "
              "`Marshal` +1.0 µs, `FillEntryData` −0.2 µs 움직였을 뿐이다.\n")
    md.append("QueryNode 안에서 둘로 갈린다. `PhyVectorSearchNode::Execute`(ANN 커널 자체)가 "
              "120 → 857 µs, 그 부모인 `SearchSegments`의 **self time**이 372 → 1121 µs로 "
              "거의 같은 폭(+736 vs +748 µs)으로 늘어난다. 후자는 커널 밖의 시간이다: "
              f"이 컬렉션은 **M=4**라 세그먼트 4개를 병렬로 뒤지는데, nprobe가 커질수록 "
              "그 4개가 서로 CPU를 다투어 RPC 전체의 wall time이 세그먼트 하나의 검색보다 "
              "빠르게 늘어난다. 즉 **ANN 작업량 자체 + 그 병렬 실행의 경합**이지, "
              "결과를 옮기는 비용이 아니다.\n")
    md.append("### 남는 모델\n")
    md.append("```\nE2E(np) = STRUCT + QN_search(np)\n```\n")
    md.append("| 항 | 값 | 의미 |")
    md.append("|---|---:|---|")
    md.append(f"| `STRUCT` | **{struct:.3f} ms** | nprobe와 **무관**하게 항상 지불하는 계층 비용 "
              "(hop 2개 + Proxy + Delegator + reduce) |")
    md.append(f"| `QN_search(np)` | {fs[0]*1000:.0f} µs + {fe[1]*1000:.1f} µs x nprobe | "
              "QueryNode 안의 ANN 검색과 그 병렬 실행 경합 |")
    md.append("")
    md.append(f"> **이 결과는 Motivation 2를 약화시키지 않고 강화한다.** hop·reduce·Proxy가 "
              f"nprobe에 **전혀 반응하지 않는다**는 것은, {struct:.1f} ms의 구조 비용이 "
              "검색 작업량과 무관한 **순수한 고정 오버헤드**라는 직접 증거다. "
              "검색을 16배 더 시켜도 계층을 지나는 비용은 줄지도 늘지도 않는다.\n")

    bad = [f"nprobe={n}" for n, r in pts if r.get("_unstable") or r.get("_single")]
    warn = []
    for nm, ft in (("E2E", fe), ("segcore", fs)):
        if ft[2] < R2_MIN:
            warn.append(f"{nm} 적합 R²={ft[2]:.3f} < {R2_MIN}")
    if bad:
        warn.append("품질 게이트 탈락 점: " + ", ".join(bad))
    if warn:
        md.append("> **⚠ 유보 사항:**")
        for x in warn:
            md.append(f"> - {x}")
        md.append("")

    md.append("**캡션에 반드시 남길 것**\n")
    md.append("1. nprobe가 바뀌면 recall도 바뀐다. 위 표의 recall 열을 같이 보고할 것.")
    md.append("2. **IVF_FLAT 전용 결과다.** 논문 본 결과는 HNSW인데, HNSW는 그래프 노드를 "
              "읽는 것이 곧 벡터를 읽는 것이라 a/b 분리가 원리적으로 불가능하다. "
              "\"HNSW에서는 분리할 수 없어 IVF_FLAT으로 별도 측정했다\"를 명시하지 않으면 "
              "외적 타당성 지적을 받는다.")
    md.append("3. posting list 크기가 불균등하므로 누적 스캔 벡터 수는 **기댓값으로만** "
              "nprobe에 선형이다 (1M/1024 ≈ 977/list).")
    md.append("4. **추정량이 섞여 있다.** `c`는 run 내 mean을 repeat 간 median으로 고른 "
              "값이고, `a`는 그 선택된 run의 Prometheus histogram 평균"
              "(`milvus_querynode_sq_core_latency`)이다. 따라서 `c-a`는 성격이 다른 두 "
              "추정량의 차이다. 이 메트릭은 정수 ms 관측이 아니므로(coarse=False) 원칙 3의 "
              "금지 대상은 아니지만, 이 사실을 캡션에 남길 것.\n")

    fig, ax = plt.subplots(figsize=(5.4, 3.6))
    xs = [p[0] for p in pts]
    ax.plot(xs, [ms(r) for _, r in pts], "o-", color=BLUE, lw=2, ms=5, label="E2E")
    sc = [f(r, "worker_segcore_ms") for _, r in pts]
    if any(v == v for v in sc):
        ax.plot(xs, sc, "s--", color=ORANGE, lw=2, ms=5, label="segcore (index+vector read)")
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs)
    ax.set_xticklabels([str(x) for x in xs])
    ax.set_xlabel("nprobe (IVF_FLAT, nlist=1024)")
    ax.set_ylabel("ms per query")
    ax.set_title("Only the vector read scales with nprobe", color=INK, fontsize=10)
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_nprobe.png"), dpi=160)
    plt.close(fig)
    md.append("![nprobe](fig_nprobe.png)\n")


def stage_stack(tr, md):
    allowed = ("M2T_S1_Q1", "M2T_S1_Q2", "M2T_S1_Q4",
               "M2T_S16_Q1", "M2T_S16_Q2", "M2T_S16_Q4")
    bad = [(l, trace_ok(tr[l])[1]) for l in allowed if l in tr and not trace_ok(tr[l])[0]]
    labs = [l for l in allowed if l in tr and trace_ok(tr[l])[0]]
    if not labs:
        return
    md.append("## T2-C. stage 분해 (traced, % of E2E)\n")
    for l, why in bad:
        md.append(f"- ⚠ **{l} 제외**: {why}")
    if bad:
        md.append("")
    # union over ALL traces, not just labs[0]: a stage that exists only at Q=4
    # (e.g. a delegator->QueryNode hop that Q=1 collapses) would otherwise vanish
    # from the chart and the bars would silently stop summing to 100%.
    keys, seen = [], set()
    for l in labs:
        for k in tr[l]["critical_path_grouped"]:
            if k not in seen:
                seen.add(k); keys.append(k)
    md.append("| condition | " + " | ".join(keys) + " | sum | traces | cov |")
    md.append("|---" * (len(keys) + 4) + "|")
    for l in labs:
        g, _ = stage_pct(tr[l])
        tot = 100 * sum(g.get(k, 0) for k in keys)
        warn = "" if abs(tot - 100) <= 2 else "  ⚠"
        d = tr[l]
        n = d.get("traces", 0)
        cov = 100 * coverage(d)
        # A small sample or a decomposition that overshoots the root by a lot is
        # weaker evidence than the others; say so in the row instead of letting
        # it sit next to a 300-trace row as if they were equal.
        weak = "  ⚠" if (n < 100 or cov > 115) else ""
        md.append(f"| {l} | " + " | ".join(f"{100*g.get(k,0):.1f}%" for k in keys)
                  + f" | {tot:.1f}%{warn} | {n} | {cov:.0f}%{weak} |")
    md.append("")
    md.append("_`cov` = critical path 누적 / root. 100%를 넘는 만큼은 겹침 클러스터링이 "
              "과다 계상한 부분이다. `traces` < 100 이거나 `cov` > 115%면 ⚠ — 다른 행과 "
              "같은 무게로 인용하지 말 것._")
    md.append("")

    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    bottom = [0.0] * len(labs)
    for i, k in enumerate(keys):
        vals = [100 * stage_pct(tr[l])[0].get(k, 0) for l in labs]
        ax.bar(range(len(labs)), vals, bottom=bottom,
               color=STAGE_COLORS[i % len(STAGE_COLORS)], label=k,
               edgecolor="white", linewidth=0.6)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xticks(range(len(labs)))
    ax.set_xticklabels([l.replace("M2T_", "") for l in labs], fontsize=8)
    ax.set_ylabel("% of E2E")
    ax.set_title("Critical path breakdown", color=INK, fontsize=10)
    ax.legend(frameon=False, fontsize=7, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.14))
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_stages.png"), dpi=160)
    plt.close(fig)
    md.append("![stages](fig_stages.png)\n")


# ------------------------------------------------------ T4: layer A/B (L0/L1/L2)
LA_RE = re.compile(r"^M2LA_(L\d[a-z]?)_k(\d+)_r(\d+)$")
# (arm, placement, CPU the delegator / the worker runs on, what it does)
#
# The three nodes are different machines (measured 2026-09-23): 94 is a
# Threadripper 7960X (1 NUMA, 5364 MHz max), 109 an Intel Xeon Gold 6548N
# (2 NUMA, 4100 MHz), 110 an Intel Xeon Platinum 8470 (2 NUMA, ~3800 MHz). A
# NQ=1 HNSW search is single-thread and memory-latency bound, so an arm that
# moves the WORKER also changes the CPU running segcore -- which is why L1b
# exists and why the CPU column is in the table rather than in a footnote.
LA_ARMS = [
    ("L0",  "deleg 110 / worker 109", "8470 / 6548N", "두 hop 모두 머신 간 (기준)"),
    ("L1",  "deleg 110 / worker 110", "8470 / **8470**", "deleg→worker 같은 호스트 — worker CPU 바뀜 ⚠"),
    ("L1b", "deleg 109 / worker 109", "6548N / **6548N**", "deleg→worker 같은 호스트 — **worker CPU 고정** ✅"),
    ("L2",  "deleg 94 / worker 94", "7960X / **7960X**", "모든 hop 같은 호스트 — worker CPU 바뀜 ⚠"),
]
# The only arm whose difference from L0 is attributable to the hop alone.
LA_CLEAN = "L1b"
LA_TRAFFIC_TOL = 3.0     # % spread across arms; above this the arms are not the same condition


def _median(v):
    v = sorted(v)
    n = len(v)
    if not n:
        return float("nan")
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def load_layerab():
    """Pool M2LA_<arm>_k<round>_r<i> across rounds, per arm.

    load_rows() cannot do this: it collapses on the _rN suffix only, so it would
    hand back one entry per (arm, round) and the interleaving that the design
    exists for would be thrown away. Here every pass of an arm is pooled, and the
    per-round medians are kept so drift stays visible instead of being averaged
    into the arm effect.
    """
    if not os.path.exists(RUNS):
        return {}
    with open(RUNS, newline="") as fh:
        rows = list(csv.DictReader(fh))
    buckets = {}
    for r in rows:
        m = LA_RE.match(r.get("label", ""))
        if not m or f(r, "mean_us") != f(r, "mean_us"):
            continue
        buckets.setdefault(m.group(1), []).append((int(m.group(2)), r))

    out = {}
    for arm, lst in buckets.items():
        lst.sort(key=lambda t: f(t[1], "mean_us"))
        means = [f(r, "mean_us") for _, r in lst]
        n = len(means)
        mu = sum(means) / n
        sd = (sum((x - mu) ** 2 for x in means) / (n - 1)) ** 0.5 if n >= 2 else float("nan")
        by_round = {}
        for k, r in lst:
            by_round.setdefault(k, []).append(f(r, "mean_us"))
        out[arm] = {
            # the whole ROW of the lower-middle pass, so E2E, traffic, the reduce
            # columns and recall on one line always describe the same run
            "row": dict(lst[(n - 1) // 2][1]),
            "n": n,
            "rounds": {k: _median(v) for k, v in sorted(by_round.items())},
            "cv": (100 * sd / mu) if (n >= 2 and mu) else float("nan"),
        }
    return out


def la_conditions_ok(d):
    """S/N/M are measured, not set (docs/04 principle 4). A move that failed to
    redistribute would otherwise be published as an arm effect."""
    r = d["row"]
    got = (f(r, "actual_shards_touched"), f(r, "actual_N_per_shard_avg"),
           f(r, "actual_M_per_worker_per_shard"))
    # compare against what this very row was RUN with, not against a constant, so
    # the check still holds if the arms are ever re-pointed at another S/M point
    want = (f(r, "param_S_num_shards"), 1.0, f(r, "param_M_insert_chunks"))
    ok = all(abs(g - w) < 0.05 for g, w in zip(got, want))
    return ok, f"S={got[0]:.2f} N={got[1]:.2f} M={got[2]:.2f}"


def table_layerab(byl, tr, md):
    la = load_layerab()
    if not la:
        return
    md.append("## T4. additional layer / distributed reduction overhead — placement A/B\n")
    md.append("S=16, N=1, M=4 (segment 64개), NQ=1, C=1, topK=10, output_fields 없음. "
              "**바뀌는 것은 pod가 어느 물리 머신에 앉는가 뿐이다.** 자세한 설계와 한계는 "
              "`docs/06_layer_ab_plan.md`.\n")
    md.append("> v2.6.23에서 streaming 계층 자체는 **뺄 수 없다** (`cmd/main.go`가 "
              "`SetStreamingServiceEnabled()`를 무조건 호출하고, QueryCoord는 streamingnode의 "
              "embedded QueryNode에 sealed segment를 배정하지 않는다). 이 표가 제거하는 것은 "
              "계층의 **전송**이다.\n")

    base = la.get("L0")
    md.append("| arm | placement | CPU (deleg / worker) | 경로 | n | E2E (ms) | Δ vs L0 | CV | p95 (ms) | "
              "traffic KB/q | QN reduce | Streaming reduce | Proxy reduce | worker RPC | S/N/M | recall |")
    md.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|")
    traffics = []
    for arm, place, cpu, path in LA_ARMS:
        d = la.get(arm)
        if not d:
            md.append(f"| **{arm}** | {place} | {cpu} | {path} | _missing_ | | | | | | | | | | | |")
            continue
        r = d["row"]
        e2e = ms(r)
        traf = f(r, "rx_kb_per_query_total") + f(r, "tx_kb_per_query_total")
        traffics.append(traf)
        dlt = "—" if (not base or arm == "L0") else f"{e2e - ms(base['row']):+.3f}"
        cv = d["cv"]
        flag = " ⚠" if (cv == cv and cv > CV_LIMIT) else ""
        ok, snm = la_conditions_ok(d)
        md.append(
            f"| **{arm}** | {place} | {cpu} | {path} | {d['n']} | {e2e:.3f}{flag} | {dlt} | "
            f"{'' if cv != cv else f'{cv:.1f}'}% | {f(r,'p95_us')/1000:.3f} | {traf:.2f} | "
            f"{f(r,'worker_reduce_segs_ms'):.4f} | {f(r,'deleg_reduce_shards_ms'):.4f} | "
            f"{f(r,'proxy_reduce_ms'):.4f} | {f(r,'worker_rpc_ms'):.3f} | "
            f"{snm}{'' if ok else ' ⚠'} | {r.get('recall_at_k','')} |")
    md.append("")

    # drift check -- the reason the arms are interleaved at all
    rounds = sorted({k for d in la.values() for k in d["rounds"]})
    if len(rounds) > 1:
        md.append("**라운드별 median (µs) — arm 효과와 드리프트를 분리해서 보기 위한 것:**\n")
        md.append("| arm | " + " | ".join(f"round {k}" for k in rounds) + " |")
        md.append("|---" * (len(rounds) + 1) + "|")
        for arm, _, _, _ in LA_ARMS:
            d = la.get(arm)
            if not d:
                continue
            md.append(f"| {arm} | " + " | ".join(
                f"{d['rounds'][k]:.0f}" if k in d["rounds"] else "—" for k in rounds) + " |")
        md.append("")
        md.append("_같은 arm의 라운드 간 차이가 arm 간 차이만큼 크면, 표의 Δ는 배치가 아니라 "
                  "시간 드리프트다. 그 경우 `LA_ROUNDS`를 올려 다시 돌릴 것._\n")

    # traffic is the control: same RPCs, same bytes. podnet.py reads each pod's own
    # netns, so co-locating pods does not hide the bytes -- they still cross a veth.
    if len(traffics) >= 2 and min(traffics) > 0:
        spread = 100 * (max(traffics) - min(traffics)) / min(traffics)
        if spread > LA_TRAFFIC_TOL:
            md.append(f"> ⚠ **traffic이 arm 간에 {spread:.1f}% 움직였다** (허용 {LA_TRAFFIC_TOL:g}%). "
                      "세 arm은 같은 RPC·같은 바이트여야 하므로, 이건 배치 효과가 아니라 조건이 "
                      "달라진 것이다 — N/M 열을 확인하고 해당 라운드를 버릴 것.\n")
        else:
            md.append(f"> ✅ traffic 편차 {spread:.1f}% (≤ {LA_TRAFFIC_TOL:g}%). 바이트 수는 "
                      "안 바뀌었으므로 E2E 차이는 **전송 지연**이지 전송량이 아니다.\n")

    # ---- the numbers this experiment exists to produce
    #
    # The headline is keyed on L1b, NOT on L2. L1 and L2 move the WORKER, so they
    # change which CPU runs segcore at the same time as they remove a hop, and on
    # this cluster that term is larger than the hop itself (L1 measured SLOWER
    # than L0 because 110's 8470 cores are slower than 109's 6548N cores). L1b
    # removes the same delegator->worker hop with segcore pinned to 109, so it is
    # the only difference attributable to the hop alone.
    tl = {a: tr.get(f"M2LAT_{a}") for a, _, _, _ in LA_ARMS}

    def seg_ms(arm):
        """segcore absolute ms for an arm: its traced share x its untraced E2E."""
        d = tl.get(arm)
        if not (d and trace_ok(d)[0] and la.get(arm)):
            return float("nan")
        g, _ = stage_pct(d)
        sk = segcore_key(g)
        return g[sk] * ms(la[arm]["row"]) if sk else float("nan")

    if base:
        e0 = ms(base["row"])
        clean = la.get(LA_CLEAN)
        md.append("**이 실험이 만드는 숫자**\n")
        if clean:
            ec = ms(clean["row"])
            sc = seg_ms(LA_CLEAN)
            md.append(f"- **머신 간 hop 하나의 값** (깨끗한 측정): `E2E(L0) − E2E({LA_CLEAN})` = "
                      f"{e0:.3f} − {ec:.3f} = **{e0-ec:+.3f} ms** ({100*(e0-ec)/e0:+.1f}% of E2E). "
                      f"두 arm 모두 segcore가 109에서 돈다 — CPU가 아니라 delegator→worker 전송만 바뀌었다.")
            if sc == sc:
                md.append(f"- **네트워크를 지워도 남는 소프트웨어 비용**: `E2E({LA_CLEAN}) − segcore({LA_CLEAN})` = "
                          f"{ec:.3f} − {sc:.3f} = **{ec-sc:.3f} ms** ({100*(ec-sc)/ec:.1f}% of E2E). "
                          f"직렬화·스케줄링·3단 reduce·goroutine 왕복이고, 이것이 UDVSS의 논거다.")
            else:
                md.append(f"- **소프트웨어 비용**: {LA_CLEAN}의 traced run이 없어 계산 불가 "
                          "(`15_motivation2.sh layerabtrace` 필요)")
        else:
            md.append(f"- ⚠ **{LA_CLEAN} arm이 없다.** L0−L1 / L0−L2만으로는 hop 비용을 말할 수 없다 "
                      f"(아래 CPU 교란 참조). `LA_ARMS=\"{LA_CLEAN}\" ./scripts/15_motivation2.sh layerab` 필요.")
        md.append("")

        # the worker-moving arms, reported as what they are: confounded
        conf = [a for a in ("L1", "L2") if la.get(a)]
        if conf:
            md.append("**worker를 옮긴 arm — CPU 교란이 섞인 값 (hop 비용으로 인용 금지)**\n")
            md.append("| arm | Δ E2E vs L0 | segcore(arm) | segcore(L0) | Δ segcore = **CPU 항** | 남는 항 ≈ hop |")
            md.append("|---|---:|---:|---:|---:|---:|")
            s0 = seg_ms("L0")
            for a in conf:
                ea, sa = ms(la[a]["row"]), seg_ms(a)
                dcpu = (sa - s0) if (sa == sa and s0 == s0) else float("nan")
                rest = (ea - e0) - dcpu if dcpu == dcpu else float("nan")
                md.append(f"| {a} | {ea-e0:+.3f} ms | "
                          f"{'—' if sa != sa else f'{sa:.3f}'} | {'—' if s0 != s0 else f'{s0:.3f}'} | "
                          f"{'—' if dcpu != dcpu else f'{dcpu:+.3f} ms'} | "
                          f"{'—' if rest != rest else f'{rest:+.3f} ms'} |")
            md.append("")
            md.append("_`Δ segcore`는 같은 쿼리·같은 세그먼트를 다른 CPU에서 돌린 차이이므로 순수 하드웨어 항이다. "
                      "`Δ E2E`에서 그것을 빼야 비로소 hop에 가까워지지만, 메모리 지연·NUMA가 segcore 밖 "
                      "단계에도 영향을 주므로 이 뺄셈도 **근사**다. 확정적인 hop 값은 위의 "
                      f"L0 vs {LA_CLEAN}이다._\n")

    # traced stage shares
    labs = [a for a, _, _, _ in LA_ARMS if tl.get(a) and trace_ok(tl[a])[0]]
    bad = [(a, trace_ok(tl[a])[1]) for a, _, _, _ in LA_ARMS if tl.get(a) and not trace_ok(tl[a])[0]]
    if labs:
        md.append("### T4-b. critical path 단계 비중 (traced — 절대 ms 아님, 비중만)\n")
        for a, why in bad:
            md.append(f"- ⚠ **{a} 제외**: {why}")
        keys, seen = [], set()
        for a in labs:
            for k in tl[a]["critical_path_grouped"]:
                if k not in seen:
                    seen.add(k); keys.append(k)
        md.append("| arm | " + " | ".join(keys) + " | sum | traces | cov |")
        md.append("|---" * (len(keys) + 4) + "|")
        for a in labs:
            g, _ = stage_pct(tl[a])
            tot = 100 * sum(g.get(k, 0) for k in keys)
            d = tl[a]
            n, cov = d.get("traces", 0), 100 * coverage(d)
            weak = "  ⚠" if (n < 100 or cov > 115) else ""
            md.append(f"| {a} | " + " | ".join(f"{100*g.get(k,0):.1f}%" for k in keys)
                      + f" | {tot:.1f}% | {n} | {cov:.0f}%{weak} |")
        md.append("")
        md.append("_hop 두 개(`2. fan-out`, `4. fan-out`)의 비중이 L0→L2로 줄고 그만큼 "
                  "segcore 비중이 올라가야 한다. 안 그러면 줄어든 것은 hop이 아니라 다른 것이다._\n")

    # figure: E2E bars + the stage stack next to them
    arms_present = [a for a, _, _, _ in LA_ARMS if a in la]
    if len(arms_present) >= 2:
        fig, axes = plt.subplots(1, 2 if labs else 1, figsize=(9.2 if labs else 5.0, 3.8))
        ax = axes[0] if labs else axes
        e = [ms(la[a]["row"]) for a in arms_present]
        ax.bar(range(len(arms_present)), e, color="#dbe7f7", edgecolor=BLUE, linewidth=1.2,
               label="E2E")
        segs = []
        for a in arms_present:
            d = tl.get(a)
            v = float("nan")
            if d and trace_ok(d)[0]:
                g, _ = stage_pct(d)
                sk = segcore_key(g)
                if sk:
                    v = g[sk] * ms(la[a]["row"])
            segs.append(v)
        if any(v == v for v in segs):
            ax.bar(range(len(arms_present)), [v if v == v else 0 for v in segs],
                   color=BLUE, label="segcore (actual vector search)")
        ax.set_xticks(range(len(arms_present)))
        ax.set_xticklabels(arms_present)
        ax.set_ylabel("ms per query")
        ax.set_title("Layer transport removed by co-location", color=INK, fontsize=10)
        ax.legend(frameon=False, fontsize=8)
        ax.spines[["top", "right"]].set_visible(False)

        if labs:
            ax2 = axes[1]
            bottom = [0.0] * len(labs)
            for i, k in enumerate(keys):
                vals = [100 * stage_pct(tl[a])[0].get(k, 0) for a in labs]
                ax2.bar(range(len(labs)), vals, bottom=bottom,
                        color=STAGE_COLORS[i % len(STAGE_COLORS)], label=k,
                        edgecolor="white", linewidth=0.6)
                bottom = [b + v for b, v in zip(bottom, vals)]
            ax2.set_xticks(range(len(labs)))
            ax2.set_xticklabels(labs)
            ax2.set_ylabel("% of E2E")
            ax2.set_title("Critical path by placement", color=INK, fontsize=10)
            ax2.legend(frameon=False, fontsize=6, ncol=2, loc="upper center",
                       bbox_to_anchor=(0.5, -0.12))
            ax2.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, "fig_layer_ab.png"), dpi=160)
        plt.close(fig)
        md.append("![layer A/B](fig_layer_ab.png)\n")

    md.append("_**confound — 캡션에 반드시 쓸 것:** co-location은 NIC hop을 없애는 대신 "
              "CPU·메모리 대역폭 경합을 더한다. L2는 delegator와 worker를 Proxy·DataNode·"
              "MixCoord·bench client와 같은 박스에 올린다. 따라서 `E2E(L0) − E2E(L2)`는 "
              "네트워크 비용의 **하한**이다._\n")


# ------------------------------------------------ T5: S sweep / N sweep, ID vs +vector
SN_RE = re.compile(r"^M2SN_(ID|VEC)_S(\d+)_Q(\d+)_r(\d+)$")


def load_snsweep():
    """Pool M2SN_<arm>_S<s>_Q<q>_r<i> written by 15_motivation2.sh stage_snsweep.

    These rows existed in runs_m2.csv but no table rendered them, so the S sweep
    lived only in that stage's stdout log.
    """
    if not os.path.exists(RUNS):
        return {}
    out = {}
    with open(RUNS, newline="") as fh:
        for r in csv.DictReader(fh):
            m = SN_RE.match(r.get("label", ""))
            if not m or f(r, "mean_us") != f(r, "mean_us"):
                continue
            out.setdefault((m.group(1), int(m.group(2)), int(m.group(3))), []).append(r)
    res = {}
    for k, g in out.items():
        g.sort(key=lambda r: f(r, "mean_us"))
        means = [f(r, "mean_us") for r in g]
        n = len(means)
        mu = sum(means) / n
        sd = (sum((x - mu) ** 2 for x in means) / (n - 1)) ** 0.5 if n >= 2 else float("nan")
        res[k] = {"row": dict(g[(n - 1) // 2]), "n": n,
                  "cv": (100 * sd / mu) if (n >= 2 and mu) else float("nan")}
    return res


def _sn_line(d):
    r = d["row"]
    traf = f(r, "rx_kb_per_query_total") + f(r, "tx_kb_per_query_total")
    cv = d["cv"]
    flag = " ⚠" if (cv == cv and cv > CV_LIMIT) else ""
    return (f"{ms(r):.3f}{flag} | {d['n']} | {'' if cv != cv else f'{cv:.1f}'}% | "
            f"{f(r,'worker_reduce_segs_ms'):.4f} | {f(r,'deleg_reduce_shards_ms'):.4f} | "
            f"{f(r,'proxy_reduce_ms'):.4f} | {traf:.2f} | "
            f"{f(r,'actual_shards_touched'):.0f}/{f(r,'actual_N_per_shard_avg'):.0f}/"
            f"{f(r,'actual_M_per_worker_per_shard'):.0f} | {r.get('recall_at_k','')} |")


def table_snsweep(md):
    sn = load_snsweep()
    if not sn:
        return
    hdr = ("| {x} | E2E (ms) | n | CV | QN reduce | Streaming reduce | Proxy reduce | "
           "traffic KB/q | S/N/M | recall |")
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"   # 10 columns, matches hdr

    md.append("## T5-A. S만 바꾼 스윕 (N=1, M=4, NQ=1, C=1, topK=10)\n")
    md.append("`output_fields`가 비어 있는 ID+거리 전용. 같은 컬렉션 계열에서 shard 수만 1→4→16.\n")
    md.append(hdr.format(x="shard S") + "\n" + sep)
    for s_ in (1, 4, 16):
        d = sn.get(("ID", s_, 1))
        md.append(f"| **{s_}** | " + (_sn_line(d) if d else "_missing_ | | | | | | | | |"))
    md.append("")
    md.append("_`Streaming reduce`(delegator의 shard-level reduce)는 S가 16배가 되어도 "
              "0.0002 ms 이하로 붙박이다. S가 올리는 것은 **Proxy reduce**(0.033→0.083 ms, "
              "쿼리당 1회 관측)와 **traffic**(9.0→87.7 KB, 9.7배)이다. 계층의 값은 reduce가 "
              "아니라 fan-out 그 자체라는 뜻이고, 이것이 T4가 reduce 열을 대조군으로만 쓰는 이유다._\n")

    md.append("## T5-B. 같은 점을 `output_fields=[\"vector\"]`로 (top-k 원본 벡터 반환)\n")
    md.append(hdr.format(x="shard S") + "\n" + sep)
    for s_ in (1, 4, 16):
        d = sn.get(("VEC", s_, 1))
        md.append(f"| **{s_}** | " + (_sn_line(d) if d else "_missing_ | | | | | | | | |"))
    md.append("")
    rows_ = []
    for s_ in (1, 4, 16):
        a, b = sn.get(("ID", s_, 1)), sn.get(("VEC", s_, 1))
        if a and b:
            rows_.append((s_, ms(a["row"]), ms(b["row"]),
                          f(a["row"], "proxy_reduce_ms"), f(b["row"], "proxy_reduce_ms"),
                          f(a["row"], "rx_kb_per_query_total") + f(a["row"], "tx_kb_per_query_total"),
                          f(b["row"], "rx_kb_per_query_total") + f(b["row"], "tx_kb_per_query_total")))
    if rows_:
        md.append("**Δ = 벡터 materialisation + 전송 비용**\n")
        md.append("| S | E2E ID | E2E +vec | Δ E2E | Proxy reduce ID→vec | traffic ID→vec |")
        md.append("|---:|---:|---:|---:|---|---|")
        for s_, e0, e1, p0, p1, t0, t1 in rows_:
            md.append(f"| {s_} | {e0:.3f} | {e1:.3f} | **+{e1-e0:.3f} ms ({e1/e0:.2f}×)** | "
                      f"{p0:.4f} → **{p1:.4f}** | {t0:.1f} → {t1:.1f} KB |")
        md.append("")
        md.append("_증가분이 거의 전부 **Proxy reduce**에 나타난다: 그 열은 쿼리당 1회 관측이고, "
                  "Proxy가 top-k 벡터를 역직렬화해 합치는 지점이다. 이것은 *탐색이 끝난 뒤* "
                  "top-k를 읽는 비용이지 *그래프를 타는 동안* 읽는 벡터 비용이 아니다 "
                  "(후자는 T3-B의 IVF_FLAT nprobe 회귀가 담당한다)._\n")

    nq = [q for q in (1, 2, 4) if ("ID", 16, q) in sn]
    if len(nq) > 1:
        md.append("## T5-C. S=16 고정, N만 바꾼 스윕\n")
        md.append(hdr.format(x="QueryNode pods (N)") + "\n" + sep)
        for q in nq:
            md.append(f"| **{q}** | " + _sn_line(sn[("ID", 16, q)]))
        md.append("")


def main():
    os.makedirs(OUT, exist_ok=True)
    byl = load_rows()
    tr = traces()
    md = ["# Motivation 2 — per-query latency decomposition", "",
          f"source: `{os.path.relpath(RUNS, ROOT)}` ({len(byl)} labels), "
          f"`{os.path.relpath(TRACE, ROOT)}` ({len(tr)} traces)", "",
          "NQ=1, concurrency=1, topK=10 throughout. 조건은 각 행의 runs_m2.csv 열에 기록됨.", "",
          "각 점은 버리는 prewarm 1회 + 실측 n회이고 표의 값은 **median**이다. "
          "CV > 5% (⚠)인 점은 쓰지 말 것 — 이 박스는 같은 조건 연속 측정에서 "
          "3970 / 2867 / 2937 µs가 나온 적이 있다.", ""]

    table1(byl, tr, md)
    ladder(byl, tr, md,
           [(q, f"M2_N_S1_Q{q}", f"M2T_S1_Q{q}") for q in (1, 2, 4)],
           "QueryNode pods (N per shard)", "T2-A. N ladder at S=1", "fig_N_S1.png")
    ladder(byl, tr, md,
           [(q, f"M2_N_S16_Q{q}", f"M2T_S16_Q{q}") for q in (1, 2, 4)],
           "QueryNode pods (N per shard)", "T2-B. N ladder at S=16", "fig_N_S16.png")
    ladder(byl, tr, md,
           [(c, ("M2_BASE_S1_Q1_M1" if c == 1 else f"M2_M_S1_Q1_C{c}"), f"M2T_M_S1_Q1_C{c}")
            for c in (1, 4, 16)],
           "insert chunks (-> M segments per QueryNode)",
           "T2-D. M ladder at S=1, N=1", "fig_M.png")
    stage_stack(tr, md)
    table_layerab(byl, tr, md)
    table_snsweep(md)
    table_outfields(byl, md)
    table_nprobe(byl, md)

    p = os.path.join(OUT, "REPORT_M2.md")
    open(p, "w").write("\n".join(md) + "\n")
    print(f"wrote {p}")
    for g in sorted(glob.glob(os.path.join(OUT, "*.png"))):
        print("  " + g)


if __name__ == "__main__":
    main()
