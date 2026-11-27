#!/bin/bash
# 76's measurement step only (76 failed at 12:15Z: corun run_one.py had lost --min-seconds;
# restored 2026-10-29 to match the 100M rows). Qdrant already on the standard compose, green.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
QL=$X/logs/qdrant_1b.log
log "77: measuring Qdrant 1B (run_engine FAST protocol; --min-seconds restored; QB_TIMEOUT=7200, PARALLEL_WARMUP=1 [user option A])"
QB_TIMEOUT=7200 PARALLEL_WARMUP=1 QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1 bash /home/dev_path/experiments/scale-deep/engines/run_engine.sh qdrant deep1b1b --no-load >> $QL 2>&1 \
  && log "77: qdrant deep1b1b measured" || log "77: qdrant measurement FAILED (see $QL)"
log "=== QDRANT 1B DONE ==="
