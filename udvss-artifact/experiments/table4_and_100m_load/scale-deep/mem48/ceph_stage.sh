#!/bin/bash
# Ceph 100M under a 48 GiB cgroup on the whole crimson-osd (mon/mgr and the bench client outside).
#  default: original d100m_pg16384 setup (same VectorNode/pivot/PG16384), crimson_memory unset
#           (seastar sizes itself from the cgroup's memory.max minus its reserve).
#  con:     only if default cannot load/run -- same setup + crimson_memory=40G (< 48G cap).
# Each arm: create + load 100M -> activate -> C ladder 20/40/80 at M16 -> M 16 32 64 128 8 96 x3.
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; S=$R/ceph/scripts; L=$D/chain.log
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256 LOAD_CONC=128
CG=/sys/fs/cgroup/osd48; export OSD_CG=$CG
log() { echo "[$(date -u +%FT%TZ)] ceph: $*" | tee -a $L; }
stop_osds() { for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
  sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done; }
oomk() { awk '$1=="oom_kill"{print $2}' $CG/memory.events; }
osd_alive() { pgrep -x crimson-osd >/dev/null; }
mkcg() { [ -d $CG ] && { cat $CG/cgroup.procs | grep -q . && die_now "cgroup busy"; rmdir $CG; }
  mkdir $CG && echo 51539607552 > $CG/memory.max && echo 0 > $CG/memory.swap.max; }
die_now() { log "FATAL $*"; exit 1; }
sampler() {   # local cgroup + OSD RSS + bench client RSS every 30 s
  local O=$D/ceph_$1_mem.tsv; echo -e "utc\tcur_gb\tpeak_gb\tanon_gb\tfile_gb\toom_kill\tosd_rss_gb\tclient_rss_gb" > $O
  while [ ! -e $D/ceph_$1.stop ]; do
    a=$(awk '$1=="anon"{a=$2} $1=="file"{f=$2} END{printf "%.2f\t%.2f", a/2^30, f/2^30}' $CG/memory.stat 2>/dev/null)
    o=$(ps -C crimson-osd -o rss= | awk '{s+=$1} END{printf "%.2f", s/2^20}')
    c=$(ps -C ceph-vector-bench-native -o rss= | awk '{s+=$1} END{printf "%.2f", s/2^20}')
    echo -e "$(date -u +%FT%TZ)\t$(awk '{printf "%.2f", $1/2^30}' $CG/memory.current)\t$(awk '{printf "%.2f", $1/2^30}' $CG/memory.peak)\t$a\t$(oomk)\t$o\t$c" >> $O
    sleep 30
  done; }
fail_reason() { echo "oom_kill=$(oomk) osd_alive=$(osd_alive && echo y || echo n) osd_log: $(grep -ahiE 'bad_alloc|out of memory|abort|Aborting|failed to allocate' $R/ceph/logs/load/${1}_osd_stdout.log 2>/dev/null | tail -2 | cut -c1-200)"; }

