#!/usr/bin/env python3
"""Vespa 1B feed record (user 2026-10-28: record + stop). Per-1M progress CSVs from the
feeder's own lines ("fed N/1e9 Ts"), for the default run (60, stopped at ~26M to switch
to paged) and the paged run (61, restarted from doc 0; first ~26.4M were overwrites)."""
import csv, re, datetime as dt
X = '/home/dev_path/experiments/deep1b-1b'
RUNS = [('default', f'{X}/logs/vespa_1b.log', '2026-10-27T05:34:35'),
        ('paged', f'{X}/logs/vespa_1b_paged.log', '2026-10-27T08:10:06')]
for mode, log, t0 in RUNS:
    t0 = dt.datetime.fromisoformat(t0)
    pts = [(int(n), float(t)) for n, t in
           re.findall(r'fed +(\d+)/\d+ +([\d.]+)s', open(log, errors='replace').read())]
    with open(f'{X}/results/vespa_1b/vespa_1b_{mode}_feed.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['mode', 'docs', 'elapsed_s', 'utc', 'cum_docs_per_s',
                    'interval_s', 'interval_docs_per_s'])
        pn, pt = 0, 0.0
        for n, t in pts:
            w.writerow([mode, n, f'{t:.1f}', (t0 + dt.timedelta(seconds=t)).strftime('%FT%TZ'),
                        f'{n / t:.0f}', f'{t - pt:.1f}', f'{(n - pn) / (t - pt):.0f}'])
            pn, pt = n, t
