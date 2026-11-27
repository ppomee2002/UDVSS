#!/bin/bash
# 2026-10-28 user: Vespa 100M PAGED with the 40 GiB cap applied DURING THE LOAD too
# (mem40 fed uncapped, capped only for serving). Same schema/HNSW (M30 efc360, paged),
# VB_DISK_LIMIT 0.97. Feed only: Vespa cpuset 0-15,24-39, feeder 24x16 on 16-23,40-47
# (same as the 1B feed). Stop rule ("이거처럼 느려지면 중단"): after 20M, 3 consecutive
# 1M intervals below 20% of the normal rate (median of 5M-20M), or no progress line for
# 60 min, or oom_kill >= 3 -> kill the feeder, record. If 100M loads: cpuset 12-19 and
# run_engine RUN_TAG=mem40cap_paged (FAST_PROTO). The Vespa 1B paged volume is moved
# aside (not deleted) to volumes/vespa.1b-paged-164M. Marker "=== VESPA MEM40CAP DONE ===".
R=/home/dev_path/experiments/scale-deep; D=$R/mem40cap; L=$D/driver.log; F=$D/feed.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
CAP="cd /home/ubuntu/vespa-bench && docker compose -f docker-compose.yml -f docker-compose.mem40.yml"
PY=/home/dev_path/corun/.venv/bin/python
export VB_PAGED=1 VB_DISK_LIMIT=0.97
tc() { curl -s --max-time 90 "http://127.0.0.1:8080/search/?yql=select%20*%20from%20sources%20*%20where%20true&hits=0&timeout=60s" | python3 -c "import json,sys;print(json.load(sys.stdin)['root']['fields']['totalCount'])" 2>/dev/null; }
cg() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' vb-vespa").scope; }
oomk() { H "awk '\$1==\"oom_kill\"{print \$2}' $(cg)/memory.events"; }
done_() { touch $D/SAMPLER_STOP; log "=== VESPA MEM40CAP DONE ==="; exit ${1:-0}; }
H "cd ~/vespa-bench && [ ! -e volumes/vespa.1b-paged-164M ] && mv volumes/vespa volumes/vespa.1b-paged-164M && mkdir volumes/vespa" \
  || { log "FATAL: could not move the 1B volume aside"; done_ 1; }
log "(prep) Vespa 1B paged volume moved to volumes/vespa.1b-paged-164M; starting Vespa under the 40G cap on an empty volume"
H "$CAP up -d" >> $L 2>&1
CG=$(cg); echo $CG > $D/state.cg
log "(prep) memory.max=$(H "cat $CG/memory.max") memory.swap.max=$(H "cat $CG/memory.swap.max")"
for i in $(seq 180); do H "curl -sf localhost:19071/state/v1/health" >/dev/null && break; sleep 5; done
cd /home/dev_path/corun && $PY - >> $L 2>&1 <<'PY' || { log "FATAL: deploy failed"; done_ 1; }
import sys; sys.path.insert(0, 'scripts'); import engine_vespa as E
E.deploy(96, log=print)
PY
H "curl -s localhost:19071/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default/content/schemas/vec.sd" | grep -q "attribute: paged" \
  || { log "FATAL: active schema not paged"; done_ 1; }
log "(prep) paged schema active"
# sampler: cgroup memory + disk reads every 30 s
( echo -e "utc\tcur_gb\tpeak_gb\tanon_gb\tfile_gb\toom_kill\tread_gb\tio_full_avg10" > $D/cgroup.tsv
  while [ ! -e $D/SAMPLER_STOP ]; do
    H "cd $CG 2>/dev/null && echo \$(cat memory.current) \$(cat memory.peak) \
       \$(awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{print a, f}' memory.stat) \
       \$(awk '\$1==\"oom_kill\"{print \$2}' memory.events) \
       \$(awk '{for(i=2;i<=NF;i++) if(\$i~/^rbytes=/){split(\$i,a,\"=\"); s+=a[2]}} END{print s+0}' io.stat) \
       \$(awk '/^full/{split(\$2,a,\"=\"); print a[2]}' io.pressure)" \
    | awk -v t=$(date -u +%FT%TZ) 'NF>=7{g=1073741824; printf "%s\t%.2f\t%.2f\t%.2f\t%.2f\t%s\t%.2f\t%s\n", t,$1/g,$2/g,$3/g,$4/g,$5,$6/g,$7}' >> $D/cgroup.tsv
    sleep 30; done ) &
H "docker update --cpuset-cpus 0-15,24-39 vb-vespa" >> $L 2>&1
log "(feed) 100M under 40G cap (Vespa cpuset 0-15,24-39, feeder 24x16 on 16-23,40-47)"
cd /home/dev_path/corun
VB_INDEX_TIMEOUT=172800 taskset -c 16-23,40-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 24 16 >> $F 2>&1 &
FP=$!
# stop-rule guard
while kill -0 $FP 2>/dev/null; do
  sleep 60
  why=$(python3 - $F <<'PY'
import re, sys, time, os, statistics as st
pts = [(int(n), float(t)) for n, t in re.findall(r"fed +(\d+)/\d+ +([\d.]+)s", open(sys.argv[1]).read())]
iv = [(n, (n - pn) / (t - pt)) for (pn, pt), (n, t) in zip([(0, 0.0)] + pts, pts)]
norm = [r for n, r in iv if 5e6 < n <= 20e6]
if pts and pts[-1][0] >= 20e6 and len(norm) >= 5:
    ref = st.median(norm); last = [r for n, r in iv[-3:]]
    if len(last) == 3 and all(r < 0.2 * ref for r in last):
        print(f"3 consecutive 1M intervals {[round(r) for r in last]} docs/s < 20% of normal {ref:.0f}/s"); sys.exit()
if time.time() - os.path.getmtime(sys.argv[1]) > 3600 and pts and pts[-1][0] < 1e8:
    print(f"no progress line for 60 min (last {pts[-1][0]})")
PY
)
  k=$(oomk); [ "${k:-0}" -ge 3 ] && why="oom_kill=$k"
  if [ -n "$why" ]; then
    log "(feed) STOP RULE: $why -> stopping the feeder"
    for p in $(ps -eo pid,args | awk '$0 ~ /vespa_feed_mp.py deep1b100m 24 16$/ && $0 !~ /awk/ {print $1}'); do kill $p; done
    break
  fi
done
wait $FP; RC=$?
LAST=$(grep -aoE "fed +[0-9]+/" $F | tail -1 | tr -dc 0-9); n=$(tc)
H "docker update --cpuset-cpus 12-19 vb-vespa" >> $L 2>&1
log "(feed) rc=$RC last progress ${LAST:-0}, totalCount=${n:-NA}, oom_kill=$(oomk), memory.events: $(H "tr '\n' ' ' < $CG/memory.events")"
if [ "$RC" = 0 ] && [ "$n" = 100000000 ]; then
  log "(measure) 100M loaded under 40G -> run_engine RUN_TAG=mem40cap_paged (FAST_PROTO, cpuset 12-19)"
  RUN_TAG=mem40cap_paged bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $D/run.out 2>&1 \
    && log "(measure) done" || log "(measure) FAILED (see run.out)"
else
  log "RESULT: Vespa 100M paged does NOT load under 40G at a usable rate: stopped at ${n:-$LAST} docs"
fi
H "docker exec vb-vespa /opt/vespa/bin/vespa-proton-cmd --local prepareRestart; docker exec vb-vespa /opt/vespa/bin/vespa-stop-services; docker stop -t 900 vb-vespa" >> $L 2>&1
log "vespa stopped cleanly (index kept)"; done_ 0
