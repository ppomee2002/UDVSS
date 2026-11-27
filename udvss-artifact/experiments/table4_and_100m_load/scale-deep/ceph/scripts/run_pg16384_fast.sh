#!/bin/bash
# PG16384: every bench process rebuilds the pivot model (1M samples x 16384 reps, ~31 min,
# no model cache). run_arm.sh would rebuild it 8x (count x2, warm-up, C ladder x3, reps x3).
# So: wait for create_and_load (its full-PG count already verified the load), stop the
# run_arm driver, then activate WITHOUT the repeat count/warm-up (run_one has its own
# warm-up round) and measure M=16 at C=20 (the C chosen for both PG1024 and PG4096) x3.
R=/home/dev_path/experiments/scale-deep; S=$R/ceph/scripts; L=$R/chain.log
CL=$R/ceph/logs/d10m_pg16384_create.log; RL=$R/ceph/logs/run_all.log
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
until grep -qaE "LOADED_OK d10m_pg16384|FATAL" $CL; do sleep 10; done
grep -qa "LOADED_OK d10m_pg16384" $CL || { log "FAILED: pg16384 create/count"; exit 1; }
desc() { for c in $(ps -o pid= --ppid $1); do echo $c; desc $c; done; }
D=$(ps -eo pid,args | awk '$2=="bash" && $3=="scripts/run_pg16384.sh"{print $1}')
if [ -n "$D" ]; then
  T="$D $(desc $D)"
  for p in $T; do kill -STOP $p 2>/dev/null; done
  for p in $T; do ps -o args= -p $p | grep -q crimson-osd || kill -9 $p 2>/dev/null; done
fi
log "pg16384: load verified; run_arm stopped; fast path (activate no-count/no-warmup, M16 C20 x3)"
bash $S/activate_workload.sh d10m_pg16384 --skip-count --skip-warmup > $R/ceph/logs/d10m_pg16384_activate.log 2>&1 \
  || { log "FAILED: pg16384 activate"; exit 1; }
echo -e "d10m_pg16384\t16\t20\tNA(fixed)" >> $R/ceph/results/chosen_c.tsv
for r in 1 2 3; do
  out=$(bash $S/run_one.sh d10m_pg16384 16 1024 20 r$r 2>&1 | tail -2 | head -1)
  echo "  [pg16384] $out" >> $RL; log "pg16384 r$r: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall[^ ]*=[0-9.]+' | tr '\n' ' ')"
done
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
log "=== PG16384 FAST DONE (OSD stopped) ==="
