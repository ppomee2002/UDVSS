#!/bin/bash
# Usage: 03_measure.sh <scale> -- on the loaded pool and the current N OSDs:
#   warm-up (discarded) -> C ladder 20/40/80/160 at M=16 (stops once a step
#   gains < 3%) -> smallest C within 3% of the best -> M 16 and 32 x 3 reps.
# Every case is time-bound (>= MIN_SECONDS of queries after one warm-up round),
# B=1024, as scale-deep. Rows: results/runs.csv (one line per case).
source $HOME/osdscale/scripts/env.sh
set -e
scale_vars $1
N=$(num_up)
R=$X/results/$SCALE/osd$N; mkdir -p $R/runs
CSV=$X/results/runs.csv
QL=1000
HZ=$(getconf CLK_TCK)

cpu_j() { local s=0 p v; for p in "$@"; do v=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null || echo 0); s=$((s+v)); done; echo $s; }

one() {  # one <M> <C> <tag> [min_seconds] -> prints qps
  local M=$1 C=$2 TAG=$3 SEC=${4:-$MIN_SECONDS}
  local ROUNDS=$(( (C * BATCH * 4 + QL - 1) / QL )); [ $ROUNDS -lt 4 ] && ROUNDS=4
  local CASE=m${M}_c${C}_${TAG} LOG=$R/runs/m${M}_c${C}_${TAG}.log
  local OP=$(osd_pids | tr '\n' ' ')
  local T0 O0 O1 T1 BPID C1
  bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $DSET \
    --m $M --query-limit $QL --rounds $ROUNDS --query-concurrency $C \
    --fanout-mode parallel --query-batch-size $BATCH --warmup-rounds 1 \
    --min-queries $QL --min-seconds $SEC > $LOG 2>&1 &
  BPID=$!
  # CPU window = query phase only: start once the OSDs are busy (> 0.5 core)
  for i in $(seq 1 300); do
    sleep 2; kill -0 $BPID 2>/dev/null || break
    a=$(cpu_j $OP); sleep 2; b=$(cpu_j $OP)
    [ $((b - a)) -gt $HZ ] && break
  done
  # $BPID is the backgrounded function's subshell; the bench is the process
  # running the bundled binary (only one bench runs at a time).
  local BP=$(ps -eo pid,args | grep "[b]undle/bin/ceph-vector-bench-native" | awk '{print $1}' | head -1)
  T0=$(date +%s.%N); O0=$(cpu_j $OP); C0=$(cpu_j $BP); C1=$C0; O1=$O0; T1=$T0
  local P0="" P1="" p; for p in $OP; do P0="$P0 $(cpu_j $p)"; done; P1=$P0
  while kill -0 $BPID 2>/dev/null; do
    if kill -0 $BP 2>/dev/null; then C1=$(cpu_j $BP); O1=$(cpu_j $OP); T1=$(date +%s.%N)
      P1=""; for p in $OP; do P1="$P1 $(cpu_j $p)"; done; fi
    sleep 2
  done
  local RC=0; wait $BPID || RC=$?
  # per-OSD cores over the same window (load-imbalance check), OSD order = osd_pids
  echo -e "$SCALE\t$N\t$M\t$C\t$TAG\t$(python3 -c "import sys;a=sys.argv[1].split();b=sys.argv[2].split();w=float(sys.argv[3]);print(' '.join(f'{(int(y)-int(x))/$HZ/w:.2f}' for x,y in zip(a,b)) if w>0 else '')" "$P0" "$P1" "$(awk -v a=$T0 -v b=$T1 'BEGIN{print b-a}')")" >> $R/per_osd_cores.tsv
  echo -e "$SCALE\t$N\t$M\t$C\t$TAG\t$(for p in $OP; do echo "$p $(cat /proc/$p/cmdline | tr '\0' ' ' | grep -oE -- '-i [0-9]+' | cut -d' ' -f2)"; done | tr '\n' ';')" >> $R/osd_ids.tsv

  python3 - "$LOG" "$CSV" "$SCALE" "$N" "$M" "$C" "$TAG" "$RC" "$T0" "$T1" "$O0" "$O1" "$C0" "$C1" "$HZ" <<'PY'
import sys,re,os
log,csv,scale,n,m,c,tag,rc,t0,t1,o0,o1,c0,c1,hz=sys.argv[1:]
lines=[l for l in open(log,errors='replace') if l.startswith('FANOUT_QUERY')]
kv=dict(re.findall(r'(\w+)=(\S+)',lines[-1])) if lines else {}
w=float(t1)-float(t0); hz=float(hz)
osd=(int(o1)-int(o0))/hz/w if w>0 else float('nan')
cli=(int(c1)-int(c0))/hz/w if w>0 else float('nan')
ok = rc=='0' and kv.get('failures')=='0' and 'recall_at_top_k' in kv
row=dict(scale=scale,osd=n,m=m,c=c,tag=tag,status='OK' if ok else 'FAILED',
  qps=kv.get('logical_qps','NA'),recall=kv.get('recall_at_top_k','NA'),
  cand=kv.get('avg_total_matching_records_per_query','NA'),
  p50_us=kv.get('logical_latency_us_p50','NA'),p95_us=kv.get('logical_latency_us_p95','NA'),
  p99_us=kv.get('logical_latency_us_p99','NA'),completed=kv.get('completed','NA'),
  seconds=kv.get('seconds','NA'),failures=kv.get('failures','NA'),
  osd_cores=f'{osd:.2f}',client_cores=f'{cli:.2f}',cpu_window_s=f'{w:.1f}',
  log=os.path.relpath(log,os.path.dirname(csv)))
new=not os.path.exists(csv) or os.path.getsize(csv)==0
with open(csv,'a') as f:
    if new: f.write(','.join(row)+'\n')
    f.write(','.join(str(v) for v in row.values())+'\n')
print(' '.join(f'{k}={v}' for k,v in row.items() if k!='log'), file=sys.stderr)
print(row['qps'] if ok else 'NA')
PY
}

log "measure $SCALE N=$N (pool $POOL, PG $PG)"
log "warm-up (discarded)"; one 16 40 warmup 60 >/dev/null

declare -A Q; best=0
for C in 20 40 80 160; do
  q=$(one 16 $C tune); log "  ladder C=$C qps=$q"
  [ "$q" = NA ] && continue
  Q[$C]=$q
  if awk -v a=$q -v b=$best 'BEGIN{exit !(a < 1.03*b)}'; then break; fi
  awk -v a=$q -v b=$best 'BEGIN{exit !(a>b)}' && best=$q
done
[ "$best" != 0 ] || die "C ladder produced no result"
CH=""
for C in 20 40 80 160; do
  [ -n "${Q[$C]:-}" ] || continue
  awk -v a=${Q[$C]} -v m=$best 'BEGIN{exit !(a>=0.97*m)}' && { CH=$C; break; }
done
log "chosen C=$CH (best ladder qps $best)"
echo -e "$SCALE\t$N\t$CH\t$best" >> $X/results/chosen_c.tsv
for M in 16 32; do for r in 1 2 3; do
  q=$(one $M $CH r$r); log "  M=$M C=$CH r$r qps=$q"
done; done
log "=== 03 complete ($SCALE N=$N) ==="
