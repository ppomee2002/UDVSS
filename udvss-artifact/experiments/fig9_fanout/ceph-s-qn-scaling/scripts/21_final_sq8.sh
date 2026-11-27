#!/bin/bash
# Usage: ROUTE=lsh|pivot 21_final_sq8.sh
# Full measurements at the pg_num/M chosen by 20_pgsweep_sq8.sh
# (results_sq8/best_<route>.txt), resumable by label:
#   CS  OSD=1                       -> reference line in (a)(b)
#   CQ  OSD=1/2/4/8/16              -> (c)(d); OSD 1/4/16 also feed (e)(f)
# 3 reps x {ID, VEC}, 500 warmup + 3000 measured, C=1, NQ=1.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
S=$EXP/scripts
read PG M < <(grep -oP '^BEST pg=\K[0-9]+ m=[0-9]+' $RES/best_$ROUTE.txt | sed 's/ m=/ /')
[ -n "$PG" ] || { log "FATAL: no best_$ROUTE.txt"; exit 1; }
done_() { grep -q "^$1," $RES/runs.csv 2>/dev/null; }
alldone() {  # FAM N
  for r in $(seq 1 $REPS); do for md in ID VEC; do
    done_ "$1_${ROUTE}_${md}_PG${PG}_M${M}_OSD$2_r$r" || return 1; done; done
}
point() {  # FAM N
  for r in $(seq 1 $REPS); do for md in ID VEC; do
    done_ "$1_${ROUTE}_${md}_PG${PG}_M${M}_OSD$2_r$r" && continue
    log "run $1 $ROUTE $md PG=$PG M=$M OSD=$2 r$r"
    $S/13_run_sq8.sh $1 $PG $M $md $r > /dev/null || log "FAILED $1 $ROUTE $md OSD=$2 r$r"
  done; done
}
fresh_cluster() {  # N
  r109 "pkill -9 -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' || true"
  kill $(exp_pids) 2>/dev/null; sleep 3
  $S/00_mon.sh > /dev/null && $S/netctr.sh setup && BUNDLE=$BUNDLE $S/01_osds.sh $1
}
nosd() { ceph_ osd stat -f json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])'; }
log "=== final $ROUTE pg=$PG m=$M ==="
for n in 1 2 4 8 16; do
  if alldone CQ $n && { [ $n != 1 ] || alldone CS 1; }; then continue; fi
  # fresh mon + OSDs + reload for every N, so each point starts from the same
  # state. A fresh mon also matters: after the PG sweep's ~1400 pg-upmap
  # epochs a newly created crimson OSD aborts with "MOSDMap lied about what
  # maps it had" while catching up on the trimmed map history.
  fresh_cluster $n && $S/12_load_sq8.sh $PG || { log "FAILED setup OSD=$n"; continue; }
  [ $n = 1 ] && point CS 1
  point CQ $n
done
log "=== final $ROUTE done ==="
