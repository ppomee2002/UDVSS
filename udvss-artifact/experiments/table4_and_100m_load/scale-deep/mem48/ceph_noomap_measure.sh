#!/bin/bash
# Takes over d100m_m48noomap after its load (user 2026-10-16): the cluster must survive, a
# re-load is not affordable before the deadline, and only QPS is needed.
#  - C fixed at 20 (no ladder: more in-flight requests = more OSD memory), 1 rep per M,
#    M ascending 8 16 32 64 96 128.
#  - memory guard on seastar's per-shard allocator (the default arm died of std::bad_alloc
#    when one shard ran out; per-shard total = memory_total_memory ~5.57 GiB):
#      before a run: skip it and every larger M if any shard > PRE_GB
#      during a run: kill the bench client (the OSD stays up) if any shard > ABORT_GB
#    so the run ends with "M>=X not measured: 48 GiB guard" instead of a crashed cluster.
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; S=$R/ceph/scripts; L=$D/chain.log
ARM=d100m_m48noomap; CONF=/vstart-extra/sd-d100m-m48noomap/ceph.conf
BIN=/home/dev_path/ceph-vectornode-seastore-batch/build/bin
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256 LOAD_CONC=256
export OSD_CG=/sys/fs/cgroup/osd48 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1; unset CRIMSON_MEMORY
export WARMUP_C=20   # activate warm-up at the same C (default 40)
PRE_GB=${PRE_GB:-5.2}; ABORT_GB=${ABORT_GB:-5.4}; C=20
log() { echo "[$(date -u +%FT%TZ)] ceph: [noomap] $*" | tee -a $L; }
shard_max() { timeout 20 $BIN/ceph -c $CONF daemon osd.0 dump_metrics memory_allocated_memory 2>/dev/null | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)['metrics']; print('%.3f' % max(m['memory_allocated_memory']['value']/2**30 for m in d if 'memory_allocated_memory' in m))
except Exception: print('NA')"; }
anon() { awk '$1=="anon"{printf "%.1f", $2/2^30}' $OSD_CG/memory.stat; }
osd_alive() { pgrep -x crimson-osd >/dev/null; }
clients() { ps -eo pid,comm | awk '$2 ~ /^ceph-vector-ben/ {print $1}'; }
sampler() { local O=$D/ceph_noomap_mem.tsv
  while [ ! -e $D/ceph_noomap.stop ]; do
    a=$(awk '$1=="anon"{a=$2} $1=="file"{f=$2} END{printf "%.2f\t%.2f", a/2^30, f/2^30}' $OSD_CG/memory.stat 2>/dev/null)
    o=$(ps -C crimson-osd -o rss= | awk '{s+=$1} END{printf "%.2f", s/2^20}')
    c=$(ps -C ceph-vector-bench-native -o rss= | awk '{s+=$1} END{printf "%.2f", s/2^20}')
    k=$(awk '$1=="oom_kill"{print $2}' $OSD_CG/memory.events)
    echo -e "$(date -u +%FT%TZ)\t$(awk '{printf "%.2f", $1/2^30}' $OSD_CG/memory.current)\t$(awk '{printf "%.2f", $1/2^30}' $OSD_CG/memory.peak)\t$a\t$k\t$o\t$c\tshardmax=$(shard_max)" >> $O
    sleep 30; done; }
hang_guard() { local gone=0
  while [ ! -e $D/ceph_noomap.stop ]; do
    if osd_alive; then gone=0; else gone=$((gone+10)); fi
    if [ $gone -ge 60 ]; then for q in $(clients); do log "hang_guard: OSD gone ${gone}s -> killing bench client $q"; kill $q; done; gone=0; fi
    sleep 10; done; }

