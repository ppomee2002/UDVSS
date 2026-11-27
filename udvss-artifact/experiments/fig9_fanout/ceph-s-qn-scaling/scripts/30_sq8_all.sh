#!/bin/bash
# SQ8 pipeline: for each routing, pick pg_num (20) then measure (21).
# Stops before the Milvus SQ8 rerun, which the user scheduled last.
RES=/home/dev_path/experiments/ceph-s-qn-scaling/results_sq8
for r in lsh pivot; do
  # the sweep's verdict is final once written; 21 resumes by label
  grep -q "^BEST" $RES/best_$r.txt 2>/dev/null || ROUTE=$r /home/dev_path/experiments/ceph-s-qn-scaling/scripts/20_pgsweep_sq8.sh
  ROUTE=$r /home/dev_path/experiments/ceph-s-qn-scaling/scripts/21_final_sq8.sh
done
echo "[$(date -u +%FT%TZ)] === sq8 all done ==="
