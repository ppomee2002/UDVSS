#!/bin/bash
# per-shard: seastar allocated, LRU bytes, dirty bytes (GiB)
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh 2>/dev/null
timeout 30 $BIN/ceph -c $CEPH_CONF tell osd.0 dump_metrics 2>/dev/null | python3 -c "
import json,sys,collections
d=json.load(sys.stdin); r=collections.defaultdict(dict)
for m in d.get('metrics',d):
    for k,v in m.items():
        if k in ('memory_allocated_memory','cache_lru_size_bytes','cache_dirty_extent_bytes'):
            r[v.get('shard')][k.split('_')[1]]=round(v['value']/2**30,2)
print(' '.join(f's{s}:{x}' for s,x in sorted(r.items())))"
