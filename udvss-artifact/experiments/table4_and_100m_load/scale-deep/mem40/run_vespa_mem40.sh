#!/bin/bash
# 2026-10-17: Vespa only, 100M under a 40 GiB cap. Same as the mem48 paged arm
# (b) except the cap: re-feed 100M uncapped (index was deleted) with the PAGED schema directly
# (same HNSW M30 efc360; VB_DISK_LIMIT 0.97 only so the feed is not disk-blocked - feed-only
# setting), clean stop, restart under 40G, serve check, run_engine full protocol (C ladder
# 20/40/80/160 at ef64, ef10..1024 x3, 120 s) -> results/vespa/deep1b100m_mem40_paged.
# Default (non-paged) is not re-run: it already OOMs 3x at 48G (anon ~45.3G > 40G).
R=/home/dev_path/experiments/scale-deep; D=$R/mem40; L=$D/driver.log; S=$D/state
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
BASE="cd /home/ubuntu/vespa-bench && docker compose -f docker-compose.yml"
CAP="$BASE -f docker-compose.mem40.yml"
PY=/home/dev_path/corun/.venv/bin/python
export VB_PAGED=1 VB_DISK_LIMIT=0.97
tc() { cd /home/dev_path/corun && timeout 150 $PY -c "import sys; sys.path.insert(0,'scripts'); import engine_vespa as E; print(E._total_count())" 2>/dev/null | tail -1; }
cg() { echo /sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' vb-vespa").scope; }
oomk() { H "awk '\$1==\"oom_kill\"{print \$2}' $(cg)/memory.events"; }
wait_cfg() { for i in $(seq 180); do H "curl -sf localhost:19071/state/v1/health" >/dev/null && return 0; sleep 5; done; return 1; }
clean_stop() {
  H "docker exec vb-vespa /opt/vespa/bin/vespa-proton-cmd --local prepareRestart" >> $L 2>&1
  H "docker exec vb-vespa /opt/vespa/bin/vespa-stop-services" >> $L 2>&1
  H "docker stop -t 900 vb-vespa" >> $L 2>&1; }
done_() { log "=== VESPA MEM40 DONE ==="; exit ${1:-0}; }
log "(prep) start Vespa uncapped on empty volume"
H "$BASE up -d" >> $L 2>&1; cg >> $S.cg
wait_cfg || { log "FATAL: config server not up"; done_ 1; }
log "(prep) deploy PAGED schema (disk limit 0.97)"
cd /home/dev_path/corun && $PY - >> $L 2>&1 <<'PY' || { log "FATAL: deploy failed"; done_ 1; }
import sys; sys.path.insert(0, 'scripts'); import engine_vespa as E
E.deploy(96, log=print)
PY
sd=$(H "curl -s localhost:19071/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default/content/schemas/vec.sd")
echo "$sd" | grep -q "attribute: paged" || { log "FATAL: active schema not paged"; done_ 1; }
log "(prep) active schema verified paged; feeding 100M (8 procs x 16 threads)"
cd /home/dev_path/corun
VB_INDEX_TIMEOUT=172800 taskset -c 9-11,20-23,44-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 8 16 >> $D/feed.log 2>&1 \
  || { log "FAILED: feed (see mem40/feed.log), totalCount=$(tc)"; done_ 1; }
n=$(tc); log "(prep) feed done, totalCount=$n"
[ "$n" = 100000000 ] || { log "re-feed all once (idempotent PUTs)"; taskset -c 9-11,20-23,44-47 .venv/bin/python -u $R/engines/vespa_feed_mp.py deep1b100m 8 16 0 >> $D/feed.log 2>&1; n=$(tc); log "totalCount=$n"; }
[ "$n" = 100000000 ] || { log "FATAL: totalCount $n != 100M"; done_ 1; }
log "(prep) clean stop"; clean_stop
H "$CAP up -d" >> $L 2>&1; cg >> $S.cg
log "(b) container up with 40G cap (memory.max=$(H "cat $(cg)/memory.max")), paged schema"
t0=$(date +%s); ok=0
while [ $(( $(date +%s) - t0 )) -lt 7200 ]; do
  n=$(tc); k=$(oomk); [ "$n" = 100000000 ] && ok=$((ok+1)) || ok=0
  echo "$(date -u +%FT%TZ) totalCount=${n:-NA} oom_kill=$k" >> $D/serve_poll.log
  [ $ok -ge 2 ] && break; [ "${k:-0}" -ge 3 ] && break; sleep 30; done
k=$(oomk)
if [ $ok -lt 2 ]; then log "(b) RESULT: paged does NOT serve under 40G (oom_kill=$k, totalCount=${n:-NA})"; clean_stop; done_ 1; fi
log "(b) serving under 40G after $(( ($(date +%s)-t0)/60 )) min, oom_kill=$k; measuring (full protocol)"
F=$R/mem48/FAST_PROTO; [ -e $F ] && mv $F $F.off-mem40
RUN_TAG=mem40_paged bash $R/engines/run_engine.sh vespa deep1b100m --no-load >> $D/run_b.out 2>&1 \
  && log "(b) measurement done" || log "(b) measurement FAILED (see run_b.out)"
[ -e $F.off-mem40 ] && mv $F.off-mem40 $F
log "(b) RESULT: oom_kill=$(oomk) memory.events: $(H "tr '\n' ' ' < $(cg)/memory.events")"
clean_stop; log "vespa stopped cleanly (index kept)"; done_ 0
