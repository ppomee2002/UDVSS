#!/bin/bash
# Event stream for the unattended run: milestones, failures, dead/stalled processes.
WS=/home/dev_path/experiments/workload-scale-sq8; LOG=$WS/run_all.log; CSV=$WS/results/runs.csv
seen_log=$(wc -l < $LOG 2>/dev/null || echo 0); seen_csv=$(wc -l < $CSV 2>/dev/null || echo 0)
last_cpu=""; same=0; gtfail=0
while true; do
  n=$(wc -l < $LOG)
  [ $n -gt $seen_log ] && { sed -n "$((seen_log+1)),${n}p" $LOG | grep -E --line-buffered "FATAL|DONE|=== |^LOAD|Error|error|abort|Abort"; seen_log=$n; }
  if [ -f $CSV ]; then c=$(wc -l < $CSV); [ $c -gt $seen_csv ] && { sed -n "$((seen_csv+1)),${c}p" $CSV | grep -v '^scale' | cut -d, -f1-10 | sed 's/^/ROW /'; seen_csv=$c; }; fi
  grep -q "RUN_ALL DONE" $LOG && { echo "PIPELINE FINISHED"; exit 0; }
  pgrep -f "[3][01]_run_" >/dev/null || { echo "PIPELINE PROCESS GONE (no DONE line)"; exit 1; }
  timeout 20 ssh -o BatchMode=yes ubuntu@node109 "pgrep -f '[b]undle-sq8/bin/crimson-osd -i' >/dev/null" || { sleep 30; timeout 20 ssh -o BatchMode=yes ubuntu@node109 "pgrep -f '[b]undle-sq8/bin/crimson-osd -i' >/dev/null" || echo "OSD PROCESS DOWN on 109"; }
  [ $gtfail = 0 ] && timeout 20 ssh -o BatchMode=yes ubuntu@node109 "grep -q Traceback /home/ubuntu/deep1b/gt.log" && { echo "GT FAILED"; gtfail=1; }
  p=$(pgrep -f "[c]eph-vector-bench-native.*ws_lsh" | head -1)
  if [ -n "$p" ]; then
    cpu=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null)
    if [ "$cpu" = "$last_cpu" ]; then same=$((same+1)); else same=0; fi
    last_cpu=$cpu; [ $same -eq 10 ] && echo "BENCH STALLED: pid $p cpu unchanged 10 min"
  else last_cpu=""; same=0; fi
  tick=$((${tick:-0}+1))
  if [ $((tick % 5)) = 0 ]; then
    source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh 2>/dev/null
    timeout 30 $BIN/ceph -c $CEPH_CONF tell osd.0 dump_metrics memory 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); a={};t={}
for m in d.get('metrics',d):
  for k,v in m.items():
    if k=='memory_allocated_memory': a[v['shard']]=v['value']
    if k=='memory_total_memory': t[v['shard']]=v['value']
hot=[f'{s}:{a[s]/2**30:.2f}/{t[s]/2**30:.0f}G' for s in a if a[s]>0.85*t[s]]
if hot: print('OSD MEMORY HIGH', *hot)" 2>/dev/null
  fi
  sleep 60
done
