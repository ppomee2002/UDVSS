#!/usr/bin/env python3
"""Step D-prep. Read-only: inspect the staged datasets, verify the ground truth,
and print the numbers that go into Table 1.

    05_prepare_dataset.py --list
    05_prepare_dataset.py --dataset deep1m --verify-gt 200
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from lib.dataset import open_dataset, read_fvecs, read_ivecs


def cmd_list(root: str) -> None:
    print(f"{'dataset':<14}{'vectors':>10}{'dim':>6}{'metric':>8}{'queries':>9}  base file")
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        try:
            ds = open_dataset(name, root)
        except Exception as exc:
            print(f"{name:<14}  <unreadable: {exc}>")
            continue
        nq = ds.meta.get("query_count", "?")
        size_gb = os.path.getsize(ds.base_path) / 2**30 if os.path.exists(ds.base_path) else 0
        flag = "" if ds.count >= 1_000_000 else "   (< 1M -- not eligible)"
        print(f"{name:<14}{ds.count:>10}{ds.dim:>6}{ds.metric:>8}{str(nq):>9}  {size_gb:.2f} GiB{flag}")


def cmd_verify(name: str, root: str, nq: int, topk: int) -> int:
    ds = open_dataset(name, root)
    print(f"dataset            {ds.name}")
    print(f"root               {ds.root}")
    print(f"vectors            {ds.count:,}")
    print(f"dim                {ds.dim}")
    print(f"metric             {ds.metric}   (source: {ds.meta.get('source_metric')}, "
          f"normalized={ds.meta.get('normalized')})")
    print(f"base bytes/vector  {ds.dim * 4}")
    print(f"raw base size      {ds.count * ds.dim * 4 / 2**30:.2f} GiB")
    print(f"queries available  {ds.meta.get('query_count')}")
    if ds.count < 1_000_000:
        print("\n!! this dataset has fewer than 1M vectors; the study requires >= 1M")
        return 1

    if nq <= 0:
        return 0

    print(f"\nverifying ground truth by brute force on {nq} queries (top-{topk}) ...")
    base = read_fvecs(ds.base_path)                     # memmapped view, (N, dim)
    q = read_fvecs(ds.query_path, 0, nq)
    gt = read_ivecs(ds.gt_path, 0, nq)
    base_sq = None
    hits = 0
    # chunked exact L2 so we never materialise an (nq x N) float64 matrix
    CH = 100_000
    best_idx = np.zeros((nq, topk), dtype=np.int64)
    best_val = np.full((nq, topk), np.inf, dtype=np.float32)
    for s in range(0, ds.count, CH):
        e = min(s + CH, ds.count)
        blk = np.asarray(base[s:e], dtype=np.float32)
        d = (blk * blk).sum(1)[None, :] - 2.0 * (q @ blk.T)   # + |q|^2, constant per row
        cand = np.argpartition(d, min(topk, d.shape[1] - 1), axis=1)[:, :topk]
        cv = np.take_along_axis(d, cand, axis=1)
        allv = np.concatenate([best_val, cv], axis=1)
        alli = np.concatenate([best_idx, cand + s], axis=1)
        order = np.argsort(allv, axis=1)[:, :topk]
        best_val = np.take_along_axis(allv, order, axis=1)
        best_idx = np.take_along_axis(alli, order, axis=1)
    for i in range(nq):
        hits += len(set(best_idx[i].tolist()) & set(gt[i][:topk].tolist()))
    overlap = hits / (nq * topk)
    print(f"brute-force vs shipped GT top-{topk} overlap: {overlap:.4f}")
    if overlap < 0.99:
        print("!! ground truth does not match an exact L2 search of this base file.")
        print("   Do not report recall against it until this is resolved.")
        return 1
    print("ground truth OK for L2")
    return 0


def main() -> int:
    root = os.environ.get("MM_DATASET_DIR", "/home/dev_path/datasets")
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dataset", default=os.environ.get("MM_DATASET", "deep1m"))
    ap.add_argument("--root", default=root)
    ap.add_argument("--verify-gt", type=int, default=0, metavar="NQ",
                    help="brute-force check this many queries against the shipped GT")
    ap.add_argument("--topk", type=int, default=int(os.environ.get("MM_TOPK", 10)))
    a = ap.parse_args()
    if a.list:
        cmd_list(a.root)
        return 0
    return cmd_verify(a.dataset, a.root, a.verify_gt, a.topk)


if __name__ == "__main__":
    raise SystemExit(main())
