#!/bin/bash
# After the 110 index completes (user OK 2026-10-29 to delete 94's old volume):
# stop q1b-qdrant cleanly, check space, delete 94's old storage, copy back (6 streams,
# --log-driver none), start 94 Qdrant (load1b compose) -> 76 (waiting) sees green and measures.
X=/home/dev_path/experiments/deep1b-1b; L=$X/logs/q110/q110.log
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L $X/logs/driver.log; }
H94() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
H110() { ssh -o BatchMode=yes labuser@node110 "$@" < /dev/null; }
IMG=qdrant/qdrant:latest
until grep -q "index COMPLETE on 110" $L; do sleep 60; done
H110 "pkill -f 'qdrant1b/[r]earm.sh'; cd ~/qdrant1b && docker compose stop -t 900 2>&1 | tail -1" >> $L
need=$(H110 "docker run --rm --log-driver none -v /home/labuser/qdrant1b/storage:/d:ro --entrypoint du $IMG -sk --exclude=temp_segments /d" | cut -f1)
old=$(H94 "docker run --rm --log-driver none -v /home/ubuntu/qdrant-bench/volumes/qdrant:/s:ro --entrypoint du $IMG -sk /s" | cut -f1)
avail=$(H94 "df -k --output=avail / | tail -1" | tr -dc 0-9)
log "q110: back-copy needs $((need/1048576))G; 94 old $((old/1048576))G + avail $((avail/1048576))G"
[ $(( old + avail - need )) -gt $((40*1048576)) ] || { log "q110: NOT ENOUGH SPACE on 94 for the copy back -> stopped (old volume kept)"; exit 1; }
H94 "docker run --rm --log-driver none -v /home/ubuntu/qdrant-bench/volumes:/v --entrypoint sh $IMG -c 'rm -rf /v/qdrant/* /v/qdrant/.[!.]* 2>/dev/null; ls -la /v/qdrant'" >> $L
log "q110: 94 old Qdrant volume deleted; copying back"
bash $X/scripts/q110_copy_par.sh back 6
bad=$(grep "^stream" $X/logs/q110/copy_back.log | grep -vc "rc=0 0 ")
[ "$bad" = 0 ] || { log "q110: copy back had failing streams: $(grep ^stream $X/logs/q110/copy_back.log | tr '\n' ' ')"; exit 1; }
log "q110: copy back done ($(tail -1 $X/logs/q110/copy_back.log)); starting Qdrant on 94"
H94 "cd ~/qdrant-bench && docker compose -f docker-compose.yml -f docker-compose.load1b.yml up -d 2>&1 | tail -1" >> $L
log "q110: 94 Qdrant started; 76 continues (waits for green, then run_engine)"
