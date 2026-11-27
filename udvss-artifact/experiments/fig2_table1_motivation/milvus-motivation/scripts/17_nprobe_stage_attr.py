#!/usr/bin/env python3
"""Where does the nprobe slope gap actually live?

The trace-OFF regression (16_report_m2.py, T3-B) produced two slopes:

    b(E2E)     = 0.0182 ms/nprobe     (client-measured)
    b(segcore) = 0.0101 ms/nprobe     (Prometheus sq_core_latency)

and the 0.0081 ms/nprobe residual was only ever *named* "result movement". This
script measures it instead: it regresses EVERY critical-path stage against nprobe
from the tracing-ON runs (M2NT_NP*) and reports which stages carry a slope.

Attribution is read off the fitted slopes. Nothing is assumed to be network or
reduce beforehand.

Tracing inflates absolute latency (~+21% measured), so trace-ON E2E is never
compared with trace-OFF E2E here. Stage SHARES come from the traces; the
trace-OFF E2E column is carried alongside for reference only.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys

RES = os.environ.get("MM_RESULTS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
TRACE = os.path.join(RES, "trace")
RUNS = os.path.join(RES, "runs_m2.csv")
OUT = os.path.join(RES, "report_m2")

R2_MIN = 0.90


def fit(xs, ys):
    good = [(x, y) for x, y in zip(xs, ys) if y == y]
    n = len(good)
    if n < 3:
        return None
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
    return a, b, (1 - ss_res / ss_tot if ss_tot else float("nan"))


def load_traces():
    out = {}
    for p in sorted(glob.glob(os.path.join(TRACE, "M2NT_NP*_summary.json"))):
        d = json.load(open(p))
        m = re.match(r"^M2NT_NP(\d+)$", d["label"])
        if m:
            out[int(m.group(1))] = d
    return out


def load_offline():
    """trace-OFF nprobe rows: median of the repeats, with recall."""
    if not os.path.exists(RUNS):
        return {}
    rows = [r for r in csv.DictReader(open(RUNS))
            if re.match(r"^M2_NPROBE_\d+(_r\d+)?$", r.get("label", ""))]
    g = {}
    for r in rows:
        np_ = int(re.match(r"^M2_NPROBE_(\d+)", r["label"]).group(1))
        g.setdefault(np_, []).append(r)
    out = {}
    for np_, v in g.items():
        v.sort(key=lambda r: float(r["mean_us"]))
        out[np_] = v[(len(v) - 1) // 2]
    return out


def main():
    tr = load_traces()
    off = load_offline()
    if len(tr) < 3:
        sys.exit(f"need >=3 traced nprobe points in {TRACE} (have {len(tr)}); "
                 "run `15_motivation2.sh nprobetrace` first")

    nps = sorted(tr)
    md = ["# T3-C. nprobe slope attribution — 어느 stage가 커지는가", "",
          "trace-OFF 회귀의 잔차 `b(E2E) − b(segcore)`를 **이름 붙이지 않고 측정**한다. "
          "tracing ON으로 nprobe를 바꿔가며 critical path를 재고, **stage마다** "
          "`stage_time = a + b·nprobe`를 적합해 기울기를 가진 stage를 찾는다.", "",
          "NQ=1, concurrency=1, topK=10, IVF_FLAT nlist=1024, 컬렉션 `mm_deep1m_s1_c1_ivf` "
          "재사용(생성·reload 없음).", ""]

    # ---- trace quality first
    md.append("## 표본 품질\n")
    md.append("| nprobe | traces used | dropped(error) | dropped(tiny) | root mean (µs) | root p50 (µs) | bimodal |")
    md.append("|---:|---:|---:|---:|---:|---:|---|")
    bad = []
    for n in nps:
        d = tr[n]
        used = d.get("traces", 0)
        if used < 200:
            bad.append(f"nprobe={n}: {used} traces (<200)")
        if d.get("bimodal"):
            bad.append(f"nprobe={n}: bimodal")
        md.append(f"| {n} | {used}{' ⚠' if used < 200 else ''} | "
                  f"{d.get('traces_dropped_error','-')} | {d.get('traces_dropped_tiny','-')} | "
                  f"{d['root_mean_us']:.0f} | {d['root_p50_us']:.0f} | {d.get('bimodal','-')} |")
    md.append("")
    if bad:
        md.append("> ⚠ " + "; ".join(bad) + "\n")

    # ---- per-stage regression, coarse then fine
    for key, title in (("critical_path_grouped", "거친 분해 (T2-C와 동일한 6 stage)"),
                       ("critical_path_grouped_fine", "세분 분해 (QueryNode local / segcore 분리)")):
        if not all(key in tr[n] for n in nps):
            md.append(f"## {title}\n\n_`{key}`가 없는 trace가 있다 "
                      "(12_trace_breakdown.py의 구버전으로 수집됨). 재수집 필요._\n")
            continue
        stages = []
        seen = set()
        for n in nps:
            for k in tr[n][key]:
                if k not in seen:
                    seen.add(k)
                    stages.append(k)

        md.append(f"## {title}\n")
        md.append("| stage | " + " | ".join(f"np={n} (µs)" for n in nps) +
                  " | slope b (µs/nprobe) | R² |")
        md.append("|---" * (len(nps) + 3) + "|")
        fits = {}
        for st in sorted(stages):
            ys = [tr[n][key].get(st, float("nan")) for n in nps]
            f = fit(nps, ys)
            fits[st] = f
            cells = " | ".join(f"{y:.1f}" if y == y else "" for y in ys)
            if f:
                md.append(f"| {st} | {cells} | **{f[1]:.2f}** | {f[2]:.3f}"
                          f"{' ⚠' if f[2] < R2_MIN else ''} |")
            else:
                md.append(f"| {st} | {cells} | | |")
        # total, as a check
        tot = [sum(tr[n][key].values()) for n in nps]
        ft = fit(nps, tot)
        if ft:
            md.append(f"| **합계 (critical path)** | " +
                      " | ".join(f"{t:.1f}" for t in tot) +
                      f" | **{ft[1]:.2f}** | {ft[2]:.3f} |")
        md.append("")

        # ---- attribution: rank stages by how much slope they carry
        pos = {k: v[1] for k, v in fits.items() if v and v[1] > 0}
        tot_slope = sum(pos.values())
        if tot_slope > 0:
            md.append("**기울기 귀속** (양의 기울기를 가진 stage만, 합 대비 비중)\n")
            md.append("| stage | slope (µs/nprobe) | 비중 | R² |")
            md.append("|---|---:|---:|---:|")
            for st in sorted(pos, key=lambda k: -pos[k]):
                md.append(f"| {st} | {pos[st]:.2f} | {100*pos[st]/tot_slope:.1f}% | "
                          f"{fits[st][2]:.3f} |")
            md.append("")

    # ---- the residual this experiment exists to explain
    md.append("## trace-OFF 잔차와의 대조\n")
    md.append("trace-OFF 회귀(T3-B, 12점)의 두 기울기:\n")
    md.append("| | slope (µs/nprobe) |")
    md.append("|---|---:|")
    md.append("| b(E2E), client | 18.2 |")
    md.append("| b(segcore), Prometheus | 10.1 |")
    md.append("| **잔차** | **8.1** |")
    md.append("")
    md.append("위 stage 회귀에서 segcore 계열을 뺀 나머지 stage들의 기울기 합이 이 잔차와 "
              "맞는지, 그리고 그 합이 어느 stage에 몰려 있는지가 이 실험의 답이다.\n")

    # ---- reference table
    md.append("## 참고: nprobe / recall / trace-OFF E2E\n")
    md.append("| nprobe | recall@10 | trace-OFF E2E median (ms) | trace-ON root mean (µs) |")
    md.append("|---:|---:|---:|---:|")
    for n in nps:
        r = off.get(n)
        rec = r["recall_at_k"] if r else ""
        e2e = f"{float(r['mean_us'])/1000:.3f}" if r else ""
        md.append(f"| {n} | {rec} | {e2e} | {tr[n]['root_mean_us']:.0f} |")
    md.append("")
    md.append("> **trace-ON E2E를 trace-OFF E2E와 직접 비교하지 말 것.** span export가 "
              "latency를 ~21% 부풀린다. trace-ON은 stage 구성비를 얻는 용도다.\n")

    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "REPORT_NPROBE_STAGES.md")
    open(p, "w").write("\n".join(md) + "\n")
    print(f"wrote {p}")
    print("\n".join(md))


if __name__ == "__main__":
    main()
