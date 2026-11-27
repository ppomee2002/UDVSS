#!/bin/bash
# 10M memory profile (uncapped OSD, PG16384): every 30 s, per-shard seastar memory + SeaStore cache metrics + RSS.
BIN=/home/dev_path/ceph-vectornode-seastore-batch/build/bin; CONF=/vstart-extra/sd-d10m-memprof/ceph.conf
O=/home/dev_path/experiments/scale-deep/mem48/memprof; STOP=$O/stop
while [ ! -e $STOP ]; do
  p=$(for q in $(pgrep -x crimson-osd); do grep -qa sd-d10m-memprof /proc/$q/cmdline && echo $q; done | head -1)
  if [ -n "$p" ]; then
    ts=$(date -u +%FT%TZ); rss=$(awk '/VmRSS/{print $2}' /proc/$p/status)
    timeout 60 $BIN/ceph -c $CONF daemon osd.0 dump_metrics 2>/dev/null > $O/m_$ts.json
    echo -e "$ts\t$rss" >> $O/rss.tsv
  fi
  sleep 30
done
