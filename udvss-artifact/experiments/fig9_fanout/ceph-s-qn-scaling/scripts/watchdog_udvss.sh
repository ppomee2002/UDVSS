#!/bin/bash
# Exits (=> notifies) on: run finished, FAILED/FATAL in the run log, an OSD
# down or a crimson-osd process missing, slow ops > 5 min, an abort /
# out-of-space / bad_alloc line in any OSD log, or no log progress for 15 min.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
B=$EXP/results_sq8_cpu126/run.log
while sleep 120; do
  grep -q "udvss cpu126 done" $B && { echo "DONE"; break; }
  f=$(grep -E "FAILED|FATAL" $B | tail -1); [ -n "$f" ] && { echo "RUNLOG: $f"; break; }
  idle=$(( $(date +%s) - $(stat -c %Y $B) ))
  [ $idle -gt 900 ] && { echo "STALL: run log idle ${idle}s"; break; }
  s=$(timeout 20 $BIN/ceph -c $CEPH_CONF -s -f json 2>/dev/null) || continue   # cluster between configs
  read up num slow < <(echo "$s" | python3 -c '
import json,sys; j=json.load(sys.stdin); o=j["osdmap"]
slow=[c for c in j["health"]["checks"] if c=="SLOW_OPS"]
print(o["num_up_osds"], o["num_osds"], len(slow))')
  procs=$(r109 "pgrep -f '[b]undle-sq8/bin/crimson-osd -i' | wc -l")
  # a fresh cluster's OSDs take a few seconds to come up: only alarm when
  # the condition holds on two consecutive checks (~2 min)
  if [ "$num" -gt 0 ] && { [ "$up" != "$num" ] || [ "$procs" -lt "$num" ]; }; then
    down_n=$(( ${down_n:-0} + 1 ))
    [ $down_n -ge 2 ] && { echo "OSD DOWN: $up/$num up, $procs procs"; break; }
  else down_n=0; fi
  err=$(r109 "grep -hE 'Aborting|out of space|bad_alloc|ceph_assert' $RDIR/out/osd.*.log 2>/dev/null | grep -v register_command | tail -1")
  [ -n "$err" ] && { echo "OSD LOG: ${err:0:300}"; break; }
  if [ "$slow" = 1 ]; then slow_n=$(( ${slow_n:-0} + 1 )); [ $slow_n -ge 3 ] && { echo "SLOW OPS > 5 min"; break; }
  else slow_n=0; fi
done
date -u; grep -vE "dangerous|DEVELOPER|pg_upmap|^set |pool '" $B | grep -E "run |===|FAILED|FATAL|N=" | tail -6
