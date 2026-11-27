#!/bin/bash
# Usage: 30_splitmerge.sh 1m|100m [split|merge|both]
# Split/merge with a full PG scan at every stage. Stage, census, change and
# the PG order are lsh-aligned-1m 30_splitmerge.sh; the sweep is its fsweep
# (85_merge_fullscan.sh), now run on both arms. Arms run one after the other:
#   split : fresh pool at 128, load, stage; 256 -> 512 -> 1024 (stage each); pool deleted
#   merge : fresh pool at 1024, load, stage; 512 -> 256 -> 128 (stage each); pool deleted
source /home/dev_path/experiments/paper-100m/sm/scripts/env.sh
S=$1; ARMS=${2:-both}
DSET=$(sm_dset $S)
R=$EXP/results/$S; mkdir -p $R $EXP/logs
RAW=$R/raw_fullscan.txt
TMO=$([ $S = 1m ] && echo 14400 || echo 172800)   # change timeout (100m: unknown, 48 h cap)
osd_watchdog & WATCHDOG=$!
trap "kill $WATCHDOG 2>/dev/null" EXIT

mk() {
  ceph_ osd pool create $1 $2 $2 >/dev/null
  ceph_ osd pool set $1 pg_autoscale_mode off >/dev/null
  ceph_ osd pool application enable $1 rados >/dev/null
  wait_pool $1 $2 1800 || { log "FATAL: pool $1 not clean"; exit 1; }
  pg_state $1 "created pg_num=$2"
}
rm_pool() {
  ceph_ config set mon mon_allow_pool_delete true
  ceph_ osd pool rm $1 $1 --yes-i-really-really-mean-it >/dev/null
  log "pool $1 deleted"
}
load_() {
  local t0=$(date +%s)
  bench --operation load --pool $1 --index $1_index $IB --pg-num $2 $QUANT $DSET \
    --query-limit 1000 --load-concurrency ${LOAD_CONC:-32} --top-k 10 > $EXP/logs/load_$1.log 2>&1 \
    || { log "FATAL: load $1"; tail -3 $EXP/logs/load_$1.log; exit 1; }
  grep -qE "^LOAD.* failures=0( |$)" $EXP/logs/load_$1.log || { log "FATAL: load failures $1"; exit 1; }
  log "$1: load $(( $(date +%s) - t0 ))s $(grep ^LOAD $EXP/logs/load_$1.log | cut -c1-150)"
}
census_() {  # $1 pool $2 pg_num $3 tag
  $OB/rados -c $CONF -p $1 ls 2>/dev/null | sort > $R/$3.objs
  bench --operation object-census --pool $1 --index $1_index $IB --pg-num $2 $QUANT $DSET \
    --object-list $R/$3.objs --query-limit $QLIMIT --top-k 10 > $R/$3.census.csv 2> $R/$3.census.err
  grep CENSUS_TOTAL $R/$3.census.err
}
fsweep() {  # $1 pool $2 pg_num $3 tag
  for m in $MLIST; do
    local out line
    out=$(bench --operation fanout-query --pool $1 --index $1_index $IB --pg-num $2 $QUANT \
      $QARGS $DSET --m $m --query-limit $QLIMIT --tree-search-budget 0 \
      --full-pg-scan --object-list $R/$3.objs \
      --query-concurrency 1 --warmup-rounds 0 --rounds 1 --io-depth 0 \
      --query-results-csv $R/f$3_m$m.rows.csv 2>&1)
    line=$(echo "$out" | grep "^FANOUT_QUERY")
    [ -z "$line" ] && { log "FATAL: fullscan $3 m=$m: $(echo "$out" | grep -i error | head -1)"; exit 1; }
    echo "f$3 pool=$1 pg=$2 m=$m $line" >> $RAW
    echo "  f$3 m=$m $(echo "$line" | grep -oE "(failures|missing_objects|duplicate_entries|recall_at_top_k|avg_total_matching_records_per_query|logical_latency_us_avg)=[^ ]+" | tr '\n' ' ')"
    echo "$line" | grep -qE "failures=0 .*missing_objects=0" || { log "FATAL: failures/missing at $3 m=$m"; exit 1; }
  done
}
stage() {  # $1 pool $2 pg_num $3 tag
  pg_state $1 "before queries at pg_num=$2"
  integrity $1 "pg_num=$2"
  local c; c=$(census_ $1 $2 $3) || { log "FATAL: census $3: $(tail -1 $R/$3.census.err)"; exit 1; }
  echo "$c" | grep -q "missing=0 failed=0" || { log "FATAL: census $3 not clean: $c"; exit 1; }
  log "$3 pg_num=$(pool_get $1 pg_num) pgp_num=$(pool_get $1 pgp_num) census: $c"
  fsweep $1 $2 $3
}
change() {  # $1 pool $2 new pg_num $3 timeout
  echo "[$(date -u +%FT%TZ)] ceph osd pool set $1 pg_num $2 ; pgp_num $2" >> $CMDLOG
  ceph_ osd pool set $1 pg_num $2 >/dev/null
  ceph_ osd pool set $1 pgp_num $2 >/dev/null
  local t0=$(date +%s)
  wait_pool $1 $2 $3 || { pg_state $1 "STUCK $2"; log "FATAL: $1 -> $2"; exit 1; }
  log "$1 -> $2 took $(( $(date +%s) - t0 ))s"
}

if [ $ARMS != merge ]; then
  P=fsplit$S; mk $P 128; load_ $P 128
  stage $P 128 split_pg128
  for N in 256 512 1024; do change $P $N $TMO; stage $P $N split_pg$N; done
  rm_pool $P
fi
if [ $ARMS != split ]; then
  P=fmerge$S; mk $P 1024; load_ $P 1024
  stage $P 1024 merge_pg1024
  for N in 512 256 128; do change $P $N $TMO; stage $P $N merge_pg$N; done
  rm_pool $P
fi
log "=== split/merge fullscan $S ($ARMS) complete ==="
