#!/bin/bash
# one-off (user 2026-10-23): after the running chunk, pause, set LOAD_CONC=1024, resume.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
until grep -q "30: STOP file -> pausing before chunk 140000000" $LOG; do sleep 20; done
while ps -eo args | grep -q "[s]cripts/all.sh"; do sleep 5; done
sed -i 's/^LOAD_CONC=256$/LOAD_CONC=1024/' $S/env.sh
grep -q "^LOAD_CONC=1024$" $S/env.sh || { log "switch: sed failed"; exit 1; }
rm -f $X/STOP
log "switch: LOAD_CONC 256 -> 1024 from chunk 140000000 (user 2026-10-23)"
cd $X && nohup setsid bash $S/all.sh > $X/logs/all.out 2>&1 < /dev/null &
