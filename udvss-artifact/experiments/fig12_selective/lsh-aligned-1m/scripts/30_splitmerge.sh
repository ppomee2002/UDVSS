#!/bin/bash
# split/merge figure re-measure with --lsh-aligned-locators. Stage, census,
# sweep and change are copied from pg-objloc-1m 80_bucket_run.sh (tree
# sweep, the figure's split + merge series) and 85_merge_fullscan.sh
# (--full-pg-scan sweep, the figure's "M=16 Fullscan" series); env.sh is a
# copy of pg-objloc-1m's (same QARGS, TREE, DSET, MLIST, QLIMIT). The only
# change is the locator flag. Left out because the figure does not use
# them: the batch-1024 QPS runs and the bdirect baseline. The fullscan
# sweep runs on the same merge pool right after the tree sweep, instead of
# on a second identically loaded pool (bmerge4): it only depends on which
# vectors each PG holds.
source /home/dev_path/experiments/lsh-aligned-1m/scripts/env.sh
IB=$IB_NEW
R=$EXP/results/splitmerge; mkdir -p $R
RAW=$R/raw.txt; RAWF=$R/raw_fullscan.txt
osd_watchdog & WATCHDOG=$!
trap "kill $WATCHDOG 2>/dev/null" EXIT

mk() {
  ceph_ osd pool create $1 $2 $2 >/dev/null
  ceph_ osd pool set $1 pg_autoscale_mode off >/dev/null
  ceph_ osd pool application enable $1 rados >/dev/null
  wait_pool $1 $2 1800 || { log "FATAL: pool $1 not clean"; exit 1; }
  pg_state $1 "created pg_num=$2"
}
load_() {
  bench --operation load --pool $1 --index $1_index $IB --pg-num $2 $DSET \
    --query-limit 1000 --load-concurrency 32 --top-k 10 > $EXP/logs/load_$1.log 2>&1 \
    || { log "FATAL: load $1"; tail -3 $EXP/logs/load_$1.log; exit 1; }
  log "$1: $(grep ^LOAD $EXP/logs/load_$1.log | cut -c1-150)"
}
census_() {  # $1 pool $2 pg_num $3 tag
  $OB/rados -c $CONF -p $1 ls 2>/dev/null | sort > $R/$3.objs
  bench --operation object-census --pool $1 --index $1_index $IB --pg-num $2 $DSET \
    --object-list $R/$3.objs --top-k 10 > $R/$3.census.csv 2> $R/$3.census.err
  grep CENSUS_TOTAL $R/$3.census.err
}
sweep() {  # $1 pool $2 pg_num $3 tag   (80_bucket_run.sh)
  for m in $MLIST; do
    local out line
    out=$(bench --operation fanout-query --pool $1 --index $1_index $IB --pg-num $2 \
      $QARGS $DSET --m $m --query-limit $QLIMIT $TREE --query-concurrency 1 \
      --warmup-rounds 0 --rounds 1 --io-depth 0 \
      --query-results-csv $R/$3_m$m.rows.csv 2>&1)
    line=$(echo "$out" | grep "^FANOUT_QUERY")
    [ -z "$line" ] && { log "FATAL: query $3 m=$m: $(echo "$out" | grep -i error | head -1)"; exit 1; }
    echo "$3 pool=$1 pg=$2 m=$m $line" >> $RAW
    echo "  $3 m=$m $(echo "$line" | grep -oE "(failures|recall_at_top_k|avg_total_matching_records_per_query|osd_requests_per_query)=[^ ]+" | tr '\n' ' ')"
  done
}
fsweep() {  # $1 pool $2 pg_num $3 tag   (85_merge_fullscan.sh)
  for m in $MLIST; do
    local out line
    out=$(bench --operation fanout-query --pool $1 --index $1_index $IB --pg-num $2 \
      $QARGS $DSET --m $m --query-limit $QLIMIT --full-pg-scan --object-list $R/$3.objs \
      --query-concurrency 1 --warmup-rounds 0 --rounds 1 --io-depth 0 \
      --query-results-csv $R/f$3_m$m.rows.csv 2>&1)
    line=$(echo "$out" | grep "^FANOUT_QUERY")
    [ -z "$line" ] && { log "FATAL: fullscan $3 m=$m: $(echo "$out" | grep -i error | head -1)"; exit 1; }
    echo "f$3 pool=$1 pg=$2 m=$m $line" >> $RAWF
    echo "  f$3 m=$m $(echo "$line" | grep -oE "(failures|missing_objects|recall_at_top_k|avg_total_matching_records_per_query)=[^ ]+" | tr '\n' ' ')"
  done
}
stage() {  # $1 pool $2 pg_num $3 tag [fullscan]
  pg_state $1 "before queries at pg_num=$2"
  integrity $1 "pg_num=$2"
  log "$3 pg_num=$(pool_get $1 pg_num) pgp_num=$(pool_get $1 pgp_num) census: $(census_ $1 $2 $3)"
  sweep $1 $2 $3
  [ "${4:-}" = fullscan ] && fsweep $1 $2 $3
  return 0
}
change() {  # $1 pool $2 new pg_num $3 timeout
  echo "[$(date -u +%FT%TZ)] ceph osd pool set $1 pg_num $2 ; pgp_num $2" >> $CMDLOG
  ceph_ osd pool set $1 pg_num $2 >/dev/null
  ceph_ osd pool set $1 pgp_num $2 >/dev/null
  local t0=$(date +%s)
  wait_pool $1 $2 $3 || { pg_state $1 "STUCK $2"; log "FATAL: $1 -> $2"; exit 1; }
  log "$1 -> $2 took $(( $(date +%s) - t0 ))s"
}

mk asplit1m 128; mk amerge1m 1024
load_ asplit1m 128; load_ amerge1m 1024

stage asplit1m 128 asplit_pg128
for N in 256 512 1024; do change asplit1m $N 7200; stage asplit1m $N asplit_pg$N; done

stage amerge1m 1024 amerge_pg1024 fullscan
for N in 512 256 128; do change amerge1m $N 14400; stage amerge1m $N amerge_pg$N fullscan; done
log "=== lsh-aligned split/merge 1M run complete ==="
