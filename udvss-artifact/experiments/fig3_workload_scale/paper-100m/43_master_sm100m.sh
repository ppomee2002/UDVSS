#!/bin/bash
# 2026-10-09 (user, before sleeping): after pivot-variants gi_bal finishes, recreate
# paper-sm from scratch and run sm 100m (split, then merge) with load concurrency 128.
# Continue even if the load is slow. Stops at the first failure.
P=/home/dev_path/experiments/paper-100m; L=$P/master.log
source $P/sm/scripts/env.sh
mlog() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
mlog "=== 43: waiting for pivot-variants (continue_gi_bal.sh) to finish ==="
while pgrep -f continue_gi_bal.sh > /dev/null || pgrep -f "pivot-variants/bench/scripts/run_" > /dev/null; do sleep 60; done
mlog "pivot-variants finished: $(tail -1 /home/dev_path/experiments/pivot-variants/bench/logs/run_all.log)"
# no other OSD may be running (one measurement at a time)
for p in $(pgrep -x crimson-osd); do
  mlog "FATAL: a crimson-osd is still running: $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-120)"; exit 1
done
# old paper-sm mon/mgr (OSD already down since 2026-10-09 ~13:57Z)
for p in $(pids_of 'ceph-mon|ceph-mgr'); do kill -TERM $p; done
for i in $(seq 1 30); do [ -z "$(pids_of 'ceph-mon|ceph-mgr')" ] && break; sleep 2; done
for p in $(pids_of 'ceph-mon|ceph-mgr'); do kill -9 $p; done
mlog "old paper-sm daemons stopped; recreating cluster (00_cluster.sh wipes $VDEST)"
mkdir -p $EXP/results/100m_aborted_conc32 && mv $EXP/logs/load_fsplit100m.log $EXP/results/100m_aborted_conc32/ 2>/dev/null
$P/sm/scripts/00_cluster.sh >> $P/sm/run_100m.log 2>&1 || { mlog "FATAL: 00_cluster (see sm/run_100m.log)"; exit 1; }
mlog "cluster up; === sm 100m, LOAD_CONC=128 ==="
( sleep 120; $P/sm/scripts/load_progress.sh fsplit100m ) &
( while ! $OB/ceph -c $CONF osd pool ls 2>/dev/null | grep -qx fmerge100m; do sleep 300; done
  $P/sm/scripts/load_progress.sh fmerge100m ) &
LOAD_CONC=128 $P/sm/scripts/30_splitmerge.sh 100m both >> $P/sm/run_100m.log 2>&1 \
  || { mlog "FATAL: sm 100m (see sm/run_100m.log)"; exit 1; }
mlog "=== MASTER DONE (43) ==="
