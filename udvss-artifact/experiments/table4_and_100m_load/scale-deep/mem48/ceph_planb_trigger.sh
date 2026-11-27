#!/bin/bash
# Plan B trigger (user 2026-10-16): if the d100m_m48noomap LOAD dies, immediately tear it down
# (keeping its logs) and run ceph_noomap46_run.sh (seastar 46 GiB). Does nothing if the load succeeds.
R=/home/dev_path/experiments/scale-deep; D=$R/mem48; L=$D/chain.log; S=$R/ceph/scripts
until grep -qE '\[noomap\] (RESULT: Cannot load|load ok)' $L; do sleep 30; done
grep -q '\[noomap\] load ok' $L && { echo "[$(date -u +%FT%TZ)] ceph: plan B not needed (noomap load ok)" >> $L; exit 0; }
E=$D/ceph_noomap_evidence; mkdir -p $E; cp -p $R/ceph/logs/load/d100m_m48noomap_* $R/ceph/logs/d100m_m48noomap_create.log $E/ 2>/dev/null
tail -c 50000000 /vstart-extra/sd-d100m-m48noomap/out/osd.0.log > $E/osd.0.tail50MB.log 2>/dev/null
for q in $(ps -eo pid,comm | awk '$2 ~ /^ceph-vector-ben/ {print $1}'); do kill $q; done
for q in $(pgrep -x crimson-osd); do kill $q; done; sleep 20; for q in $(pgrep -x crimson-osd); do kill -9 $q; done
bash $S/teardown.sh d100m_m48noomap >> $D/ceph_stage.out 2>&1
echo "[$(date -u +%FT%TZ)] ceph: PLAN B triggered: noomap load failed; cluster torn down (evidence $E); $(df -h / | tail -1)" >> $L
sed -i "s/=== MEM48 CHAIN DONE ===/=== noomap stage ended (plan B follows) ===/" $L   # so the restarted watch keeps running
cd $D; for q in $(ps -eo pid,args | awk '$2=="bash" && $3=="ceph_watch.sh" {print $1}'); do kill $q; done
setsid bash ceph_watch.sh > ceph_watch.out 2>&1 < /dev/null &
exec bash $D/ceph_noomap46_run.sh > $D/ceph_noomap46_run.out 2>&1
