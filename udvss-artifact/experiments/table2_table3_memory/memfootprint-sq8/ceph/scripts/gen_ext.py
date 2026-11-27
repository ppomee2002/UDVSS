#!/usr/bin/env python3
"""Extend data/aug_8M.fvecs in place (append only; the 8M prefix is untouched, so runs reading
it as a prefix are unaffected) to TARGET ids with the same generator (scripts/common.py vectors()),
and write ceph/data/gt_<N>.ivecs (200 x top-10, exact over ids [0,N)) for each N in STEPS.
Hard link data/aug_ext.fvecs -> same inode. 2026-10-16."""
import os, sys, numpy as np
sys.path.insert(0, "/home/dev_path/experiments/memfootprint-sq8/scripts")
import common as C
D = "/home/dev_path/experiments/memfootprint-sq8/ceph/data"
STEPS = [int(x) for x in sys.argv[1].split(",")]
F = f"{D}/aug_8M.fvecs"; ROW = 4 * (C.DIM + 1)
have = os.path.getsize(F) // ROW
assert os.path.getsize(F) % ROW == 0
target = max(STEPS)
hdr = np.full((100_000, 1), C.DIM, np.int32).view(np.float32)
with open(F, "ab") as fh:
    for lo in range(have, target, 100_000):
        fh.write(np.hstack([hdr, C.vectors(lo, lo + 100_000)]).astype(np.float32).tobytes())
        if (lo // 100_000) % 10 == 0: print(f"append {lo + 100_000}", flush=True)
if not os.path.exists(f"{D}/aug_ext.fvecs"): os.link(F, f"{D}/aug_ext.fvecs")
print("rows", os.path.getsize(F) // ROW, flush=True)
for n in STEPS:
    out = f"{D}/gt_{n}.ivecs"
    if os.path.exists(out): continue
    ids = C.ground_truth(n).astype(np.int32)
    np.hstack([np.full((ids.shape[0], 1), C.TOPK, np.int32), ids]).tofile(out + ".tmp"); os.replace(out + ".tmp", out)
    print("gt", n, flush=True)
print("GEN_EXT DONE", flush=True)
