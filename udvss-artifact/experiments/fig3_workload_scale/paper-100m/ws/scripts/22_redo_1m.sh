#!/bin/bash
# User request 2026-10-08: after 10m/100m, reload 1m once and measure every 1m row on
# that single pool with the same procedure as 10m/100m (per rep, per M: full at C=32
# on rep 1 only, then tree at C=1; 3 reps). The earlier 1m rows (first load, plus tree
# M384 rep3 from a second load) move to results/runs_1m_superseded.csv; they are not
# averaged with these.
source /home/dev_path/experiments/paper-100m/ws/scripts/env.sh
D=$WS/scripts; S=1m
if grep -q "^$S," $RES/runs.csv; then
  { head -1 $RES/runs.csv; grep "^$S," $RES/runs.csv; } > $RES/runs_1m_superseded.csv
  grep -v "^$S," $RES/runs.csv > $RES/runs.csv.tmp && mv $RES/runs.csv.tmp $RES/runs.csv
  log "moved $(($(wc -l < $RES/runs_1m_superseded.csv)-1)) old 1m rows to runs_1m_superseded.csv"
fi
log "=== 1m redo: reload ==="
$D/10_load.sh $S || { log "FATAL: 1m redo load"; exit 1; }
for REP in $(seq 1 $WS_REPS); do for M in $WS_MS; do for MODE in full tree; do
  grep -q "^$S,[0-9]*,$MODE,1024,$M,$REP," $RES/runs.csv && continue
  C=1
  if [ $MODE = full ]; then [ $REP -gt $WS_FULL_REPS ] && continue; C=$WS_FULL_C; fi
  $D/11_query.sh $S $MODE $M $REP $C || { log "FATAL: 1m redo $MODE M$M r$REP"; exit 1; }
done; done; done
log "=== 1M REDO DONE ==="
