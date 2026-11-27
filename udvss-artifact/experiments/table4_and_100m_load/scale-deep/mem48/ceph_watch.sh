#!/bin/bash
# Watchdog for the mem48 Ceph stage (user 2026-10-15: "ceph 감시 잘해, 잘 터지더라").
# Passive: never restarts anything, only records. Appends one line per event to ceph_watch.log:
#   STAGE   new "ceph:" line in chain.log (RESULT / tune / M runs)
#   OSD_UP / OSD_DIED  crimson-osd of an m48 arm appeared / vanished (crash evidence copied
#                      to ceph_crash_<utc>.log before teardown can delete anything)
#   OOM     memory.events oom_kill of /sys/fs/cgroup/osd48 increased
#   OSDERR  new abort/assert/segfault/bad_alloc lines in the OSD stdout log
#   STALL   OSD stdout and load/bench logs unchanged for 30 min while the OSD is up
#   HB      every 30 min: cgroup cur/peak, anon/file, OSD RSS
D=/home/dev_path/experiments/scale-deep/mem48; R=/home/dev_path/experiments/scale-deep
L=$D/chain.log; W=$D/ceph_watch.log; CG=/sys/fs/cgroup/osd48
ev() { echo "[$(date -u +%FT%TZ)] $*" >> $W; }
osdpid() { for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-d100m-m48" /proc/$p/cmdline 2>/dev/null && { echo $p; return; }; done; }
arm_of() { tr '\0' ' ' < /proc/$1/cmdline | grep -oE 'sd-d100m-m48(def|con|noomap46|noomap)' | sed 's/sd-d100m-m48/d100m_m48/'; }
oomk() { awk '$1=="oom_kill"{print $2}' $CG/memory.events 2>/dev/null; }
mem() { printf "cur=%.1fG peak=%.1fG %s osd_rss=%sG" $(awk '{print $1/2^30}' $CG/memory.current 2>/dev/null || echo 0) \
  $(awk '{print $1/2^30}' $CG/memory.peak 2>/dev/null || echo 0) \
  "$(awk '$1=="anon"{a=$2} $1=="file"{f=$2} END{printf "anon=%.1fG file=%.1fG", a/2^30, f/2^30}' $CG/memory.stat 2>/dev/null)" \
  "$(ps -C crimson-osd -o rss= | awk '{s+=$1} END{printf "%.1f", s/2^20}')"; }
ERR='Aborting|ceph_assert|FAILED assert|Segmentation|SIGSEGV|SIGABRT|terminate called|bad_alloc|failed to allocate|out of memory|Backtrace:.*abort'
ev "watch started"
nl=$(wc -l < $L); pid=""; arm=""; k0=$(oomk); k0=${k0:-0}; nerr=0; hb=0; cpu0=0; last_act=$(date +%s)
while ! grep -q "=== MEM48 CHAIN DONE" $L; do
  # chain.log ceph lines
  n=$(wc -l < $L); [ $n -gt $nl ] && sed -n "$((nl+1)),${n}p" $L | grep -a "ceph:" | cut -c1-300 | while read -r x; do ev "STAGE $x"; done; nl=$n
  p=$(osdpid)
  if [ -n "$p" ] && [ "$p" != "$pid" ]; then
    pid=$p; arm=$(arm_of $p); OL=$R/ceph/logs/load/${arm}_osd_stdout.log
    nerr=$(grep -acE "$ERR" $OL 2>/dev/null); nerr=${nerr:-0}; last_act=$(date +%s); ev "OSD_UP pid=$pid arm=$arm $(mem)"
  elif [ -z "$p" ] && [ -n "$pid" ]; then
    f=$D/ceph_crash_$(date -u +%Y%m%dT%H%M%SZ).log
    { echo "== memory.events"; cat $CG/memory.events 2>/dev/null; echo "== chain.log tail"; tail -5 $L
      echo "== osd stdout tail ($OL)"; tail -80 $OL 2>/dev/null
      echo "== latest load/bench logs"; for g in $(ls -t $R/ceph/logs/load/${arm}_load_* $R/ceph/logs/$arm/*.log 2>/dev/null | head -2); do echo "-- $g"; tail -15 $g; done; } > $f 2>&1
    if grep -q "RESULT: full sweep ran" <(tail -3 $L); then ev "OSD_STOPPED pid=$pid arm=$arm (normal end of arm)"
    else ev "OSD_DIED pid=$pid arm=$arm oom_kill=$(oomk) evidence=$f last_err: $(grep -aE "$ERR" $OL 2>/dev/null | tail -1 | cut -c1-200)"; fi
    pid=""
  fi
  k=$(oomk); k=${k:-0}; [ "$k" -gt "$k0" ] && { ev "OOM oom_kill $k0 -> $k $(mem)"; k0=$k; }
  if [ -n "$pid" ]; then
    c=$(grep -acE "$ERR" $OL 2>/dev/null); c=${c:-0}
    [ "$c" -gt "$nerr" ] && { ev "OSDERR $(grep -aE "$ERR" $OL | tail -1 | cut -c1-250)"; nerr=$c; }
    # the load log prints nothing until the end, so "alive" = the OSD keeps burning CPU
    t=$(awk '{print $14+$15}' /proc/$pid/stat 2>/dev/null); t=${t:-0}
    [ $(( t - cpu0 )) -gt 100 ] && { cpu0=$t; last_act=$(date +%s); }
    [ $(( $(date +%s) - last_act )) -gt 1800 ] && { ev "STALL osd pid=$pid used <1 s CPU in 30 min $(mem)"; last_act=$(date +%s); }
    [ $(( $(date +%s) - hb )) -ge 1800 ] && { ev "HB arm=$arm $(mem) oom_kill=$k"; hb=$(date +%s); }
  fi
  sleep 60
done
ev "chain done; watch exits"
