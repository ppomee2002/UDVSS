#!/bin/bash
# pglog_arm.sh <vstart-dir-name> <out.tsv>: every 60 s total PG log entries, max/PG, total writes, OSD RSS GB, seastar allocated GB
N=$1; OUT=$2; C="/home/dev_path/ceph-vectornode-seastore-batch/build/bin/ceph -c /vstart-extra/$N/ceph.conf"; O=/home/dev_path/experiments/scale-deep/mem48/memprof
while [ ! -e $O/stop ]; do
  p=$(for q in $(pgrep -x crimson-osd); do grep -qa "/$N/" /proc/$q/cmdline && echo $q; done | head -1)
  if [ -n "$p" ]; then
  r=$(timeout 60 $C pg dump pgs --format json 2>/dev/null | grep -v DEVELOPER | python3 -c "
import json,sys; d=json.load(sys.stdin); p=d['pg_stats'] if isinstance(d,dict) else d
print(sum(x['log_size'] for x in p), max(x['log_size'] for x in p), sum(x['stat_sum']['num_write'] for x in p))" 2>/dev/null)
  a=$(timeout 60 $C daemon osd.0 dump_metrics memory_allocated_memory 2>/dev/null | grep -o '"value": [0-9]*' | awk '{s+=$2} END{printf "%.2f", s/2^30}')
  echo -e "$(date -u +%FT%TZ)\t$r\t$(awk '/VmRSS/{printf "%.2f",$2/2^20}' /proc/$p/status 2>/dev/null)\t$a" >> $OUT
  fi
  sleep 60
done
