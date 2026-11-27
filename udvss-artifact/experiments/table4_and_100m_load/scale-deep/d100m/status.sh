#!/bin/bash
# status.sh <arm> : every 10 min rewrite STATUS.md (+ append metrics.tsv) so the
# 100M run can be followed without reading logs: phase, load progress/rate/ETA,
# memory (OSD / client RSS, host), device + disk use, and every measured point
# next to the route-sim prediction for the same M.
ARM=${1:?}; D=/home/dev_path/experiments/scale-deep/d100m; C=/home/dev_path/experiments/scale-deep/ceph
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256; source $C/scripts/bench_env.sh >/dev/null 2>&1; ALLOW_MISSING_CLUSTER=1 load_workload $ARM >/dev/null 2>&1
CONF=$WL_CLUSTER/ceph.conf; RS=$(ls /home/dev_path/experiments/scale-deep/routesim100m/logs/bal_pg${WL_PGNUM}_*.log 2>/dev/null | head -1)
M=$D/metrics.tsv; [ -s $M ] || echo -e "utc\tphase\twritten\trate_now\trate_avg\teta_h\tosd_rss_gb\tclient_rss_gb\thost_avail_gb\tdev_used\tdisk_free" > $M
prev=0; t_prev=$(date +%s); t0=""
while true; do
  now=$(date +%s); utc=$(date -u +%FT%TZ)
  phase=$(tail -1 $D/driver.log 2>/dev/null | cut -c1-120)
  w=$($BIN/rados -c $CONF df 2>/dev/null | awk -v p=$WL_POOL '$1==p{print $13}'); w=${w:-0}
  [ -z "$t0" ] && [ "$w" -gt 0 ] && { t0=$t_prev; }
  rn=$(( (w-prev) / (now-t_prev>0?now-t_prev:1) )); ra=0; eta=NA
  [ -n "$t0" ] && { el=$((now-t0)); [ $el -gt 0 ] && ra=$((w/el)); [ $ra -gt 0 ] && eta=$(awk -v w=$w -v r=$ra -v n=$WL_BASE_COUNT 'BEGIN{printf "%.1f",(n-w)/r/3600}'); }
  osd=$(for p in $(pgrep -x crimson-osd); do grep -qa "$WL_CLUSTER/" /proc/$p/cmdline && ps -o rss= -p $p; done | awk '{s+=$1}END{printf "%.1f",s/1048576}')
  cli=$(ps -C ceph-vector-bench-native -o rss= | awk '{s+=$1}END{printf "%.1f",s/1048576}')
  av=$(free -g | awk '/Mem:/{print $7}')
  dev=$($BIN/ceph -c $CONF pg stat 2>/dev/null | grep -oE "[0-9.]+ [KMGT]iB used" | head -1)
  dk=$(df -h /vstart-extra | awk 'NR==2{print $4}')
  echo -e "$utc\t$(echo "$phase" | cut -c24-60)\t$w\t$rn\t$ra\t$eta\t$osd\t$cli\t$av\t$dev\t$dk" >> $M
  prev=$w; t_prev=$now
  {
    echo "# 100M status ($ARM, PG $WL_PGNUM) — updated $utc"
    echo; echo "**Phase:** \`$phase\`"; echo
    echo "**Load:** written $w / $WL_BASE_COUNT ($(awk -v w=$w -v n=$WL_BASE_COUNT 'BEGIN{printf "%.1f",100*w/n}')%), rate now ${rn}/s, avg ${ra}/s, ETA ${eta} h"
    echo; echo "**Memory:** OSD RSS ${osd} GB, client RSS ${cli} GB, host available ${av} GB · **Device:** ${dev:-NA} of $SEASTORE_DEVICE_SIZE · disk free ${dk}"
    echo; echo "## Measured points (M, C, QPS, recall, candidates, p50) vs route-sim"
    echo; echo "| M | C | rep | QPS | recall | route-sim recall | cand | route-sim cand | p50 s |"; echo "|---|---|---|---|---|---|---|---|---|"
    for f in $(ls $C/results/$ARM/*.row 2>/dev/null); do
      awk -F'\t' -v rs="$RS" 'BEGIN{while((getline l < rs)>0) if(l~/^ROUTE_SIM/){split(l,a," "); m=substr(a[2],3); r[m]=substr(a[4],14); c[m]=substr(a[6],17)}}
        {printf "| %s | %s | %s | %.1f | %.4f | %s | %.0f | %.0f | %.1f |\n",$2,$4,$5,$17,$26,r[$2],$24,c[$2],$19/1e6}' $f
    done
    echo; echo "## Trend (last 12 samples, every 10 min)"; echo; echo '```'; (head -1 $M; tail -12 $M) | column -t -s$'\t'; echo '```'
  } > $D/STATUS.md.tmp && mv $D/STATUS.md.tmp $D/STATUS.md
  grep -q "=== D100M DONE" $D/driver.log 2>/dev/null && exit 0
  sleep 600
done