rm -f $D/ceph_noomap.stop; sampler & hang_guard &
log "measure takeover: waiting for create_and_load $ARM to finish"
while ps -eo args | grep -q "^bash $S/create_and_load.sh $ARM"; do sleep 20; done
CL=$R/ceph/logs/${ARM}_create.log
if ! grep -q "LOADED_OK $ARM" $CL; then
  log "RESULT: Cannot load within 48 GiB -- $(grep -a FATAL $CL | tail -1 | cut -c1-200); oom_kill=$(awk '$1=="oom_kill"{print $2}' $OSD_CG/memory.events) osd_alive=$(osd_alive && echo y || echo n) osd_log: $(grep -ahiE 'bad_alloc|Aborting' $R/ceph/logs/load/${ARM}_osd_stdout.log | tail -1 | cut -c1-150)"
  touch $D/ceph_noomap.stop; echo "[$(date -u +%FT%TZ)] === MEM48 CHAIN DONE ===" >> $L; exit 1
fi
log "load ok: $(grep -aE '^  LOAD |records=' $CL | tr -s ' ' | cut -c1-200 | tr '\n' ' ') peak=$(awk '{printf "%.1f", $1/2^30}' $OSD_CG/memory.peak)G anon=$(anon)G shardmax=$(shard_max)G"
bash $S/activate_workload.sh $ARM --skip-count > $R/ceph/logs/${ARM}_activate.log 2>&1; arc=$?
p=$(for q in $(pgrep -x crimson-osd); do grep -qa sd-d100m-m48noomap /proc/$q/cmdline && echo $q; done | head -1)
log "activate rc=$arc osd=$p cgroup=$(cat /proc/$p/cgroup 2>/dev/null) flag=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -c NO_OMAP) anon=$(anon)G shardmax=$(shard_max)G"
if [ $arc != 0 ] || [ -z "$p" ] || ! grep -q osd48 /proc/$p/cgroup; then
  log "RESULT: activate failed or OSD not in the 48G cgroup -- stopping (cluster kept)"; touch $D/ceph_noomap.stop; exit 1; fi
echo -e "$ARM\t16\t$C\tfixed_C20_no_ladder" >> $R/ceph/results/chosen_c.tsv
STOPPED=""
for M in 8 16 32 64 96 128; do
  sm=$(shard_max)
  if [ "$sm" != NA ] && awk -v a=$sm -v t=$PRE_GB 'BEGIN{exit !(a>t)}'; then
    STOPPED="M>=$M not measured: 48 GiB guard (shard max ${sm}G > ${PRE_GB}G before the run)"; log "$STOPPED"; break; fi
  bash $S/run_one.sh $ARM $M 1024 $C r1 > $D/run_m$M.out 2>&1 & rp=$!
  ab=0
  while kill -0 $rp 2>/dev/null; do
    sm=$(shard_max)
    if [ "$sm" != NA ] && awk -v a=$sm -v t=$ABORT_GB 'BEGIN{exit !(a>t)}'; then
      ab=1; log "M$M: shard max ${sm}G > ${ABORT_GB}G during the run -> killing bench client to save the OSD"
      for q in $(clients); do kill $q; done; fi
    osd_alive || break
    sleep 3; done
  wait $rp
  out=$(tail -2 $D/run_m$M.out | head -1); echo "  [$ARM] $out" >> $R/ceph/logs/run_all.log
  log "M$M r1: $(echo "$out" | grep -oE 'OK|FAILED|qps=[0-9.]+|recall=[0-9.]+|failures=[0-9]+' | tr '\n' ' ') shardmax=$(shard_max)G anon=$(anon)G"
  osd_alive || { STOPPED="OSD died during M$M"; log "RESULT: $STOPPED $(grep -ahiE 'bad_alloc|Aborting' $R/ceph/logs/load/${ARM}_osd_stdout.log | tail -1 | cut -c1-150)"; break; }
  [ $ab = 1 ] && { STOPPED="M>=$M aborted by the 48 GiB guard (OSD kept alive)"; log "$STOPPED"; break; }
done
log "RESULT: loads within 48 GiB; ${STOPPED:-all M measured} (oom_kill=$(awk '$1=="oom_kill"{print $2}' $OSD_CG/memory.events), peak=$(awk '{printf "%.1f", $1/2^30}' $OSD_CG/memory.peak)G); cluster kept, OSD left running"
touch $D/ceph_noomap.stop
echo "[$(date -u +%FT%TZ)] === MEM48 CHAIN DONE ===" >> $L
