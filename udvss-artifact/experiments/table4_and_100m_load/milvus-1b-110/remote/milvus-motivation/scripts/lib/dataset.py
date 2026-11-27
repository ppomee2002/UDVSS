"""fvecs / ivecs readers for the datasets already staged under $MM_DATASET_DIR.

Layout (same convention as the Ceph vector-bench datasets on this host):
    <dir>/<name>/<name>_base.fvecs         float32 base vectors
    <dir>/<name>/<name>_query.fvecs        float32 query vectors
    <dir>/<name>/<name>_groundtruth.ivecs  int32 top-k neighbour ids (L2)
    <dir>/<name>/<name>_meta.json          counts / dim / metric

fvecs record: int32 dim, then `dim` float32.
ivecs record: int32 k,   then `k`   int32.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


@dataclass
class Dataset:
    name: str
    root: str
    count: int
    dim: int
    metric: str
    meta: dict

    @property
    def base_path(self) -> str:
        return os.path.join(self.root, f"{self.name}_base.fvecs")

    @property
    def query_path(self) -> str:
        return os.path.join(self.root, f"{self.name}_query.fvecs")

    @property
    def gt_path(self) -> str:
        return os.path.join(self.root, f"{self.name}_groundtruth.ivecs")


def open_dataset(name: str, root: Optional[str] = None) -> Dataset:
    root = root or os.environ.get("MM_DATASET_DIR", "/home/dev_path/datasets")
    d = os.path.join(root, name)
    meta_path = os.path.join(d, f"{name}_meta.json")
    if not os.path.isdir(d):
        raise FileNotFoundError(d)
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    dim = meta.get("dimension")
    count = meta.get("base_count")
    if dim is None or count is None:
        dim, count = _probe_fvecs(os.path.join(d, f"{name}_base.fvecs"))
    metric = {"euclidean": "L2", "angular": "COSINE", "ip": "IP"}.get(
        str(meta.get("distance_metric", "euclidean")).lower(), "L2")
    return Dataset(name, d, int(count), int(dim), metric, meta)


def _is_fbin(path: str) -> bool:
    # big-ann .fbin (u32 n, u32 dim, n*dim f32), e.g. Deep1B base.1B.fbin
    # symlinked as <name>_base.fvecs (2026-10-26)
    return os.path.realpath(path).endswith(".fbin")


def _probe_fvecs(path: str) -> Tuple[int, int]:
    if _is_fbin(path):
        with open(path, "rb") as fh:
            n, dim = (int(x) for x in np.frombuffer(fh.read(8), dtype="<u4"))
        return dim, n
    with open(path, "rb") as fh:
        dim = int(np.frombuffer(fh.read(4), dtype="<i4")[0])
    rec = 4 + 4 * dim
    n = os.path.getsize(path) // rec
    return dim, n


def read_fvecs(path: str, start: int = 0, count: Optional[int] = None) -> np.ndarray:
    """Return a (count, dim) float32 array, memory-efficiently, without the dim prefix."""
    dim, total = _probe_fvecs(path)
    if count is None:
        count = total - start
    count = max(0, min(count, total - start))
    if _is_fbin(path):
        arr = np.memmap(path, dtype="<f4", mode="r", offset=8 + start * dim * 4,
                        shape=(count, dim))
        return np.ascontiguousarray(arr)
    rec_floats = dim + 1
    arr = np.memmap(path, dtype="<f4", mode="r",
                    offset=start * rec_floats * 4,
                    shape=(count, rec_floats))
    return np.ascontiguousarray(arr[:, 1:])


def read_ivecs(path: str, start: int = 0, count: Optional[int] = None) -> np.ndarray:
    with open(path, "rb") as fh:
        k = int(np.frombuffer(fh.read(4), dtype="<i4")[0])
    rec_ints = k + 1
    total = os.path.getsize(path) // (rec_ints * 4)
    if count is None:
        count = total - start
    count = max(0, min(count, total - start))
    arr = np.memmap(path, dtype="<i4", mode="r",
                    offset=start * rec_ints * 4,
                    shape=(count, rec_ints))
    return np.ascontiguousarray(arr[:, 1:])


def recall_at_k(returned_ids: np.ndarray, gt_ids: np.ndarray, k: int) -> float:
    """Mean recall@k. returned_ids/gt_ids are (nq, >=k) int arrays of base-row indices."""
    n = min(len(returned_ids), len(gt_ids))
    if n == 0:
        return float("nan")
    hits = 0
    for i in range(n):
        hits += len(set(returned_ids[i][:k].tolist()) & set(gt_ids[i][:k].tolist()))
    return hits / (n * k)
