#!/bin/bash
# Fresh cluster -> correctness smoke -> checks. Stops at the first failure.
cd /home/dev_path/experiments/lsh-aligned-1m
source scripts/env.sh
log "=== 00_cluster ==="; scripts/00_cluster.sh || { log "FATAL: cluster"; exit 1; }
log "=== 10_correctness ==="; scripts/10_correctness.sh || { log "FATAL: correctness run"; exit 1; }
log "=== 11_check ==="; python3 scripts/11_check.py || { log "FAIL: correctness checks"; exit 1; }
log "=== CORRECTNESS OK ==="
