#!/usr/bin/env python3
"""Step D. Create a collection with a chosen S, ingest the dataset with a chosen
flush cadence (which is how M is constructed), index it, and load it with a chosen
replica_number.

    06_load_collection.py --shards 4 --chunks 8 --replicas 1 \
                          --collection mm_deep1m_s4_c8

The primary key is the base-file row index, so recall can be scored directly
against the shipped ground truth.

Why chunks-and-flush instead of a "segment count" parameter: Milvus has no such
parameter. flush() seals every current growing segment, so R flush rounds over S
shards produce ~S*R sealed segments -- PROVIDED dataCoord.enableCompaction is false,
otherwise the compactor merges them back within minutes. See docs/02_snm_semantics.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient, connections, utility

from lib.dataset import open_dataset, read_fvecs


def build_schema(dim: int) -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=dim),
        ],
        description="milvus motivation study",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.environ.get("MM_URI"))
    ap.add_argument("--dataset", default=os.environ.get("MM_DATASET", "deep1m"))
    ap.add_argument("--collection", required=True)
    ap.add_argument("--shards", type=int, required=True, help="S: num_shards at create time")
    ap.add_argument("--chunks", type=int, required=True,
                    help="number of insert+flush rounds; drives the sealed-segment count")
    ap.add_argument("--replicas", type=int, default=1,
                    help="replica_number at load(). NOTE: this is NOT N -- one query is "
                         "served by exactly one replica. See docs/02_snm_semantics.md.")
    ap.add_argument("--limit", type=int, default=0, help="ingest only the first N rows (0 = all)")
    ap.add_argument("--batch", type=int, default=20000, help="rows per insert RPC")
    ap.add_argument("--index", default=os.environ.get("MM_INDEX", "HNSW"))
    ap.add_argument("--index-params", default=os.environ.get("MM_INDEX_PARAMS", '{"M":16,"efConstruction":200}'))
    ap.add_argument("--metric", default=os.environ.get("MM_METRIC", "L2"))
    ap.add_argument("--drop-existing", action="store_true")
    a = ap.parse_args()

    if not a.uri:
        print("set --uri or MM_URI (run mm_resolve after install)", file=sys.stderr)
        return 2

    ds = open_dataset(a.dataset)
    n_total = a.limit if a.limit > 0 else ds.count
    if n_total < 1_000_000:
        print(f"!! ingesting only {n_total} rows; the study requires >= 1M", file=sys.stderr)

    if a.metric != ds.metric:
        print(f"[note] dataset metric is {ds.metric}, ingesting with metric_type={a.metric}")

    index_params_dict = json.loads(a.index_params)

    client = MilvusClient(uri=a.uri)
    connections.connect(alias="mm", uri=a.uri)     # utility.* needs an ORM connection

    if client.has_collection(a.collection):
        if not a.drop_existing:
            print(f"collection {a.collection} already exists; pass --drop-existing to replace")
            return 1
        print(f"dropping existing collection {a.collection}")
        client.drop_collection(a.collection)

    print(f"creating {a.collection}: dim={ds.dim} metric={a.metric} num_shards={a.shards}")
    client.create_collection(
        collection_name=a.collection,
        schema=build_schema(ds.dim),
        num_shards=a.shards,           # -> CreateCollectionRequest.shards_num
        consistency_level="Bounded",
    )

    described = client.describe_collection(a.collection)
    got_shards = described.get("num_shards")
    print(f"  describe_collection num_shards = {got_shards}")
    if got_shards != a.shards:
        print(f"!! server reports num_shards={got_shards}, asked for {a.shards}", file=sys.stderr)
        return 1

    # ---------------------------------------------------------------- ingest
    rows_per_chunk = (n_total + a.chunks - 1) // a.chunks
    est_mb_per_chunk = rows_per_chunk * ds.dim * 4 / 2**20 / max(a.shards, 1)
    print(f"ingest: {n_total:,} rows in {a.chunks} chunks of {rows_per_chunk:,} rows")
    print(f"  ~{est_mb_per_chunk:.0f} MB per shard per chunk "
          f"(auto-seal fires near 123 MB with the default maxSize=1024 x sealProportion=0.12)")
    if est_mb_per_chunk > 100:
        print("  !! a chunk is close to / above the auto-seal size: the size limit, not your")
        print("     flush cadence, will decide where segments break. Use more chunks.")
    if rows_per_chunk // max(a.shards, 1) < 1024:
        print("  !! fewer than minSegmentNumRowsToEnableIndex=1024 rows per shard per chunk;")
        print("     those segments get no index and are brute-forced. Use fewer chunks.")

    t0 = time.time()
    done = 0
    for c in range(a.chunks):
        s = c * rows_per_chunk
        e = min(s + rows_per_chunk, n_total)
        if s >= e:
            break
        for b in range(s, e, a.batch):
            be = min(b + a.batch, e)
            vecs = read_fvecs(ds.base_path, b, be - b)
            client.insert(
                collection_name=a.collection,
                data=[{"id": int(b + i), "vector": vecs[i].tolist()} for i in range(be - b)],
            )
            done += be - b
        client.flush(a.collection)
        print(f"  chunk {c+1}/{a.chunks}: rows {s:,}..{e:,}  flushed  "
              f"({done:,}/{n_total:,}, {time.time()-t0:.0f}s)")

    print(f"ingest done in {time.time()-t0:.0f}s")

    # ---------------------------------------------------------------- index + load
    print(f"creating {a.index} index {index_params_dict} metric={a.metric}")
    ip = client.prepare_index_params()
    ip.add_index(field_name="vector", index_type=a.index,
                 metric_type=a.metric, params=index_params_dict)
    client.create_index(a.collection, ip)
    utility.wait_for_index_building_complete(a.collection, index_name="vector", using="mm")
    print("  index built")

    print(f"loading with replica_number={a.replicas}")
    client.load_collection(a.collection, replica_number=a.replicas)
    utility.wait_for_loading_complete(a.collection, using="mm")
    print("  loaded")

    # ---------------------------------------------------------------- record what we got
    segs = utility.get_query_segment_info(a.collection, using="mm")
    def _is_sealed(seg):
        st = str(getattr(seg, "state", ""))
        return "Sealed" in st or "Flushed" in st or st == "3"
    sealed = [s for s in segs if _is_sealed(s)]
    by_node = {}
    for s in segs:
        for nid in (list(s.nodeIds) or [-1]):
            by_node.setdefault(int(nid), []).append(int(s.segmentID))
    manifest = {
        "collection": a.collection,
        "dataset": a.dataset,
        "vectors_ingested": done,
        "dim": ds.dim,
        "metric": a.metric,
        "index": a.index,
        "index_params": index_params_dict,
        "num_shards_requested": a.shards,
        "num_shards_reported": got_shards,
        "insert_chunks": a.chunks,
        "replica_number": a.replicas,
        "segments_total": len(segs),
        "segments_sealed": len(sealed),
        "segments_per_node": {str(k): len(v) for k, v in sorted(by_node.items())},
        "rows_per_segment": sorted(int(s.num_rows) for s in segs),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out = os.path.join(os.environ.get("MM_RESULTS", "."), "collections", f"{a.collection}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(json.dumps(manifest, indent=2))
    print(f"\nmanifest -> {out}")
    print("next: 07_probe_snm.py --collection %s" % a.collection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
