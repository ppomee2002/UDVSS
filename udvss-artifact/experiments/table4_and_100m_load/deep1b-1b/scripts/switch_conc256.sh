#!/bin/bash
# one-off (2026-10-24): 1024 in-flight ran 2.6-2.8k/s vs 256's steady 3.1-3.3k/s
# (the 5.8k/s "1024" reading was a 60 s burst right after a pause). After the
# running chunk: pause, LOAD_CONC=256, resume.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
touch $X/STOP
until grep -q "30: STOP file -> pausing before chunk 140000000" $LOG; do sleep 20; done
while ps -eo args | awk '$1=="bash" && $2=="scripts/all.sh"' | grep -q .; do sleep 5; done
sed -i 's/^LOAD_CONC=1024 .*/LOAD_CONC=256    # steady 3.1-3.3k\/s; 1024 2.6-2.8k\/s, 4096 ~3k\/s with conflicts (2026-10-24)/' $S/env.sh
grep -q "^LOAD_CONC=256 " $S/env.sh || { log "switch256: sed failed"; exit 1; }
rm -f $X/STOP
log "switch256: LOAD_CONC 1024 -> 256 from chunk 140000000"
cd $X && nohup setsid bash $S/all.sh > $X/logs/all.out 2>&1 < /dev/null &
