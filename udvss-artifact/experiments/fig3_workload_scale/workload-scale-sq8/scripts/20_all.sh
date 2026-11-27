#!/bin/bash
# Full pipeline: one fresh pool per scale, M sweep in tree and full mode.
# Resumes by skipping (scale,mode,M) rows already in results/runs.csv.
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh
D=$WS/scripts
for S in ${WS_SCALES:-1m 10m 100m}; do
  grep -q "^$S," $RES/runs.csv 2>/dev/null && [ "$(grep -c "^$S," $RES/runs.csv)" -ge 8 ] && continue
  grep -q "^$S," $RES/runs.csv 2>/dev/null || $D/10_load.sh $S || exit 1
  for MODE in tree full; do for M in $WS_MS; do
    grep -q "^$S,[0-9]*,$MODE,1024,$M," $RES/runs.csv 2>/dev/null && continue
    $D/11_query.sh $S $MODE $M || exit 1
  done; done
done
log "=== workload-scale all done ==="
