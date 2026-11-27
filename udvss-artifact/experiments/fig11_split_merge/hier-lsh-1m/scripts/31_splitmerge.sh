#!/bin/bash
# split_merge_lsh_aligned re-run with hierarchical LSH (lsh-aligned-1m
# scripts/30_splitmerge.sh): split pool 128->256->512->1024, merge pool
# 1024->512->256->128, tree sweep M=16/32/64 at every stage, M=16 full-PG
# scan on the merge path. Changes: --placement-mode hierarchical_lsh, and
# 1000 queries at concurrency 64 instead of 150 at concurrency 1
# (recall/candidates do not depend on concurrency).
# Usage: 31_splitmerge.sh [tag-prefix] [dataset args...]  (defaults: 1m, SIFT1M)
source $HOME/hier/scripts/env.sh
P=${1:-1m}; shift || true
[ $# -gt 0 ] && DSET="$*"
IB="${IDXARGS/--pg-num 1024 /}"
TREE="--tree-search-budget 64 --tree-no-improve-patience 4 --tree-onode-budget 64"
MLIST=${MLIST:-16 32 64}; FMLIST=${FMLIST:-16}; QL=${QL:-1000}; FQL=${FQL:-$QL}; LC=${LC:-64}
# SEQ=1: split pool first, drop it, then the merge pool (one 100M pool at a time).
SEQ=${SEQ:-0}
R=$H/results/splitmerge_$P; mkdir -p $R
RAW=$R/raw.txt; RAWF=$R/raw_fullscan.txt
pool_get() { ceph_ osd pool get $1 $2 | awk '{print $2}'; }
all_clean() {
  local st tot cln; st=$(ceph_ pg stat)
  tot=$(echo "$st" | grep -oE "^[0-9]+")
  cln=$(echo "$st" | grep -oE "[0-9]+ active\+clean[;,]" | head -1 | grep -oE "^[0-9]+")
  [ -n "$tot" ] && [ "$tot" = "$cln" ]
}
wait_pool() {
  local deadline=$(( $(date +%s) + $3 ))
  while [ $(date +%s) -lt $deadline ]; do
    [ "$(pool_get $1 pg_num)" = $2 ] && [ "$(pool_get $1 pgp_num)" = $2 ] && all_clean && return 0
    [ -z "$(ps -eo args | grep '[h]ier/bundle/bin/crimson-osd')" ] && { log "CRITICAL: OSD is gone"; return 2; }
    sleep 10
  done
  return 1
}
mk() {
  ceph_ osd pool create $1 $2 $2 >/dev/null
  ceph_ osd pool set $1 pg_autoscale_mode off >/dev/null
  ceph_ osd pool application enable $1 rados >/dev/null
  wait_pool $1 $2 1800 || { log "FATAL: pool $1 not clean"; exit 1; }
}
load_() {
  bench --operation load --pool $1 --index $1_index $IB --pg-num $2 $DSET \
    --query-limit 1000 --load-concurrency $LC --top-k 10 > $H/logs/load_$1.log 2>&1 \
    || { log "FATAL: load $1"; tail -3 $H/logs/load_$1.log; exit 1; }
  log "$1: $(grep ^LOAD $H/logs/load_$1.log | cut -c1-170)"
}
objs_() { $OB/rados -c $CONF -p $1 ls 2>/dev/null | sort > $R/$2.objs; log "$2 objects=$(wc -l < $R/$2.objs)"; }
sweep() {  # pool pg tag
  for m in $MLIST; do
    local line
    line=$(bench --operation fanout-query --pool $1 --index $1_index $IB --pg-num $2 \
      $SQARGS $DSET --m $m --query-limit $QL $TREE --query-concurrency 64 \
      --warmup-rounds 0 --rounds 1 2>&1 | grep "^FANOUT_QUERY")
    [ -z "$line" ] && { log "FATAL: query $3 m=$m"; exit 1; }
    echo "$3 pool=$1 pg=$2 m=$m $line" >> $RAW
    log "  $3 m=$m $(echo "$line" | grep -oE "(failures|missing_objects|recall_at_top_k|avg_total_matching_records_per_query)=[^ ]+" | tr '\n' ' ')"
  done
}
fsweep() {
  for m in $FMLIST; do
    local line
    line=$(bench --operation fanout-query --pool $1 --index $1_index $IB --pg-num $2 \
      $SQARGS $DSET --m $m --query-limit $FQL --full-pg-scan --object-list $R/$3.objs \
      --query-concurrency 256 --warmup-rounds 0 --rounds 1 2>&1 | grep "^FANOUT_QUERY")
    [ -z "$line" ] && { log "FATAL: fullscan $3 m=$m"; exit 1; }
    echo "f$3 pool=$1 pg=$2 m=$m $line" >> $RAWF
    log "  f$3 m=$m $(echo "$line" | grep -oE "(failures|missing_objects|recall_at_top_k|avg_total_matching_records_per_query)=[^ ]+" | tr '\n' ' ')"
  done
}
stage() {  # pool pg tag [fullscan]
  objs_ $1 $3
  sweep $1 $2 $3
  [ "${4:-}" = fullscan ] && fsweep $1 $2 $3
  return 0
}
change() {
  echo "[$(date -u +%FT%TZ)] ceph osd pool set $1 pg_num $2 ; pgp_num $2" >> $CMDLOG
  ceph_ osd pool set $1 pg_num $2 >/dev/null
  ceph_ osd pool set $1 pgp_num $2 >/dev/null
  local t0=$(date +%s)
  wait_pool $1 $2 $3 || { log "FATAL: $1 -> $2"; exit 1; }
  log "$1 -> $2 took $(( $(date +%s) - t0 ))s"
}
S=hsplit$P; Mg=hmerge$P
if [ $SEQ = 0 ]; then
  mk $S 128; mk $Mg 1024
  load_ $S 128; load_ $Mg 1024
else
  mk $S 128; load_ $S 128
fi
stage $S 128 split_pg128
for N in 256 512 1024; do change $S $N 14400; stage $S $N split_pg$N; done
if [ $SEQ = 1 ]; then
  ceph_ config set mon mon_allow_pool_delete true
  ceph_ osd pool rm $S $S --yes-i-really-really-mean-it
  log "dropped $S"
  mk $Mg 1024; load_ $Mg 1024
fi
stage $Mg 1024 merge_pg1024 fullscan
for N in 512 256 128; do change $Mg $N 28800; stage $Mg $N merge_pg$N fullscan; done
log "=== hierarchical split/merge $P complete ==="
