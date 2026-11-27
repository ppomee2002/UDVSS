#!/bin/bash
# run_arm.sh <arm> "<M list>" <M_tune>
#   create + load once -> activate (other OSDs stopped, pinned, count, warm-up)
#   -> C ladder 20/40/80 at M_tune (repeat 1) -> 3 repeats per M at the chosen C,
#   every case time-bound: >= MIN_SECONDS (120) of queries after a warm-up round
#   -> teardown (the 64 GiB device is deleted; rows and logs stay).
# Same procedure as workload-bench (B=1024, OSD 0-7, client 8-47).
source /home/dev_path/experiments/scale-deep/ceph/scripts/bench_env.sh
ARM=${1:?}; MLIST=${2:?}; MTUNE=${3:?}
S=$BENCH_ROOT/scripts
B=1024
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
ALLOW_MISSING_CLUSTER=1 load_workload "$ARM"
if ! grep -q "LOADED_OK $ARM" "$BENCH_ROOT/logs/${ARM}_create.log" 2>/dev/null; then
  # KEEP=1 leaves the previous arm's OSD up, and create_and_load refuses to run
  # beside another OSD: stop the other arms' OSDs first (data stays on disk).
  for w in $(wl_names); do
    [ "$w" = "$ARM" ] && continue
    v=$(wl_field "$w" cluster); [ -d "$v" ] && stop_osd "$v"
  done
  log "create+load $ARM"
  bash $S/create_and_load.sh "$ARM" > "$BENCH_ROOT/logs/${ARM}_create.log" 2>&1 \
    || { tail -5 "$BENCH_ROOT/logs/${ARM}_create.log"; die "create/load failed"; }
  grep -aE "PIVOT_MODEL|PIVOT_PLACEMENT|LOAD |records=" "$BENCH_ROOT/logs/${ARM}_create.log" | sed 's/^/  /'
fi
log "activate $ARM"
bash $S/activate_workload.sh "$ARM" > "$BENCH_ROOT/logs/${ARM}_activate.log" 2>&1 \
  || { tail -5 "$BENCH_ROOT/logs/${ARM}_activate.log"; die "activate failed"; }
tail -1 "$BENCH_ROOT/logs/${ARM}_activate.log"

# v2 (from gi_bal on): the smallest C within 3% of the best QPS. On a flat
# ladder arg-max picked C=80 for no gain and 4x the queries per case.
declare -A cq
for C in 20 40 80; do
  out=$(bash $S/run_one.sh "$ARM" "$MTUNE" $B $C tune 2>&1 | tail -2 | head -1); echo "  $out"
  q=$(echo "$out" | grep -oE " qps=[0-9.]+" | cut -d= -f2)
  [ -n "$q" ] && cq[$C]=$q
done
max_q=0; for C in "${!cq[@]}"; do awk -v a="${cq[$C]}" -v b="$max_q" 'BEGIN{exit !(a>b)}' && max_q=${cq[$C]}; done
best_c=""; best_q=0
for C in 20 40 80; do
  [ -n "${cq[$C]:-}" ] || continue
  awk -v a="${cq[$C]}" -v m="$max_q" 'BEGIN{exit !(a>=0.97*m)}' && { best_c=$C; best_q=${cq[$C]}; break; }
done
[ -n "$best_c" ] || die "C ladder produced no result"
log "$ARM chosen C=$best_c (qps $best_q at M=$MTUNE)"
echo -e "$ARM\t$MTUNE\t$best_c\t$best_q" >> "$BENCH_ROOT/results/chosen_c.tsv"

for M in $MLIST; do for R in 1 2 3; do
  out=$(bash $S/run_one.sh "$ARM" "$M" $B $best_c r$R 2>&1 | tail -2 | head -1); echo "  $out"
done; done
if [ "${KEEP:-0}" = 1 ]; then
  log "keeping $ARM (KEEP=1); the next arm's activate stops its OSD"
else
  log "teardown $ARM"
  bash $S/teardown.sh "$ARM"
fi
log "DONE $ARM"
