import json,glob,os,sys
K=['memory_allocated_memory','cache_cached_extent_bytes','cache_dirty_extent_bytes','cache_lru_size_bytes','journal_trimmer_dirty_journal_bytes','cache_cached_extents','memory_total_memory']
D=os.path.dirname(os.path.abspath(__file__)); rss=dict(l.split() for l in open(D+'/rss.tsv'))
G=1<<30
print('utc                  rssG  alloc_sum alloc_max  cached  dirty  lru  dirtyjournal  n_cached  total/shard')
for f in sorted(glob.glob(D+'/m_*.json')):
    ts=f.split('m_')[1][:-5]
    try: d=json.load(open(f))['metrics']
    except Exception: continue
    v={k:{} for k in K}
    for m in d:
        for k,x in m.items():
            if k in v: v[k][x.get('shard')]=x['value']
    s=lambda k: sum(v[k].values()); mx=lambda k: max(v[k].values()) if v[k] else 0
    print(f"{ts} {int(rss.get(ts,0))/2**20:5.1f} {s(K[0])/G:9.2f} {mx(K[0])/G:9.2f} {s(K[1])/G:7.2f} {s(K[2])/G:6.2f} {s(K[3])/G:5.2f} {s(K[4])/G:12.2f} {s(K[5]):9d} {mx(K[6])/G:8.1f}")
