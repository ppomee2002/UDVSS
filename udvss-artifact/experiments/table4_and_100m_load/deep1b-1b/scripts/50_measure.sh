#!/bin/bash
# scale-deep protocol (as d100m/run.sh), SHORTENED (user 2026-10-24, deadline
# 11/1, like mem48's FAST_PROTO): warm-up (discarded) -> C ladder 20/40/80 at
# M16 with early stop (a step that is not >= 3% faster than the best smaller C
# ends the ladder; smallest C within 3% of the best) -> M 16 32 64 128, 1 rep
# each; every run time-bound (>= 120 s after one warm-up round), B=1024.
# Full protocol: 50_measure.sh.bak-full.
# Rows -> results/runs.csv.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
set -e
grep -q LOADED_OK $X/logs/count.log || die "50: not LOADED_OK"
D=$X/results/runs; mkdir -p $D; CSV=$X/results/runs.csv
[ -s $CSV ] || echo "m,c,tag,status,qps,recall,cand,p50_us,p95_us,p99_us,completed,seconds,failures,osd_cores,client_cores,osd_rss_g,client_rss_max_g,start_utc,log" > $CSV
one() {  # one <M> <C> <tag> [rounds-override] -> prints qps
  local M=$1 C=$2 TAG=$3 L=$D/m$1_c$2_$3.log HZ=$(getconf CLK_TCK) P=$(osd_pid)
  local ROUNDS=$(( (C * BATCH * 4 + 999) / 1000 )); [ $ROUNDS -lt 4 ] && ROUNDS=4
  local S=$(date -u +%FT%TZ) o0 o1 t0 t1 cmax=0 r
  # warm-up call (min_seconds 0): exactly <rounds> passes, as scale-deep's activate
  local MQ=1000; [ "${5:-}" = 0 ] && MQ=$(( ${4:-$ROUNDS} * 1000 ))
  taskset -c $CLIENT_CORES $QBENCH --operation fanout-query --pool $POOL --index $INDEX $ROUTE --pivot-probe-budget $PROBE_BUDGET $QDSET \
    --m $M --query-limit 1000 --rounds ${4:-$ROUNDS} --query-concurrency $C --fanout-mode parallel \
    --query-batch-size $BATCH --warmup-rounds 1 --min-queries $MQ --min-seconds ${5:-$MIN_SECONDS} > $L 2>&1 &
  local BP=$!
  o0=$(awk '{print $14+$15}' /proc/$P/stat); t0=$(date +%s.%N)
  while kill -0 $BP 2>/dev/null; do
    r=$(ps -o rss= -p $BP 2>/dev/null | awk '{print int($1/1024)}'); [ -n "$r" ] && [ "$r" -gt $cmax ] && cmax=$r
    sleep 5; done
  local RC=0; wait $BP || RC=$?
  o1=$(awk '{print $14+$15}' /proc/$P/stat 2>/dev/null || echo $o0); t1=$(date +%s.%N)
  python3 - "$L" "$CSV" "$M" "$C" "$TAG" "$RC" "$o0" "$o1" "$t0" "$t1" "$HZ" "$(ps -o rss= -p $P | awk '{printf "%.1f", $1/2^20}')" "$(awk -v m=$cmax 'BEGIN{printf "%.1f", m/1024}')" "$S" <<'PY'
import sys,re,os
log,csv,m,c,tag,rc,o0,o1,t0,t1,hz,orss,cmax,start=sys.argv[1:]
lines=[l for l in open(log,errors='replace') if l.startswith('FANOUT_QUERY')]
kv=dict(re.findall(r'(\w+)=(\S+)',lines[-1])) if lines else {}
sec=float(kv.get('seconds','nan')); w=float(t1)-float(t0)
ok=rc=='0' and kv.get('failures')=='0' and 'recall_at_top_k' in kv
row=[m,c,tag,'OK' if ok else 'FAILED',kv.get('logical_qps','NA'),kv.get('recall_at_top_k','NA'),
     kv.get('avg_total_matching_records_per_query','NA'),kv.get('logical_latency_us_p50','NA'),
     kv.get('logical_latency_us_p95','NA'),kv.get('logical_latency_us_p99','NA'),kv.get('completed','NA'),
     kv.get('seconds','NA'),kv.get('failures','NA'),
     f'{(int(o1)-int(o0))/float(hz)/w:.2f}' if w>0 else 'NA','NA',orss,cmax,start,os.path.basename(log)]
open(csv,'a').write(','.join(map(str,row))+'\n')
print(f"M={m} C={c} {tag} {'OK' if ok else 'FAILED'} qps={row[4]} recall={row[5]} cand={row[6]} osd_rss={orss}G client_max={cmax}G", file=sys.stderr)
print(row[4] if ok else 'NA')
PY
}
log "50: warm-up M16 C40 (discarded)"; one 16 40 warmup 8 0 >/dev/null 2>>$LOG
declare -A Q
best=0
for C in 20 40 80; do
  q=$(one 16 $C tune 2>>$LOG); log "50: ladder C=$C qps=$q"
  [ "$q" = NA ] && break
  Q[$C]=$q
  awk -v a=$q -v b=$best 'BEGIN{exit !(a < 1.03*b)}' && { log "50: ladder early stop at C=$C"; break; }
  awk -v a=$q -v b=$best 'BEGIN{exit !(a>b)}' && best=$q
done
max=0; for C in "${!Q[@]}"; do awk -v a=${Q[$C]} -v b=$max 'BEGIN{exit !(a>b)}' && max=${Q[$C]}; done
BC=""; for C in 20 40 80; do [ -n "${Q[$C]:-}" ] && awk -v a=${Q[$C]} -v m=$max 'BEGIN{exit !(a>=0.97*m)}' && { BC=$C; break; }; done
[ -n "$BC" ] || die "50: C ladder gave nothing"
log "50: chosen C=$BC (best $max)"; echo "$BC $max" > $X/results/chosen_c.txt
for M in 16 32 64 128; do for r in 1; do
  [ -n "$(osd_pid)" ] || die "50: OSD died"
  q=$(one $M $BC r$r 2>>$LOG); log "50: M=$M C=$BC r$r qps=$q"
done; done
log "=== 50 complete ==="
