#!/bin/bash
# Waits for 19_correctness_driver.sh; only if it ends CORRECTNESS OK, runs
# the selective re-measure: old arm (control) then new arm, then the plots.
cd /home/dev_path/experiments/lsh-aligned-1m
source scripts/env.sh
L=logs/19_correctness_driver.log
while ! grep -qE "CORRECTNESS OK|FATAL|FAIL" $L; do sleep 30; done
grep -q "CORRECTNESS OK" $L || { log "correctness did not pass; selective NOT started"; tail -15 $L; exit 1; }
log "correctness OK; starting selective"
for arm in old new; do
  selective/scripts/20_selective.sh $arm 10000 || { log "FATAL: selective $arm"; exit 1; }
done
cd selective
for arm in old new; do
  python3 scripts/plot_tradeoff.py results/${arm}_selective_q10000.csv \
    --name selective_tradeoff_$arm > logs/plot_$arm.log 2>&1 || { log "FATAL: plot $arm"; exit 1; }
done
log "=== selective re-measure complete ==="
