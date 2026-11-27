#!/bin/bash
# 2026-10-17 user ("자동으로 비워도 돼"): keep dev1's stale page cache from squeezing Vespa's paged
# vectors (host-wide memory pressure stalled the feed 11-13Z). Every 60 s: if the dev1 main cgroup
# holds > 5 GiB of file cache, reclaim it down to ~1 GiB, file pages only (swappiness=0: no anon
# swap-out, Ceph OSD untouched). Exits when the Vespa mem40 run is done.
D=/home/dev_path/experiments/scale-deep/mem40; CG=/sys/fs/cgroup/main; L=$D/cache_keeper.log
while ! grep -q "=== VESPA MEM40 DONE ===" $D/driver.log 2>/dev/null; do
  f=$(awk '$1=="file"{print int($2/2^20)}' $CG/memory.stat)
  if [ "$f" -gt 5120 ]; then
    echo "$((f-1024))M swappiness=0" > $CG/memory.reclaim 2>>$L
    echo "[$(date -u +%FT%TZ)] reclaimed: file ${f}M -> $(awk '$1=="file"{print int($2/2^20)}' $CG/memory.stat)M" >> $L
  fi
  sleep 60
done
echo "[$(date -u +%FT%TZ)] vespa done; exit" >> $L
