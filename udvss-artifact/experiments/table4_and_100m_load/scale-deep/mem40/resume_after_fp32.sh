#!/bin/bash
# 2026-10-17 user: resume the paused Vespa mem40 feed as soon as the FP32 4G Ceph run is done.
# Stops the fp32 test OSD first (cores 8-9, overlaps feeder core 9; results already written).
L=/home/dev_path/experiments/scale-deep/mem40/driver_notes.log
until grep -q "NOOMAP_PGLOG FP32 DONE" /home/dev_path/experiments/memfootprint-sq8/ceph/logs/chain_noomap_pglog_fp32.out; do sleep 20; done
for p in $(pgrep -f "crimson-osd -i 0 -c /home/dev_path/vstart/memfp2-fp32"); do kill $p; done
sleep 10
P="1211209 1211230 1211231 1211232 1211233 1211234 1211235 1211236 1211237"
kill -CONT $P
echo "[$(date -u +%FT%TZ)] fp32 done; fp32 OSD stopped; feeder SIGCONT ($P)" >> $L
