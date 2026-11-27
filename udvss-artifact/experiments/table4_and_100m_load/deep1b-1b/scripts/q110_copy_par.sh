#!/bin/bash
# 94 -> 110 Qdrant storage copy in N parallel tar streams (single stream ran at only 55-80 MB/s).
# 2026-10-29: --log-driver none is REQUIRED: without it the default json-file driver also
# writes the whole tar stream to the container log on the SOURCE host (filled 94 to 100%).
# Usage: q110_copy_par.sh out|back N
DIR=$1; N=${2:-4}; X=/home/dev_path/experiments/deep1b-1b; L=$X/logs/q110
H94() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
H110() { ssh -o BatchMode=yes labuser@node110 "$@"; }
IMG=qdrant/qdrant:latest; SEG=./collections/wg_deep1b1b/0/segments
if [ $DIR = out ]; then SRC=H94; DST=H110; SV=/home/ubuntu/qdrant-bench/volumes/qdrant; DV=/home/labuser/qdrant1b/storage
else SRC=H110; DST=H94; SV=/home/labuser/qdrant1b/storage; DV=/home/ubuntu/qdrant-bench/volumes/qdrant; fi
$SRC "docker run --rm -v $SV:/s:ro --entrypoint sh $IMG -c 'cd /s && ls -d $SEG/*'" < /dev/null > $L/seglist_$DIR.txt
$SRC "docker run --rm -v $SV:/s:ro --entrypoint sh $IMG -c 'cd /s && find . -path $SEG -prune -o -path ./collections/wg_deep1b1b/0/temp_segments -prune -o \( -type f -o -type l \) -print'" < /dev/null > $L/rest_$DIR.txt
$DST "mkdir -p $DV" < /dev/null
t0=$(date +%s)
for i in $(seq 0 $((N-1))); do
  { awk -v i=$i -v n=$N 'NR%n==i' $L/seglist_$DIR.txt; [ $i = 0 ] && cat $L/rest_$DIR.txt; } > $L/list_${DIR}_$i.txt
  ( $SRC "cat > /tmp/qlist_$i.txt; docker run --rm --log-driver none -v $SV:/s:ro -v /tmp/qlist_$i.txt:/l.txt:ro --entrypoint tar $IMG -cSf - -C /s -T /l.txt" < $L/list_${DIR}_$i.txt \
    | $DST "docker run -i --rm --log-driver none -v $DV:/d --entrypoint tar $IMG -xSf - -C /d" ; echo "stream $i rc=${PIPESTATUS[*]} $(( $(date +%s) - t0 ))s" >> $L/copy_$DIR.log ) &
done
wait
echo "ALL DONE $DIR $(( $(date +%s) - t0 ))s" >> $L/copy_$DIR.log
