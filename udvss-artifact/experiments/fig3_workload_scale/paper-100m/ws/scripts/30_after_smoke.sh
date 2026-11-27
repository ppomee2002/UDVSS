#!/bin/bash
# Waits for the split/merge correctness smoke on 7390; if it passed (EXIT 0),
# runs the ws pipeline 1m -> 10m -> 100m. Otherwise does nothing.
L=/home/dev_path/experiments/paper-100m/sm/logs/10_correctness.log
WS=/home/dev_path/experiments/paper-100m/ws
until grep -q '^EXIT' $L 2>/dev/null; do sleep 10; done
if ! grep -q '^EXIT 0$' $L; then echo "[$(date -u +%FT%TZ)] FATAL: smoke failed, ws not started" >> $WS/run_all.log; exit 1; fi
echo "[$(date -u +%FT%TZ)] smoke passed; === ws pipeline start ===" >> $WS/run_all.log
exec $WS/scripts/20_all.sh >> $WS/run_all.log 2>&1
