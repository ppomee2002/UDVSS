#!/usr/bin/env python3
"""Choose C per (dataset, ef) by arg-max QPS, mirroring pick_best_c.sh.

The Ceph study tunes throughput, so the pick is the arg-max, not the cheapest
point on a plateau -- latency is reported separately.  Every C within
--near-pct of the best is listed as a tie rather than silently resolved, and a
pair whose best C is the largest tested, with the last step still gaining more
than --near-pct, is marked NOT-PLATEAUED so the ladder can be extended.

Reads the .row files, never a summary, and touches nothing.
"""
import argparse, glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_env as E
from run_one import COLS
I = {c: i for i, c in enumerate(COLS)}

ap = argparse.ArgumentParser()
ap.add_argument("--ef-list", default=" ".join(str(x) for x in E.EF_TUNING))
ap.add_argument("--near-pct", type=float, default=2.0)
ap.add_argument("--out", default=f"{E.RESULTS}/chosen_c.tsv")
a = ap.parse_args()
EFS = [int(x) for x in a.ef_list.split()]

rows = []
for f in glob.glob(f"{E.RESULTS}/*/ef*_nq*_c*_r*.row"):
    p = open(f).read().rstrip("\n").split("\t")
    if len(p) != len(COLS) or p[I["status"]] != "OK":
        continue
    # tuning rows only: the finals are tagged f1..f3 and must not feed back into
    # the choice that produced them.
    if not p[I["repeat"]].isdigit():
        continue
    rows.append(p)

out = [["dataset", "ef", "nq", "chosen_c", "best_qps", "ties_within_pct", "status"]]
for ds in E.DATASETS:
    for ef in EFS:
        g = [p for p in rows if p[I["dataset"]] == ds and int(p[I["ef"]]) == ef]
        if not g:
            out.append([ds, ef, E.NQ, "NA", "NA", "", "NO-DATA"]); continue
        by_c = sorted(((int(p[I["concurrency"]]), float(p[I["qps_vectors"]])) for p in g))
        best_c, best_q = max(by_c, key=lambda t: t[1])
        ties = [str(c) for c, q in by_c if c != best_c and q >= best_q * (1 - a.near_pct / 100)]
        status = "OK"
        if best_c == max(c for c, _ in by_c) and len(by_c) > 1:
            prev = [q for c, q in by_c if c < best_c][-1]
            if (best_q - prev) / prev * 100 > a.near_pct:
                status = "NOT-PLATEAUED"
        print(f"{ds:<11} ef={ef:<5} " +
              "  ".join(f"C{c}:{q:.0f}" for c, q in by_c) +
              f"   -> C={best_c} ({status})")
        out.append([ds, ef, E.NQ, best_c, round(best_q, 3), ",".join(ties), status])

with open(a.out, "w") as fh:
    for r in out:
        fh.write("\t".join(str(x) for x in r) + "\n")
print(f"\nwritten: {a.out}")
