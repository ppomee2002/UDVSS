#!/usr/bin/env python3
"""Pick the pivot variant for d10m_pg4096: fewest candidates at recall 0.95
(interpolated between the M points). Prints the routing column for arms.tsv."""
import glob, os, re
FLAGS = {
 "ll3_s65536": "--pivot-build-sample,65536,--pivot-refine-iters,3",
 "sph_s65536_it25": "--pivot-build-sample,65536,--pivot-refine-iters,25,--pivot-spherical",
 "sph_s262144_it25": "--pivot-build-sample,262144,--pivot-refine-iters,25,--pivot-spherical",
 "s262144_it25": "--pivot-build-sample,262144,--pivot-refine-iters,25",
 "bal_s262144_it25": "--pivot-build-sample,262144,--pivot-refine-iters,25,--pivot-balance-eps,0.1,--pivot-place-cap-eps,0.1",
}
R = 0.95; best = None
for p in sorted(glob.glob(os.path.dirname(os.path.abspath(__file__)) + "/logs/*.log")):
    t = os.path.basename(p)[:-4]; s = open(p).read()
    if "GT_PG_RANK" not in s: continue
    pts = [(float(r), float(c)) for r, c in re.findall(r"recall_at_10=([\d.]+) gt_reachable=[\d.]+ candidates_mean=([\d.]+)", s)]
    ca = None
    for (r0, c0), (r1, c1) in zip(pts, pts[1:]):
        if r0 < R <= r1: ca = c0 + (c1 - c0) * (R - r0) / (r1 - r0); break
    print(f"# {t:<20} cand@{R}={ca:.0f}" if ca else f"# {t} never reaches {R}")
    if ca and (best is None or ca < best[1]): best = (t, ca)
print(f"# best: {best[0]}")
print(FLAGS[best[0]])
