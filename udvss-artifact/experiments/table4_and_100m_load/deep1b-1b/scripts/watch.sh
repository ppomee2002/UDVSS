#!/bin/bash
# Every 60 s: OSD / client RSS, MemAvailable, OSD log size -> results/mem.tsv.
#  * OSD log > 20 GiB: tail kept as osd.0.log.tail-<utc>, log truncated (100M's
#    log reached 197 GiB and filled the root disk).
#  * MemAvailable < 6 GiB: STOP file + kill bench clients so the OSD is not
#    OOM-killed (an unclean crimson stop could not be restarted at 100M).
#  * OSD gone: ALERT once.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
T=$X/results/mem.tsv
[ -s $T ] || echo -e "utc\tosd_rss_g\tclient_rss_g\tavail_g\tosd_log_g\tdev_used_g" > $T
alerted=0
while [ ! -e $X/WATCH_STOP ]; do
  P=$(osd_pid)
  o=$( [ -n "$P" ] && ps -o rss= -p $P | awk '{printf "%.1f", $1/2^20}' || echo NA)
  c=$(ps -eo rss,args | grep -E "[c]eph-vector-bench-native .*--pool $POOL " | awk '{s+=$1} END{printf "%.1f", s/2^20}')
  a=$(awk '/MemAvailable/{printf "%.1f", $2/2^20}' /proc/meminfo)
  lg=$(stat -c %s $V/out/osd.0.log 2>/dev/null | awk '{printf "%.1f", $1/2^30}')
  du=$(du -s --block-size=1G $V/dev 2>/dev/null | cut -f1)
  echo -e "$(date -u +%FT%TZ)\t$o\t$c\t$a\t${lg:-NA}\t${du:-NA}" >> $T
  if [ -n "$lg" ] && awk -v g=$lg 'BEGIN{exit !(g>20)}'; then
    tail -c 200000000 $V/out/osd.0.log > $V/out/osd.0.log.tail-$(date -u +%Y%m%dT%H%M%SZ)
    : > $V/out/osd.0.log; log "watch: OSD log ${lg}G -> tail kept, truncated"
  fi
  if awk -v a=$a 'BEGIN{exit !(a<6)}'; then
    touch $X/STOP
    for q in $(ps -eo pid,args | grep -E "[c]eph-vector-bench-native .*--pool $POOL " | awk '{print $1}'); do kill $q; done
    log "watch: ALERT MemAvailable ${a}G < 6G -> STOP + bench clients killed (OSD RSS ${o}G)"
  fi
  if [ -z "$P" ] && [ -e $CONF ] && [ $alerted = 0 ]; then log "watch: ALERT OSD not running"; alerted=1; fi
  [ -n "$P" ] && alerted=0
  sleep 60
done
