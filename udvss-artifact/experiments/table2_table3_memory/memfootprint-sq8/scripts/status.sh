#!/usr/bin/env bash
# quick campaign status: chain liveness, last ROW per series, pods, node memory, disks
cd /home/dev_path/experiments/memfootprint-sq8
while read n p; do kill -0 $p 2>/dev/null && st=alive || st=exited; echo "$n $st | $(grep -E 'START|END|CHAIN' logs/chains/$n.out | tail -1)"; done < logs/chains/pids
echo; for d in results/p*/*/; do [ -f $d/run.log ] && echo "$d $(grep ROW $d/run.log | tail -1 | cut -c1-260)"; done
echo; scripts/kc.sh -n memexp get pods -o wide --no-headers 2>/dev/null | awk '{print $1,$3,$4,$6}'
scripts/kc.sh top node milvus-stream110 --no-headers 2>/dev/null
df -h / /home/dev_path | tail -2
