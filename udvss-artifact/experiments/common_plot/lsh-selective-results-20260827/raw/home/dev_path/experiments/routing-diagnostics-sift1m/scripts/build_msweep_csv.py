#!/usr/bin/env python3
"""Parse logs/msweep/m*.log FANOUT_QUERY lines into results/msweep_summary.csv."""
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
M_LIST = [1, 2, 4, 8, 16, 32, 64, 128, 256]

FIELD_RE = re.compile(r"(\w+)=([\d.eE+-]+)")

rows = []
for m in M_LIST:
    log_path = ROOT / "logs" / "msweep" / f"m{m}.log"
    text = log_path.read_text()
    line = next(l for l in text.splitlines() if l.startswith("FANOUT_QUERY"))
    fields = dict(FIELD_RE.findall(line))
    rows.append({
        "M": m,
        "recall_at_10": float(fields["recall_at_top_k"]),
        "actual_avg_pg_probes": int(fields["subrequests_per_logical_query"]),
        "avg_matching_records_per_query": float(fields["avg_total_matching_records_per_query"]),
        "avg_distance_computations_per_query": float(fields["avg_total_distance_computations_per_query"]),
        "qps": float(fields["logical_qps"]),
        "latency_us_avg": float(fields["logical_latency_us_avg"]),
        "latency_us_p50": float(fields["logical_latency_us_p50"]),
        "latency_us_p95": float(fields["logical_latency_us_p95"]),
        "completed": int(fields["completed"]),
        "failures": int(fields["failures"]),
        "seconds": float(fields["seconds"]),
    })

out_path = ROOT / "results" / "msweep_summary.csv"
with open(out_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(f"wrote {out_path} ({len(rows)} rows)")
for r in rows:
    print(f"  M={r['M']:<4d} recall={r['recall_at_10']:.4f} qps={r['qps']:8.2f} "
          f"p50={r['latency_us_p50']/1000:7.1f}ms p95={r['latency_us_p95']/1000:7.1f}ms")
