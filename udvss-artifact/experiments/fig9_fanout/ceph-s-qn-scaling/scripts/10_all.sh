#!/bin/bash
# Full sweep, resumable: a label already in results/runs.csv is skipped.
#   [A] CS: OSD=1, PG = 1/4/16, M = PG          -> (a)(b)
#   [B] CQ: PG=16, M = 16, OSD = 1/2/4/8/16     -> (c)(d)
# Order inside each point is rep-major (r1: ID,VEC; r2: ...) so slow drift
# lands evenly on both modes.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
S=$EXP/scripts
done_() { grep -q "^$1," $EXP/results/runs.csv 2>/dev/null; }
nosd() { ceph_ osd stat -f json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])'; }
pool_ok() { ceph_ osd pool ls 2>/dev/null | grep -qx sqn_pg$1 && grep -q "^LOAD completed=1000000 failures=0" $EXP/logs/02_load_pg$1_osd$2.log 2>/dev/null; }
point() {  # FAM PG N
  for r in $(seq 1 $REPS); do for m in ID VEC; do
    done_ "$1_${m}_PG$2_OSD$3_r$r" && continue
    log "run $1 $m PG=$2 OSD=$3 r$r"
    $S/03_run.sh $1 $2 $m $r > /dev/null || { log "FAILED $1 $m PG=$2 OSD=$3 r$r"; }
  done; done
}

alldone() {  # FAM PG N
  for r in $(seq 1 $REPS); do for m in ID VEC; do done_ "$1_${m}_PG$2_OSD$3_r$r" || return 1; done; done
}

log "=== [A] PG sweep, OSD=1 ==="
for pg in 1 4 16; do
  alldone CS $pg 1 && continue
  [ "$(nosd)" = 1 ] || $S/01_osds.sh 1
  pool_ok $pg 1 || $S/02_pool_load.sh $pg
  point CS $pg 1
done

log "=== [B] OSD sweep, PG=16 ==="
if ! alldone CQ 16 1; then   # same cluster + pool as [A]'s PG=16, separate runs
  [ "$(nosd)" = 1 ] || $S/01_osds.sh 1
  pool_ok 16 1 || $S/02_pool_load.sh 16
  point CQ 16 1
fi
for pg in 1 4; do ceph_ osd pool rm sqn_pg$pg sqn_pg$pg --yes-i-really-really-mean-it 2>/dev/null; done
for n in 2 4 8 16; do
  alldone CQ 16 $n && continue
  if [ "$(nosd)" != $n ] || ! pool_ok 16 $n; then
    $S/01_osds.sh $n && $S/02_pool_load.sh 16 || { log "FAILED setup OSD=$n"; continue; }
  fi
  point CQ 16 $n
done
log "=== all done ==="
