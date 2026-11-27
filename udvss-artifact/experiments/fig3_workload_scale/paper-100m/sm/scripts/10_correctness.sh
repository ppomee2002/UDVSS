#!/bin/bash
# Correctness smoke for --lsh-aligned-locators + SQ8 (copy of lsh-aligned-1m) (20k SIFT vectors), before
# any performance run:
#   asplit : load at 128, native split 256 -> 512 -> 1024
#   amerge : load at 1024 (= fresh 1024), native merge 512 -> 256 -> 128
#   f256, f512 : fresh loads at 256 / 512 (fresh 128 = asplit at 128)
# Per stage: object census (the bench reads every object through its
# bucket locator, so op_rval=0 means it is stored in the PG the client
# computed) and `ceph osd map` for every bucket locator (the monitor's own
# raw hash and PG). 11_check.py compares everything.
source /home/dev_path/experiments/paper-100m/sm/scripts/env.sh
DATA=/home/dev_path/ceph/sift1M/sift
SD="--base $DATA/sift_base.fvecs --query $DATA/sift_query.fvecs --groundtruth $DATA/sift_groundtruth.ivecs --base-limit 20000 $QUANT"
# IB = SQ8 + lsh-aligned index args from env.sh
R=$EXP/results/correctness; mkdir -p $R
osd_watchdog & WATCHDOG=$!
trap "kill $WATCHDOG 2>/dev/null" EXIT

mk() {
  ceph_ osd pool create $1 $2 $2 >/dev/null
  ceph_ osd pool set $1 pg_autoscale_mode off >/dev/null
  ceph_ osd pool application enable $1 rados >/dev/null
  wait_pool $1 $2 900 || { log "FATAL: pool $1 not clean"; exit 1; }
}
load_() {
  bench --operation load --pool $1 --index csm_index $IB --pg-num $2 $SD \
    --query-limit 100 --load-concurrency 16 --top-k 10 > $EXP/logs/c_load_$1.log 2>&1 \
    || { log "FATAL: load $1"; tail -3 $EXP/logs/c_load_$1.log; exit 1; }
  log "load $1 at $2: $(grep ^LOAD $EXP/logs/c_load_$1.log | cut -c1-120)"
}
stage() {  # $1 pool $2 pg_num $3 tag
  [ "$(pool_get $1 pg_num)" = $2 ] || { log "FATAL: $1 pg_num $(pool_get $1 pg_num) != $2"; exit 1; }
  $OB/rados -c $CONF -p $1 ls 2>/dev/null | sort > $R/$3.objs
  bench --operation object-census --pool $1 --index csm_index $IB --pg-num $2 $SD \
    --object-list $R/$3.objs --top-k 10 > $R/$3.census.csv 2> $R/$3.census.err \
    || { log "FATAL: census $3"; tail -3 $R/$3.census.err; exit 1; }
  # Monitor-side mapping of every bucket locator the census used.
  python3 - $R/$3.census.csv > $R/$3.keys <<'PY'
import csv,sys
L=open(sys.argv[1]).read().splitlines()
L=L[next(i for i,l in enumerate(L) if l.startswith('oid,')):]
print('\n'.join(sorted({r['locator'] for r in csv.DictReader(L) if r.get('locator')})))
PY
  if [ -s $R/$3.keys ]; then
    while read k; do
      echo "$k $(ceph_ osd map $1 "$k" | sed -E 's/.*-> pg [0-9]+\.([0-9a-f]+) \([0-9]+\.([0-9a-f]+)\).*/\1 \2/')"
    done < $R/$3.keys > $R/$3.osdmap
  fi
  log "$3 pg_num=$2: $(grep CENSUS_TOTAL $R/$3.census.err)"
}
change() {  # $1 pool $2 new pg_num $3 timeout
  echo "[$(date -u +%FT%TZ)] ceph osd pool set $1 pg_num/pgp_num $2" >> $CMDLOG
  ceph_ osd pool set $1 pg_num $2 >/dev/null
  ceph_ osd pool set $1 pgp_num $2 >/dev/null
  local t0=$(date +%s)
  wait_pool $1 $2 $3 || { log "FATAL: $1 did not reach clean pg_num $2"; exit 1; }
  log "$1 -> pg_num $2 in $(( $(date +%s) - t0 ))s"
}

mk asplit 128; load_ asplit 128; stage asplit 128 split128
for n in 256 512 1024; do change asplit $n 1800; stage asplit $n split$n; done
for n in 256 512; do mk f$n $n; load_ f$n $n; stage f$n $n fresh$n; done
mk amerge 1024; load_ amerge 1024; stage amerge 1024 merge1024
for n in 512 256 128; do change amerge $n 7200; stage amerge $n merge$n; done
log "=== 10 complete; run 11_check.py ==="
