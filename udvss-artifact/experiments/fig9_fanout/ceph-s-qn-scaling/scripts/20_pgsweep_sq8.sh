#!/bin/bash
# Usage: ROUTE=lsh|pivot 20_pgsweep_sq8.sh
# Pick Ceph's pg_num: OSD=1, pg_num in PGS, M doubling from 1 until recall@10
# reaches RECALL_TARGET (latency only grows with M, so that first M is the
# cheapest point meeting the target for that pg_num). Short runs: 100 warmup
# + 500 measured, ID mode. The winner is the lowest-latency (pg_num, M).
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
S=$EXP/scripts
PGS=${PGS:-"16 64 256 1024"}
row() { grep "^PGS_${ROUTE}_ID_PG$1_M$2_OSD1_r0," $RES/runs.csv 2>/dev/null; }
recall_of() { python3 -c "import csv
for r in csv.DictReader(open('$RES/runs.csv')):
    if r['label']=='PGS_${ROUTE}_ID_PG$1_M$2_OSD1_r0': print(r['recall'])"; }

log "=== PG sweep $ROUTE (target recall $RECALL_TARGET) ==="
# 01_osds.sh drops every pool, so an existing pool always holds a complete load
[ "$(ceph_ osd ls 2>/dev/null | wc -l)" = 1 ] || BUNDLE=$BUNDLE $S/01_osds.sh 1
for pg in $PGS; do
  ceph_ osd pool ls 2>/dev/null | grep -qx $(sq_pool $pg) || $S/12_load_sq8.sh $pg || { log "FAILED load $ROUTE pg$pg"; continue; }
  # doubling plus 1.5x steps: LSH with one query table cannot reach every PG
  # (pg_num 256: M >= 255 is EINVAL), so the largest steps may fail
  for m in 1 2 4 8 16 32 48 64 96 128 192 256 384 512 768 1024; do
    [ $m -le $pg ] || break
    [ -n "$(row $pg $m)" ] || $S/13_run_sq8.sh PGS $pg $m ID 0 500 100 > /dev/null \
      || { log "FAILED $ROUTE pg$pg m$m"; break; }
    rc=$(recall_of $pg $m)
    log "$ROUTE pg$pg m$m recall=$rc"
    python3 -c "import sys; sys.exit(0 if float('$rc') >= $RECALL_TARGET else 1)" && break
  done
done
python3 - <<PY | tee $RES/best_$ROUTE.txt
import pandas as pd
d = pd.read_csv("$RES/runs.csv")
d = d[(d.family == "PGS") & (d.route == "$ROUTE") & (d.failures == 0)]
ok = d[d.recall >= $RECALL_TARGET]
first = ok.sort_values("m").groupby("pg").head(1)          # cheapest M per pg_num
print(first[["pg", "m", "recall", "mean_us", "candidates"]].to_string(index=False))
if len(first):
    b = first.sort_values("mean_us").iloc[0]
    print(f"BEST pg={int(b.pg)} m={int(b.m)} recall={b.recall:.4f} mean_us={b.mean_us:.1f}")
else:
    b = d.sort_values("recall", ascending=False).iloc[0]
    print(f"BEST pg={int(b.pg)} m={int(b.m)} recall={b.recall:.4f} mean_us={b.mean_us:.1f} TARGET_NOT_REACHED")
PY
