#!/usr/bin/env python3
"""
Occupancy aggregator — runs once a day (and on demand).

1. Replays the change files of the last WINDOW days, sample by sample, and
   accumulates for every location: occupied-fraction by hour of day, split
   weekday / weekend, plus how many samples that rests on.
2. Writes agg/occupancy.json.gz  — per location: avg %, weekday[24], weekend[24],
   samples, days, stale flag — and agg/summary.json with the totals.
3. Writes state/latest.csv.gz — the full state as of the last sample, which
   sample.py uses as its starting point tomorrow — and a dated copy in
   state/snapshots/ so any day can be replayed later.

"Occupied fraction" of a location at a sample = occupied / (available + occupied).
Charge points that are out of service are left out of both numerator and
denominator, so a broken charger does not read as "busy".
"""
import csv, glob, gzip, json, os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

WINDOW_DAYS = 42
MIN_SAMPLES = 96 * 5          # at least ~5 days of samples before we publish a number
STALE_MIN = 24 * 60           # operator last_updated older than this at last sample -> flag


def day_list():
    today = datetime.now(timezone.utc).date()
    days = sorted(d for d in os.listdir('data') if os.path.isdir(f'data/{d}'))
    cutoff = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    return [d for d in days if d >= cutoff]


def load_snapshot_before(first_day):
    """Latest dated snapshot strictly before first_day, else empty."""
    snaps = sorted(glob.glob('state/snapshots/*.csv.gz'))
    best = None
    for s in snaps:
        d = os.path.basename(s)[:10]
        if d < first_day:
            best = s
    state = {}
    if best:
        with gzip.open(best, 'rt', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[0] != 'id':
                    state[row[0]] = (int(row[1]), int(row[2]), int(row[3]), int(row[4]) if len(row) > 4 else 0)
    return state


def main():
    days = day_list()
    if not days:
        print('no data yet'); return

    # index of locations (grows as new ids appear)
    idx = {}
    def ix(lid):
        i = idx.get(lid)
        if i is None:
            i = len(idx); idx[lid] = i
        return i

    cap = 120000
    av = np.zeros(cap, np.int32); occ = np.zeros(cap, np.int32); tot = np.zeros(cap, np.int32)
    upd = np.zeros(cap, np.int64); seen = np.zeros(cap, bool)
    occ_sum = np.zeros((cap, 2, 24), np.float32)   # [loc, weekday(0)/weekend(1), hour]
    n_samp = np.zeros((cap, 2, 24), np.int32)
    days_seen = np.zeros((cap, WINDOW_DAYS + 1), bool)

    # seed from snapshot before the window
    for lid, (a, o, t, u) in load_snapshot_before(days[0]).items():
        i = ix(lid); av[i], occ[i], tot[i], upd[i], seen[i] = a, o, t, u, True

    n_runs = 0
    for di, day in enumerate(days):
        for fn in sorted(glob.glob(f'data/{day}/*.csv.gz')):
            ts = None
            with gzip.open(fn, 'rt', encoding='utf-8') as f:
                for row in csv.reader(f):
                    if row[0] == 'ts' or len(row) < 6:
                        continue
                    ts = int(row[0])
                    i = ix(row[1])
                    av[i], occ[i], tot[i], upd[i], seen[i] = int(row[2]), int(row[3]), int(row[4]), int(row[5]), True
            if ts is None:
                # empty change file still marks a covered sample; take time from filename
                ts = int(datetime.strptime(day + os.path.basename(fn)[:4], '%Y-%m-%d%H%M')
                         .replace(tzinfo=timezone.utc).timestamp() // 60)
            t = datetime.fromtimestamp(ts * 60, timezone.utc)
            b = 1 if t.weekday() >= 5 else 0
            h = t.hour
            n = len(idx)
            oper = av[:n] + occ[:n]
            live = seen[:n] & (oper > 0)
            frac = np.where(live, occ[:n] / np.maximum(oper, 1), 0.0)
            occ_sum[:n, b, h] += np.where(live, frac, 0.0)
            n_samp[:n, b, h] += live.astype(np.int32)
            days_seen[:n, di] |= live
            n_runs += 1
    print(f'replayed {n_runs} samples over {len(days)} days, {len(idx)} locations', flush=True)

    # ---- publish
    n = len(idx)
    ids = [None] * n
    for lid, i in idx.items():
        ids[i] = lid
    last_ts = None
    try:
        with open('state/runs.txt') as f:
            last_ts = int(f.readlines()[-1].split(',')[0])
    except Exception:
        pass

    out = {}
    tot_samp = n_samp[:n].sum(axis=(1, 2))
    tot_occ = occ_sum[:n].sum(axis=(1, 2))
    for i in range(n):
        if tot_samp[i] < MIN_SAMPLES:
            continue
        wd = np.where(n_samp[i, 0] > 0, occ_sum[i, 0] / np.maximum(n_samp[i, 0], 1), -1)
        we = np.where(n_samp[i, 1] > 0, occ_sum[i, 1] / np.maximum(n_samp[i, 1], 1), -1)
        rec = {
            'avg': round(float(tot_occ[i] / tot_samp[i]) * 100, 1),
            'wd': [round(float(x) * 100) if x >= 0 else None for x in wd],
            'we': [round(float(x) * 100) if x >= 0 else None for x in we],
            'n': int(tot_samp[i]),
            'days': int(days_seen[i].sum()),
        }
        if last_ts and upd[i] and last_ts - int(upd[i]) > STALE_MIN:
            rec['stale'] = True
        out[ids[i]] = rec

    os.makedirs('agg', exist_ok=True)
    with gzip.open('agg/occupancy.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))

    published = list(out.values())
    summary = {
        'built_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'window_days': len(days), 'first_day': days[0], 'last_day': days[-1],
        'samples_replayed': n_runs, 'locations_seen': n,
        'locations_published': len(published),
        'avg_occupancy_pct': round(float(np.mean([p['avg'] for p in published])), 1) if published else None,
        'stale_flagged': sum(1 for p in published if p.get('stale')),
    }
    with open('agg/summary.json', 'w') as f:
        json.dump(summary, f, indent=1)

    # ---- snapshot of the current state for tomorrow's sampler
    os.makedirs('state/snapshots', exist_ok=True)
    rows = [(ids[i], int(av[i]), int(occ[i]), int(tot[i]), int(upd[i])) for i in range(n) if seen[i]]
    for path in ('state/latest.csv.gz', f'state/snapshots/{days[-1]}.csv.gz'):
        with gzip.open(path, 'wt', encoding='utf-8', newline='') as f:
            w = csv.writer(f); w.writerow(['id', 'av', 'occ', 'tot', 'upd']); w.writerows(rows)
    # keep 70 dated snapshots
    for old in sorted(glob.glob('state/snapshots/*.csv.gz'))[:-70]:
        os.remove(old)
    print('AGG OK', json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
