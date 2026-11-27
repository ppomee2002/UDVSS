#!/bin/bash
# Usage: 20_diag.sh <scale> <M> <C> <tag> -- one time-bound query run on the
# loaded pool and current OSDs, sampled mid-run for 30 s:
#   per-thread CPU (top -H) of every OSD and of the bench client,
#   system-wide memory bandwidth (uncore_imc cas_count) and IPC (perf stat -a).
# Read-only on the pool. Output: results/diag/<tag>/.
source $HOME/osdscale/scripts/env.sh
set -e
scale_vars $1; M=$2; C=$3; TAG=$4
D=$X/results/diag/$TAG; mkdir -p $D
N=$(num_up)
log "diag $SCALE N=$N M=$M C=$C -> $D"
bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $DSET \
  --m $M --query-limit 1000 --rounds 400 --query-concurrency $C \
  --fanout-mode parallel --query-batch-size $BATCH --warmup-rounds 1 \
  --min-queries 1000 --min-seconds 150 > $D/bench.log 2>&1 &
BPID=$!
# wait until the query phase is running (OSDs busy), then 20 s more
for i in $(seq 1 120); do sleep 2; [ "$(ps -o %cpu= -p $(osd_pids | head -1) | cut -d. -f1)" -gt 0 ] && break; done
sleep 40
BP=$(ps -eo pid,args | grep "[b]undle/bin/ceph-vector-bench-native" | awk '{print $1}' | head -1)
OP=$(osd_pids | tr '\n' ',' | sed 's/,$//')
top -b -H -d 30 -n 2 -p $OP,$BP -w 200 > $D/top_threads.txt &
sudo perf stat -a -e uncore_imc/cas_count_read/,uncore_imc/cas_count_write/,cycles,instructions \
  -o $D/perf_stat.txt -- sleep 30
wait %2 2>/dev/null || true
for p in $(osd_pids); do echo "$p $(tr '\0' ' ' < /proc/$p/cmdline | grep -oE -- '-i [0-9]+')"; done > $D/osd_pids.txt
echo "$BP bench" >> $D/osd_pids.txt
wait $BPID || log "bench exited non-zero"
grep -a "^FANOUT_QUERY" $D/bench.log | tail -1 | tr ' ' '\n' | grep -E "^(logical_qps|recall_at_top_k|avg_total_matching|logical_latency_us_p50|failures)=" | tr '\n' ' '; echo
log "=== diag done ($TAG) ==="
