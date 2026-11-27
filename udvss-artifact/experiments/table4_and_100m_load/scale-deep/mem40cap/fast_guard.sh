#!/bin/bash
# user 2026-10-28: "60분까지 기다리지말고 그런 경향 보이면 바로 컷". Tighter external stop
# rule on top of run.sh's own (run.sh is running; not edited). Every 30 s, after 10M docs:
#  (a) the last 2 completed 1M intervals both < 50% of normal (median of the 3M-10M intervals), or
#  (b) the current (unfinished) 1M interval already exceeds 2x the normal interval AND
#      io.pressure full avg10 >= 20 in the last 4 cgroup samples (thrashing),
# -> kill the feeder; run.sh then records the RESULT and stops Vespa cleanly.
D=/home/dev_path/experiments/scale-deep/mem40cap
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $D/driver.log; }
log "(guard) fast stop rule armed (fast_guard.sh)"
while ! grep -q "=== VESPA MEM40CAP DONE" $D/driver.log; do
  why=$(python3 - $D/feed.log $D/cgroup.tsv <<'PY'
import re, sys, time, os, statistics as st
pts = [(int(n), float(t)) for n, t in re.findall(r"fed +(\d+)/\d+ +([\d.]+)s", open(sys.argv[1]).read())]
if not pts or pts[-1][0] >= 1e8 or pts[-1][0] < 10e6: sys.exit()
iv = [(n, (n - pn) / (t - pt), t - pt) for (pn, pt), (n, t) in zip([(0, 0.0)] + pts, pts)]
norm = [x for x in iv if 3e6 < x[0] <= 10e6]
ref = st.median(r for _, r, _ in norm); ref_s = st.median(s for _, _, s in norm)
last = [r for _, r, _ in iv[-2:]]
if all(r < 0.5 * ref for r in last):
    print(f"last 2 1M intervals {[round(r) for r in last]} docs/s < 50% of normal {ref:.0f}/s at {pts[-1][0]}"); sys.exit()
cur = time.time() - os.path.getmtime(sys.argv[1])
io = [float(l.split('\t')[7]) for l in open(sys.argv[2]).read().splitlines()[-4:] if l[0].isdigit()]
if cur > 2 * ref_s and len(io) == 4 and min(io) >= 20:
    print(f"current 1M interval {cur:.0f}s > 2x normal {ref_s:.0f}s with io full avg10 {io} at {pts[-1][0]}")
PY
)
  if [ -n "$why" ]; then
    log "(feed) FAST STOP RULE: $why -> stopping the feeder"
    for p in $(ps -eo pid,args | awk '$0 ~ /vespa_feed_mp.py deep1b100m 24 16$/ && $0 !~ /awk/ {print $1}'); do kill $p; done
    exit 0
  fi
  sleep 30
done
