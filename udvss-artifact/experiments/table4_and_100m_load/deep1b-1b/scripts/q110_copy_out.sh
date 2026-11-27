#!/bin/bash
# 94 -> 110 Qdrant storage stream (tar, sparse, temp_segments excluded: Qdrant drops them on start)
t0=$(date +%s)
ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "docker run --rm -v /home/ubuntu/qdrant-bench/volumes/qdrant:/s:ro --entrypoint tar qdrant/qdrant:latest -cSf - --exclude=./collections/wg_deep1b1b/0/temp_segments -C /s ." < /dev/null \
 | ssh -o BatchMode=yes labuser@node110 "mkdir -p /home/labuser/qdrant1b/storage && docker run -i --rm -v /home/labuser/qdrant1b/storage:/d --entrypoint tar qdrant/qdrant:latest -xSf - -C /d"
echo "rc=${PIPESTATUS[*]} $(( $(date +%s) - t0 ))s"
