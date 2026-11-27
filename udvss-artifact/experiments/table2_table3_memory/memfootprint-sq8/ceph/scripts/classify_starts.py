#!/usr/bin/env python3
"""For every osd_died_at_start row, find the reason in the arm's osd.0.log within
the start window: bad_alloc (memory) vs 'out of space' (seastore segments) vs other."""
import json, re, sys, datetime as dt, subprocess
EXP = "/home/dev_path/experiments/memfootprint-sq8/ceph"
def ts(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
for arm in ("fp32", "int8"):
    log = f"/home/dev_path/vstart/memfp-{arm}/out/osd.0.log"
    rows = [json.loads(l) for l in open(f"{EXP}/results/main/{arm}/rows.jsonl")]
    out = []
    for r in rows:
        if r["failure"] != "osd_died_at_start":
            continue
        t1 = ts(r["utc"]); t0 = t1 - dt.timedelta(seconds=40)
        pats = [(t0 + dt.timedelta(seconds=k)).strftime("%Y-%m-%d %H:%M:%S") for k in range(0, 45)]
        g = subprocess.run(["grep", "-aE", "|".join(re.escape(p) for p in pats), log],
                           capture_output=True, text=True).stdout
        reasons = []
        for key, pat in (("bad_alloc", r"bad_alloc"), ("out_of_space", r"out of space"),
                         ("abort", r"Aborting|ceph_assert|FAILED")):
            n = len(re.findall(pat, g))
            if n: reasons.append(f"{key}x{n}")
        first = next((l for l in g.splitlines() if re.search(r"bad_alloc|out of space", l)), "")
        out.append({"N": r["N"], "cap": r["cap"], "cache": r["cache_pershard"], "utc": r["utc"],
                    "reasons": reasons, "first": first[:220]})
    json.dump(out, open(f"{EXP}/results/main/{arm}/start_failure_reasons.json", "w"), indent=1)
    for o in out:
        print(arm, o["N"], o["cap"], o["cache"], o["reasons"], "|", o["first"][:160])
