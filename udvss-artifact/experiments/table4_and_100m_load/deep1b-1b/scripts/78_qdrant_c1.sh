#!/bin/bash
# Qdrant 1B at C=1 (user 2026-10-29, as Milvus 1B tiered): throughput is ~1.7 vec/s whatever C,
# so C=20 units would take ~12000 s. Same run_one.py call as run_engine.sh (client taskset
# 20-23,44-47, 120 s time-bound, 1 rep, co-run 0); ef 64 first, then 10..1024.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
R=/home/dev_path/experiments/scale-deep/engines
OUT=$R/results/qdrant/deep1b1b_c1; mkdir -p $OUT; LOG=$R/logs/qdrant_deep1b1b_c1.log
export QB_TIMEOUT=7200 QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1
log "78: Qdrant 1B C=1 measure (ef 64 10 16 32 128 256 512 1024) -> $OUT"
cd /home/dev_path/corun
for EF in 64 10 16 32 128 256 512 1024; do
  out=$(taskset -c 20-23,44-47 .venv/bin/python -u scripts/run_one.py --engine qdrant --dataset deep1b1b \
        --ef $EF --concurrency 1 --repeat r1 --min-seconds 120 --out-dir $OUT --co-run 0 2>&1 | tee -a $LOG | grep -E " qps=" | tail -1)
  log "78: ef=$EF C=1: ${out:-NO RESULT (see $LOG)}"
done
log "=== QDRANT 1B C1 DONE ==="
