#!/bin/bash
# 100M run of 31_splitmerge.sh: Deep1B 100M prefix (the only 100M float set,
# as in scale-deep), S = 0.5 * mean ||v - centroid|| = 0.4817 (1M strided
# sample), one pool at a time, full-PG scans on 200 queries. Needs the
# cluster built with OSD_DEV_SIZE=768G (256G ran a shard out of space and the
# OSD asserted at load concurrency 128 on 2026-10-13).
export HSCALE=0.4817 SEQ=1 FQL=200 LC=${LC:-64}
D=$HOME/hier/data100m
exec bash $HOME/hier/scripts/31_splitmerge.sh 100m --base $D/deep1b100m_base.fvecs \
  --query $D/deep1b100m_query.fvecs --groundtruth $D/deep1b100m_groundtruth.ivecs
