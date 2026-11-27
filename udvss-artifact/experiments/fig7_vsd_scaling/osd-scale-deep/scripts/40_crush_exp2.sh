#!/bin/bash
# Usage: 40_crush_exp2.sh -- experiment 2, CRUSH side (user 2026-10-21 night):
#   waits for 30_mmax_exp2.sh, then REPLACES the 100m pivot cluster with a
#   12-OSD cluster holding the same 100m vectors under CRUSH-style hash
#   placement (scale 100mh), and sweeps recall@10 / candidates vs M = 1..16384
#   (one pass of the 1000 queries per M, C = 1, B = 256, same as the UDVSS
#   sweep) -> results/exp2_crush.csv.
source $HOME/osdscale/scripts/env.sh
set -e
L=$X/logs/30_mmax_exp2.log
log "40: waiting for 30_mmax_exp2"
until grep -q "=== 30 complete ===" $L; do
  ps -eo args | grep -q "[3]0_mmax_exp2.sh" || die "30_mmax_exp2.sh is gone without completing"
  sleep 60
done
log "40: 30 complete -> rebuild 12 OSDs for 100mh"
bash $X/scripts/01_osds.sh 100mh 12 > $X/logs/01_osds_100mh_osd12.log 2>&1 || die "01_osds 100mh failed"
log "40: load 100mh (hash placement)"
bash $X/scripts/02_load.sh 100mh > $X/logs/02_load_100mh_osd12.log 2>&1 || die "02_load 100mh failed"
grep -q "LOADED_OK 100mh" $X/logs/02_load_100mh_osd12.log || die "100mh not LOADED_OK"

scale_vars 100mh
N=$(num_up); [ "$N" = 12 ] || die "expected 12 OSDs up, got $N"
R=$X/results/$SCALE/osd$N; mkdir -p $R/runs
QL=1000
HZ=$(getconf CLK_TCK)
IDXARGS="${IDXARGS/--pivot-probe-budget $PROBE_BUDGET/--pivot-probe-budget 16384}"
eval "$(sed -n '/^cpu_j()/,/^}$/p' $X/scripts/03_measure.sh)"
BATCH=256
CSV=$X/results/exp2_crush.csv
log "40: exp2 CRUSH sweep, one pass per M"
for M in 1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192 16384; do
  q=$(one $M 1 exp2 0); log "  exp2 crush M=$M qps=$q"
done
log "=== 40 complete ==="
