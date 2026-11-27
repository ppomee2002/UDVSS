#!/bin/bash
# Per scale: fresh pool + load, then M sweep x WS_REPS (full PG scan). Resumable:
# skips (scale,M,rep) rows already in runs.csv; reloads only if the scale has no row.
source /home/dev_path/experiments/paper-100m/ws/scripts/env.sh
D=$WS/scripts; mkdir -p $RES
for S in ${WS_SCALES:-1m 10m 100m}; do
  log "=== scale $S ==="
  if ! grep -q "^$S," $RES/runs.csv 2>/dev/null || [ "${FORCE_LOAD:-}" = 1 ]; then
    $D/10_load.sh $S || { log "FATAL: load $S"; exit 1; }
  fi
  for REP in $(seq 1 $WS_REPS); do for M in $WS_MS; do for MODE in full tree; do
    grep -q "^$S,[0-9]*,$MODE,1024,$M,$REP," $RES/runs.csv 2>/dev/null && continue
    C=1
    if [ $MODE = full ] && [ $S != 1m ]; then [ $REP -gt $WS_FULL_REPS ] && continue; C=$WS_FULL_C; fi
    $D/11_query.sh $S $MODE $M $REP $C || { log "FATAL: query $S $MODE M$M r$REP"; exit 1; }
  done; done; done
done
log "=== RUN_ALL DONE ==="
