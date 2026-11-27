"""Control-plane view of a loaded collection: shards, delegators, segment placement.

This is what Milvus *declares*. The measured counterpart lives in `milvus_metrics`.
Keep the two apart: a control-plane number is an upper bound on participation
(QueryCoord lists a node for a shard even if the delegator ends up skipping it for
holding zero segments -- `organizeSubTask(..., skipEmpty=True)`).

pymilvus object shapes verified against pymilvus 2.6.17:
    MilvusClient.describe_replica(name) -> List[ReplicaInfo]
    ReplicaInfo{ id, shards, group_nodes, resource_group }   (client/types.py:481)
    Shard{ channel_name, shard_nodes (set), shard_leader }   (client/types.py:374)

NOTE: `utility.get_replicas` was REMOVED from pymilvus 2.6's ORM surface; calling it
raises AttributeError. Use describe_replica.
"""
from __future__ import annotations

from typing import Any, Dict, List

from pymilvus import utility


def shard_topology(collection: str, client=None, alias: str = "mm") -> Dict[str, Any]:
    """Per-shard QueryNode counts as QueryCoord declares them.

    Returns per replica a list of {channel, leader, nodes, n_nodes}, plus
    cp_N_per_shard_{min,avg,max} over the shards of the first replica -- the first
    replica is the right scope because the proxy's balancer serves one query from
    exactly one replica (`lb_policy.go: selectNode`).
    """
    out: Dict[str, Any] = {"replicas": [], "replica_count": 0}
    if client is None:
        out["error"] = "shard_topology needs a MilvusClient (describe_replica)"
        return out
    try:
        reps = client.describe_replica(collection)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for g in (reps or []):
        shards = []
        for sh in getattr(g, "shards", []) or []:
            nodes = sorted(int(x) for x in (getattr(sh, "shard_nodes", None) or []))
            shards.append({
                "channel": getattr(sh, "channel_name", None),
                "leader": getattr(sh, "shard_leader", None),
                "nodes": nodes,
                "n_nodes": len(nodes),
            })
        out["replicas"].append({
            "replica_id": getattr(g, "id", None),
            "resource_group": getattr(g, "resource_group", None),
            "group_nodes": sorted(int(x) for x in (getattr(g, "group_nodes", None) or [])),
            "shards": shards,
            "n_shards": len(shards),
        })
    out["replica_count"] = len(out["replicas"])

    counts: List[int] = []
    if out["replicas"]:
        counts = [s["n_nodes"] for s in out["replicas"][0]["shards"]]
    if counts:
        out["cp_N_per_shard_min"] = min(counts)
        out["cp_N_per_shard_max"] = max(counts)
        out["cp_N_per_shard_avg"] = round(sum(counts) / len(counts), 4)
        out["cp_shard_nodes"] = ";".join(
            f"{s['channel'].split('_')[-1] if s['channel'] else '?'}:{s['n_nodes']}"
            for s in out["replicas"][0]["shards"]
        )
    else:
        out["cp_N_per_shard_min"] = out["cp_N_per_shard_max"] = out["cp_N_per_shard_avg"] = ""
        out["cp_shard_nodes"] = ""
    return out


def segment_placement(collection: str, alias: str = "mm") -> Dict[str, Any]:
    """Segment -> node map, and the per-node segment count (the control-plane M input)."""
    segs = utility.get_query_segment_info(collection, using=alias)
    per_node: Dict[int, Dict[str, int]] = {}
    for s in segs:
        for nid in (list(s.nodeIds) or [-1]):
            d = per_node.setdefault(int(nid), {"segments": 0, "rows": 0})
            d["segments"] += 1
            d["rows"] += int(s.num_rows)
    counts = [v["segments"] for k, v in per_node.items() if k >= 0]
    return {
        "segments_total": len(segs),
        "segments_per_node": {str(k): v for k, v in sorted(per_node.items())},
        "nodes_with_segments": len(counts),
        "cp_segments_per_node_min": min(counts) if counts else "",
        "cp_segments_per_node_max": max(counts) if counts else "",
        "cp_segments_per_node_avg": round(sum(counts) / len(counts), 4) if counts else "",
    }
