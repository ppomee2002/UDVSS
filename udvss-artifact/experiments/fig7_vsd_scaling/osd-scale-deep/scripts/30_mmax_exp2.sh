#!/bin/bash
# Usage: 30_mmax_exp2.sh -- on the live 100m 12-OSD cluster:
#  (1) 2026-10-22: add "M = max" to the OSD-scaling figure at 12 OSDs:
#      M = 16384 = every PG (full scan), C = 1, B = 256 (B=1024 got the client
#      OOM-killed at ~40G RSS on 2026-10-21: it holds 1024 x 16384 per-PG
#      replies; 256 queries still keep all 16384 PG requests in flight), same
#      time-bound rule as 03 (warm-up round, then >= 1000 queries and >= 120 s),
#      3 reps -> results/runs.csv (tags b256_r1..3).
#  (2) experiment 2, UDVSS side: recall@10 and candidates vs M = 1..8192
#      (16384 comes from (1)), one pass of the 1000 queries per M, C = 1,
#      B = 256 -> results/exp2_udvss.csv. Recall/candidates do not depend on N.
source $HOME/osdscale/scripts/env.sh
set -e
scale_vars 100m
N=$(num_up); [ "$N" = 12 ] || die "expected 12 OSDs up, got $N"
R=$X/results/$SCALE/osd$N; mkdir -p $R/runs
QL=1000
HZ=$(getconf CLK_TCK)
IDXARGS="${IDXARGS/--pivot-probe-budget 96/--pivot-probe-budget 16384}"
eval "$(sed -n '/^cpu_j()/,/^}$/p' $X/scripts/03_measure.sh)"
BATCH=256

CSV=$X/results/runs.csv
log "(1) M=16384 full scan, N=$N, C=1, B=$BATCH, 3 reps"
for r in 1 2 3; do q=$(one 16384 1 b256_r$r); log "  M=16384 C=1 B=$BATCH r$r qps=$q"; done

CSV=$X/results/exp2_udvss.csv
log "(2) exp2 UDVSS sweep, one pass per M"
for M in 1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192; do
  q=$(one $M 1 exp2 0); log "  exp2 M=$M qps=$q"
done
log "=== 30 complete ==="
