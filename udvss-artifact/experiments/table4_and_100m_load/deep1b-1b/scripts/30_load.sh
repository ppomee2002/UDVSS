#!/bin/bash
# Load all 1e9 rows in $CHUNK-row processes (--base-offset/--base-limit), every
# row to its PG from $PLACEMENT. Resumable: a chunk with a .done marker is
# skipped; a failed chunk stops the run (rows are idempotent puts, so a chunk
# is simply re-run). Rates and OSD memory per chunk -> results/load_chunks.tsv.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
set -e
D=$X/results/load; mkdir -p $D
T=$X/results/load_chunks.tsv
[ -s $T ] || echo -e "offset\trows\tseconds\trate\tosd_rss_g\tmem_avail_g\tend_utc" > $T
for o in $(seq 0 $CHUNK $((N - 1))); do
  [ -e $D/chunk_$o.done ] && continue
  [ -e $X/STOP ] && { log "30: STOP file -> pausing before chunk $o"; exit 3; }
  [ -n "$(osd_pid)" ] || die "30: OSD not running before chunk $o"
  c=$(( N - o < CHUNK ? N - o : CHUNK ))
  L=$D/chunk_$o.log
  bench --operation load --pool $POOL --index $INDEX $ROUTE --pivot-probe-budget $PROBE_BUDGET $DSET \
    --placement-file $PLACEMENT --base-offset $o --base-limit $c --load-concurrency $LOAD_CONC > $L 2>&1 \
    || die "30: chunk $o exited non-zero ($(tail -1 $L | cut -c1-200))"
  f() { grep -aoE "$1=[^ ]+" $L | tail -1 | cut -d= -f2; }
  [ "$(f completed)" = "$c" ] && [ "$(f failures)" = 0 ] || die "30: chunk $o completed=$(f completed) failures=$(f failures)"
  s=$(grep -aE "^LOAD " $L | grep -oE "seconds=[0-9.]+" | cut -d= -f2)
  rss=$(ps -o rss= -p $(osd_pid) | awk '{printf "%.1f", $1/2^20}')
  av=$(awk '/MemAvailable/{printf "%.1f", $2/2^20}' /proc/meminfo)
  echo -e "$o\t$c\t$s\t$(awk -v c=$c -v s=$s 'BEGIN{printf "%.0f", c/s}')\t$rss\t$av\t$(date -u +%FT%TZ)" >> $T
  touch $D/chunk_$o.done
  log "30: chunk $o done ($c rows, $(awk -v c=$c -v s=$s 'BEGIN{printf "%.0f", c/s}')/s, OSD RSS ${rss}G, avail ${av}G)"
done
log "=== 30 complete ==="
