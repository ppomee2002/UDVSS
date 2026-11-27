#!/bin/bash
# load_progress.sh <pool> : every 10 min log WR_OPS (≈ vectors written) and the
# rate, so a load whose bench log stays empty until the end can be followed.
source /home/dev_path/experiments/paper-100m/sm/scripts/env.sh
P=$1; L=$EXP/logs/load_progress_$P.log; prev=0; t_prev=$(date +%s); t0=$t_prev
while sleep 600; do
  line=$($OB/rados -c $CONF df 2>/dev/null | awk -v p=$P '$1==p')
  [ -z "$line" ] && { echo "[$(date -u +%FT%TZ)] pool $P gone; stop" >> $L; exit 0; }
  w=$(echo "$line" | awk '{print $13}'); now=$(date +%s)   # WR_OPS: USED and RD are 'value unit' pairs
  awk -v w=$w -v p=$prev -v dt=$((now-t_prev)) -v el=$((now-t0)) -v t="$(date -u +%FT%TZ)" 'BEGIN{
    r=(w-p)/dt; avg=w/el; left=(1e8-w)/(avg>0?avg:1);
    printf "[%s] written=%d (%.2f%%) rate_now=%.0f/s rate_avg=%.0f/s eta_h=%.1f\n", t, w, w/1e6, r, avg, left/3600}' >> $L
  prev=$w; t_prev=$now
  grep -q "^LOAD" $EXP/logs/load_$P.log 2>/dev/null && { echo "[$(date -u +%FT%TZ)] load finished" >> $L; exit 0; }
done
