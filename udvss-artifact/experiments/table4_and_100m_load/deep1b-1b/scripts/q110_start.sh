#!/bin/bash
# After the 94->110 copy: verify all streams rc=0, start Qdrant on 110 (same image 1.19.1,
# cpuset 0-191, 150g, 192 optimizer threads), re-arm the optimizer with 24 jobs, log progress.
X=/home/dev_path/experiments/deep1b-1b; L=$X/logs/q110/q110.log; C=$X/logs/q110/copy_out.log
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L $X/logs/driver.log; }
H110() { ssh -o BatchMode=yes labuser@node110 "$@" < /dev/null; }
until grep -q "ALL DONE out" $C; do sleep 30; done
bad=$(grep "^stream" $C | grep -vc "rc=0 0 ")
[ "$bad" = 0 ] || { log "q110: copy had failing streams: $(grep ^stream $C | tr '\n' ' ')"; exit 1; }
log "q110: copy done ($(tail -1 $C)); 110 storage $(H110 "docker run --rm --log-driver none -v /home/labuser/qdrant1b/storage:/d:ro --entrypoint du qdrant/qdrant:latest -sh /d" | cut -f1)"
H110 "cd ~/qdrant1b && docker compose up -d 2>&1 | tail -1" >> $L
until H110 "curl -sf --max-time 5 localhost:6333/healthz" >/dev/null; do sleep 10; done
for i in $(seq 120); do s=$(H110 "curl -s --max-time 60 localhost:6333/collections/wg_deep1b1b" | python3 -c "import json,sys;r=json.load(sys.stdin)['result'];print(r['status'],r['points_count'],r['indexed_vectors_count'],r['segments_count'])" 2>/dev/null); [ -n "$s" ] && break; sleep 15; done
log "q110: Qdrant up on 110: $s"
log "q110: PATCH 24 jobs: $(H110 "curl -s -X PATCH localhost:6333/collections/wg_deep1b1b -H 'Content-Type: application/json' -d '{\"optimizers_config\":{\"max_optimization_threads\":24}}'")"
echo -e "utc\tstatus\tpoints\tindexed\tsegments\tcpu\tmem" > $X/results/qdrant_1b_index_110.tsv
while :; do
  s=$(H110 "curl -s --max-time 60 localhost:6333/collections/wg_deep1b1b" | python3 -c "import json,sys;r=json.load(sys.stdin)['result'];print(r['status'],r['points_count'],r['indexed_vectors_count'],r['segments_count'])" 2>/dev/null)
  m=$(H110 "docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' q1b-qdrant")
  echo -e "$(date -u +%FT%TZ)\t${s// /\t}\t$m" >> $X/results/qdrant_1b_index_110.tsv
  set -- $s; [ "$1" = green ] && [ -n "$2" ] && [ "$2" = "$3" ] && { log "q110: index COMPLETE on 110: $s"; break; }
  sleep 60
done
