#!/bin/bash
# run.sh <arm> "<M list>" : the formal 100M Ceph arm (user 2026-10-11 night).
#  create (224G device, PG from routesim100m) + load (writes the pivot model file)
#  -> activate (full-PG count already done by create; warm-up kept)
#  -> C ladder 20/40/80 at M=16 (smallest C within 3% of max) -> M list x3, >=120 s each
#  -> stop the OSD, keep the cluster. status.sh keeps STATUS.md / metrics.tsv current.
ARM=${1:?}; MLIST=${2:?}
R=/home/dev_path/experiments/scale-deep; D=$R/d100m; S=$R/ceph/scripts
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256 LOAD_CONC=128
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $D/driver.log >> $R/chain.log; }
stop_osds() { for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
  sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done; }
nohup setsid bash $D/status.sh $ARM >/dev/null 2>&1 &
log "d100m $ARM: create + load (device $SEASTORE_DEVICE_SIZE, LOAD_CONC $LOAD_CONC)"
if ! grep -q "LOADED_OK $ARM" $R/ceph/logs/${ARM}_create.log 2>/dev/null; then
  bash $S/create_and_load.sh $ARM > $R/ceph/logs/${ARM}_create.log 2>&1 \
    || { log "FAILED: create/load ($(grep -a FATAL $R/ceph/logs/${ARM}_create.log | tail -1))"; log "=== D100M DONE (failed) ==="; exit 1; }
fi
log "load done: $(grep -aE '^  LOAD |records=' $R/ceph/logs/${ARM}_create.log | tr -s ' ' | cut -c1-200 | tr '\n' ' ')"
log "activate (skip count: create_and_load just did it)"
bash $S/activate_workload.sh $ARM --skip-count > $R/ceph/logs/${ARM}_activate.log 2>&1 \
  || { log "FAILED: activate"; stop_osds; log "=== D100M DONE (failed) ==="; exit 1; }
declare -A cq
for C in 20 40 80; do
  out=$(bash $S/run_one.sh $ARM 16 1024 $C tune 2>&1 | tail -2 | head -1)
  q=$(echo "$out" | grep -oE " qps=[0-9.]+" | cut -d= -f2); [ -n "$q" ] && cq[$C]=$q
  log "tune M16 C$C: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|cand=[0-9.]+' | tr '\n' ' ')"
done
max=0; for C in "${!cq[@]}"; do awk -v a=${cq[$C]} -v b=$max 'BEGIN{exit !(a>b)}' && max=${cq[$C]}; done
BC=""; for C in 20 40 80; do [ -n "${cq[$C]:-}" ] && awk -v a=${cq[$C]} -v m=$max 'BEGIN{exit !(a>=0.97*m)}' && { BC=$C; break; }; done
[ -n "$BC" ] || { log "FAILED: C ladder"; stop_osds; log "=== D100M DONE (failed) ==="; exit 1; }
echo -e "$ARM\t16\t$BC\t${cq[$BC]}" >> $R/ceph/results/chosen_c.tsv; log "chosen C=$BC"
for M in $MLIST; do for r in 1 2 3; do
  out=$(bash $S/run_one.sh $ARM $M 1024 $BC r$r 2>&1 | tail -2 | head -1)
  echo "  [$ARM] $out" >> $R/ceph/logs/run_all.log
  log "M$M r$r: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|cand=[0-9.]+|failures=[0-9]+' | tr '\n' ' ')"
done; done
stop_osds; log "OSD stopped (cluster kept)"; log "=== D100M DONE ==="