run_arm() {   # <tag> <arm> ; returns 0 when the full sweep ran within the cap
  local tag=$1 ARM=$2
  mkcg || die_now "cannot create $CG"
  rm -f $D/ceph_$tag.stop; sampler $tag & local sp=$!
  log "[$tag] $ARM: create + load (cgroup $CG memory.max=$(cat $CG/memory.max), crimson_memory=${CRIMSON_MEMORY:-unset})"
  bash $S/create_and_load.sh $ARM > $R/ceph/logs/${ARM}_create.log 2>&1
  if [ $? != 0 ] || ! grep -q "LOADED_OK $ARM" $R/ceph/logs/${ARM}_create.log; then
    log "[$tag] RESULT: Cannot load within 48 GiB -- $(grep -a FATAL $R/ceph/logs/${ARM}_create.log | tail -1 | cut -c1-200); $(fail_reason $ARM); loaded so far: $(grep -aoE 'completed=[0-9]+|[0-9]+/100000000' $R/ceph/logs/load/${ARM}_load_*.log 2>/dev/null | tail -1)"
    touch $D/ceph_$tag.stop; return 1; fi
  log "[$tag] load ok: $(grep -aE '^  LOAD |records=' $R/ceph/logs/${ARM}_create.log | tr -s ' ' | cut -c1-200 | tr '\n' ' ') peak=$(awk '{printf "%.1f", $1/2^30}' $CG/memory.peak)G"
  bash $S/activate_workload.sh $ARM --skip-count > $R/ceph/logs/${ARM}_activate.log 2>&1 \
    || { log "[$tag] RESULT: Cannot run within 48 GiB (activate failed) $(fail_reason $ARM)"; touch $D/ceph_$tag.stop; return 1; }
  declare -A cq; local best=0
  # mem48 FAST_PROTO (user 2026-10-15): ladder stops at the first C that had failures or did not
  # beat the best smaller C by 3% (the selection rule could not pick it); 1 rep per M.
  for C in 20 40 80; do
    out=$(bash $S/run_one.sh $ARM 16 1024 $C tune 2>&1 | tail -2 | head -1)
    q=$(echo "$out" | grep -oE " qps=[0-9.]+" | cut -d= -f2); [ -n "$q" ] && cq[$C]=$q
    log "[$tag] tune M16 C$C: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|failures=[0-9]+' | tr '\n' ' ')"
    osd_alive || { log "[$tag] RESULT: Cannot run within 48 GiB (OSD died in C ladder) $(fail_reason $ARM)"; touch $D/ceph_$tag.stop; return 1; }
    if [ $FAST = 1 ]; then f=$(echo "$out" | grep -oE "failures=[0-9]+" | cut -d= -f2)
      if [ -z "$q" ] || [ "${f:-0}" != 0 ] || awk -v a=$q -v m=$best 'BEGIN{exit !(a<1.03*m)}'; then
        log "[$tag] C ladder early stop at C=$C (failures=${f:-NA} or < 1.03 x best $best)"; break; fi; fi
    [ -n "$q" ] && awk -v a=$q -v m=$best 'BEGIN{exit !(a>m)}' && best=$q
  done
  local max=0 BC=""; for C in "${!cq[@]}"; do awk -v a=${cq[$C]} -v b=$max 'BEGIN{exit !(a>b)}' && max=${cq[$C]}; done
  for C in 20 40 80; do [ -n "${cq[$C]:-}" ] && awk -v a=${cq[$C]} -v m=$max 'BEGIN{exit !(a>=0.97*m)}' && { BC=$C; break; }; done
  [ -n "$BC" ] || { log "[$tag] RESULT: Cannot run (C ladder empty) $(fail_reason $ARM)"; touch $D/ceph_$tag.stop; return 1; }
  echo -e "$ARM\t16\t$BC\t${cq[$BC]}" >> $R/ceph/results/chosen_c.tsv; log "[$tag] chosen C=$BC"
  for M in 16 32 64 128 8 96; do for r in $REPS; do
    out=$(bash $S/run_one.sh $ARM $M 1024 $BC r$r 2>&1 | tail -2 | head -1)
    echo "  [$ARM] $out" >> $R/ceph/logs/run_all.log
    log "[$tag] M$M r$r: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|failures=[0-9]+' | tr '\n' ' ')"
    osd_alive || { log "[$tag] RESULT: OSD died during M$M r$r $(fail_reason $ARM) -- partial sweep"; touch $D/ceph_$tag.stop; return 1; }
  done; done
  log "[$tag] RESULT: full sweep ran within 48 GiB (oom_kill=$(oomk), peak=$(awk '{printf "%.1f", $1/2^30}' $CG/memory.peak)G)"
  stop_osds; touch $D/ceph_$tag.stop; return 0
}

FAST=0; REPS="1 2 3"; [ -e $D/FAST_PROTO ] && { FAST=1; REPS="1"; }
log "=== ceph stage started (FAST_PROTO=$FAST: C ladder early stop, reps=$REPS)"
unset CRIMSON_MEMORY
if run_arm default d100m_m48def; then
  log "default ran within the cap -> memory-constrained arm not needed"
else
  stop_osds; bash $S/teardown.sh d100m_m48def >> $D/ceph_stage.out 2>&1; log "default cluster deleted"
  export CRIMSON_MEMORY=40G
  run_arm con d100m_m48con || { stop_osds; log "con arm also failed"; }
fi
stop_osds; log "=== CEPH STAGE DONE (cluster kept)"
