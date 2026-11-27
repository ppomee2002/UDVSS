#!/usr/bin/env python3
# Every 30 s, drop base.1B.fbin's clean pages from the page cache (POSIX_FADV_DONTNEED)
# so the feeder's 384 GB sequential read does not evict Vespa's paged attribute
# (mem40 lesson: foreign page cache collapsed the paged feed). Read-only file.
import os, time, sys
p = '/data1b/deep1b/base.1B.fbin'
stop = '/home/dev_path/experiments/deep1b-1b/DROP_CACHE_STOP'
fd = os.open(p, os.O_RDONLY)
while not os.path.exists(stop):
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    time.sleep(30)
