#!/bin/bash
# Fresh cluster for split/merge: as 00_cluster.sh, but the mon is swapped for
# the bundled pg-split ceph-mon (crimson_allow_pg_merge exists only there),
# same recipe as lsh-aligned-1m/scripts/00_cluster.sh.
source $HOME/hier/scripts/env.sh
set -e  # (see 30b_swap_mon.sh if this stops after 00)
# stop our previous cluster (explicit PIDs, only processes naming $H/)
for p in $(ps -eo pid,args | grep -E "[h]ier/(cluster|osd|bundle)" | grep -E "ceph-run|ceph-mon|ceph-mgr|crimson-osd" | awk '{print $1}'); do
  kill $p 2>/dev/null || true
done
for i in $(seq 1 30); do
  [ -z "$(ps -eo args | grep -E '[h]ier/(cluster|osd|bundle)' | grep -E 'ceph-mon|ceph-mgr|crimson-osd')" ] && break; sleep 2
done
for p in $(ps -eo pid,args | grep -E "[h]ier/(cluster|osd|bundle)" | grep -E "ceph-mon|ceph-mgr|crimson-osd" | awk '{print $1}'); do kill -9 $p; done
rm -rf $H/osd
log "old cluster stopped"
bash $H/scripts/00_cluster.sh
# swap mon
for p in $(ps -eo pid,args | grep "[h]ier/cluster/ceph.conf" | grep -E "ceph-run .*ceph-mon|bin/ceph-mon" | awk '{print $1}'); do kill $p; done
for i in $(seq 1 15); do [ -z "$(ps -eo args | grep '[h]ier/cluster/ceph.conf' | grep ceph-mon)" ] && break; sleep 2; done
cd $VDEST && setsid -f $BUNDLE/run.sh ceph-mon -i a -c $CONF --erasure_code_dir=$BUNDLE/plugins --plugin_dir=$BUNDLE/plugins -f > $H/logs/mon_stdout.log 2>&1 < /dev/null
sleep 5
ceph_ config set global crimson_allow_pg_merge true
ceph_ config get mon crimson_allow_pg_merge
# OSD picks the option up at start: restart it
for p in $(ps -eo pid,args | grep "[h]ier/bundle/bin/crimson-osd" | awk '{print $1}'); do kill $p; done
for i in $(seq 1 30); do [ -z "$(ps -eo args | grep '[h]ier/bundle/bin/crimson-osd')" ] && break; sleep 2; done
echo "        crimson_allow_pg_merge = true" >> $H/osd/ceph.conf
ulimit -n 1048576
cd $H/osd && setsid -f $BUNDLE/run.sh crimson-osd -i 0 -c $H/osd/ceph.conf -f > $H/osd/out/osd.0.stdout2 2>&1 < /dev/null
for t in $(seq 1 90); do ceph_ osd stat | grep -q "1 up" && break; sleep 5; done
ceph_ -s | grep -E "mon:|osd:|mgr:"
ps -eo args | grep -E "[h]ier/" | grep -E "ceph-mon|crimson-osd|ceph-mgr" | cut -c1-110
log "=== 30 complete ==="
