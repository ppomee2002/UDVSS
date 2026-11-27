#!/bin/bash
# Event stream for the unattended ws run: milestones, rows, failures, dead/stalled processes.
WS=/home/dev_path/experiments/paper-100m/ws; LOG=$WS/run_all.log; CSV=$WS/results/runs.csv
until [ -f $LOG ]; do sleep 5; done
seen_log=$(wc -l < $LOG); seen_csv=$(wc -l < $CSV 2>/dev/null || echo 0); last_cpu=""; same=0; tick=0
while true; do
  n=$(wc -l < $LOG)
  [ $n -gt $seen_log ] && { sed -n "$((seen_log+1)),${n}p" $LOG | grep -E --line-buffered "FATAL|DONE|=== |^LOAD|objects listed|rror|bort"; seen_log=$n; }
  if [ -f $CSV ]; then c=$(wc -l < $CSV); [ $c -gt $seen_csv ] && { sed -n "$((seen_csv+1)),${c}p" $CSV | grep -v '^scale' | cut -d, -f1,3,5,6,11,12,13,14,16,17 | sed 's/^/ROW scale,mode,M,rep,recall,cand,mean,p50,p99,req: /'; seen_csv=$c; }; fi
  grep -q "RUN_ALL DONE" $LOG && { echo "PIPELINE FINISHED"; exit 0; }
  pgrep -f "[2][012]_(all|redo)" >/dev/null || { echo "PIPELINE PROCESS GONE (no DONE line)"; exit 1; }
  timeout 20 ssh -o BatchMode=yes ubuntu@node109 "pgrep -f '[b]undle-lshsq8/bin/crimson-osd -i' >/dev/null" || { sleep 30; timeout 20 ssh -o BatchMode=yes ubuntu@node109 "pgrep -f '[b]undle-lshsq8/bin/crimson-osd -i' >/dev/null" || echo "OSD PROCESS DOWN on 109"; }
  p=$(pgrep -x ceph-vector-ben | while read q; do grep -qa ws_lshsq8 /proc/$q/cmdline && echo $q; done | head -1)
  if [ -n "$p" ]; then
    cpu=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null)
    if [ "$cpu" = "$last_cpu" ]; then same=$((same+1)); else same=0; fi
    last_cpu=$cpu; [ $same -eq 10 ] && echo "BENCH STALLED: pid $p cpu unchanged 10 min"
  else last_cpu=""; same=0; fi
  tick=$((tick+1))
  if [ $((tick % 5)) = 0 ]; then
    source $WS/scripts/env.sh 2>/dev/null
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
