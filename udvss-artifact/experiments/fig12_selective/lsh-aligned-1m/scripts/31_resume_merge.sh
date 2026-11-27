#!/bin/bash
# Resume 30_splitmerge.sh after a pause. Split stages and the merge pool's
# pg 1024 stage are done (results/splitmerge). Restarts this cluster's
# daemons if they are down (never re-runs 00_cluster.sh, which wipes
# $VDEST), then for each remaining merge stage: re-issues pg_num (a no-op
# if the earlier request is still in flight), waits for clean, and runs the
# stage unless its tree and fullscan sweeps are both complete. A stage cut
# off mid-sweep is dropped from the raw files and measured again.
source /home/dev_path/experiments/lsh-aligned-1m/scripts/env.sh
IB=$IB_NEW
eval "$(sed -n '/^mk()/,/^}$/p;/^census_()/,/^}$/p;/^sweep()/,/^}$/p;/^fsweep()/,/^}$/p;/^stage()/,/^}$/p;/^change()/,/^}$/p' $EXP/scripts/30_splitmerge.sh)"
R=$EXP/results/splitmerge; RAW=$R/raw.txt; RAWF=$R/raw_fullscan.txt

# Exact process names only: pids_of() uses pgrep -f, which also matches any
# shell whose command line merely mentions the daemon and the conf path.
running() { for p in $(pgrep -x $1); do grep -qaF -- "$CONF" /proc/$p/cmdline 2>/dev/null && return 0; done; return 1; }
start_daemons() {
  ulimit -l unlimited
  running ceph-mon || bash -c 'exec nohup '"$NB"'/ceph-mon -i a -c '"$CONF"' -f' >> $EXP/logs/mon_stdout.log 2>&1 &
  sleep 5
  running ceph-mgr || bash -c 'exec nohup '"$OB"'/ceph-mgr -i x -c '"$CONF"' -f' >> $EXP/logs/mgr_stdout.log 2>&1 &
  running crimson-osd || bash -c 'ulimit -l unlimited; exec nohup '"$NB"'/crimson-osd -i 0 -c '"$CONF"' -f' >> $EXP/logs/osd_stdout.log 2>&1 &
  for i in $(seq 1 120); do sleep 5; timeout 20 $OB/ceph -c $CONF -s 2>/dev/null | grep -qE "osd: 1 osds: 1 up" && break; done
  ceph_ -s | grep -qE "osd: 1 osds: 1 up" || { log "FATAL: osd not up"; exit 1; }
  log "daemons up: $(ceph_ pg stat)"
}
done_stage() {  # tag -> 0 if both sweeps finished (last M present in each raw)
  local last=${MLIST##* }
  grep -q "^$1 .* m=$last " $RAW 2>/dev/null && grep -q "^f$1 .* m=$last " $RAWF 2>/dev/null
}
drop_partial() {
  sed -i "/^$1 /d" $RAW 2>/dev/null; sed -i "/^f$1 /d" $RAWF 2>/dev/null
}

start_daemons
osd_watchdog & WATCHDOG=$!
trap "kill $WATCHDOG 2>/dev/null" EXIT
for N in 512 256 128; do
  tag=amerge_pg$N
  done_stage $tag && { log "$tag already complete, skipping"; continue; }
  drop_partial $tag
  change amerge1m $N 14400
  stage amerge1m $N $tag fullscan
done
log "=== lsh-aligned split/merge 1M run complete (resumed) ==="
