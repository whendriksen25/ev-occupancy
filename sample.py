#!/usr/bin/env python3
"""
Occupancy recorder — one sample of the Benelux live charger status.

Runs every 15 minutes in GitHub Actions. Fetches the national live feeds
(NL: NDW/DOT-NL, LU: Chargy), works out per location how many charge points
are available / occupied / out of service, compares with the previous sample
and writes ONLY the changes to data/<UTC date>/<HHMM>.csv.gz.

Previous state is reconstructed from state/latest.csv.gz (daily snapshot,
written by aggregate.py) plus every change file of the current day, so the
job needs no cache and no database. First run ever = every location counts
as a change, which seeds the history.
"""
import csv, glob, gzip, io, json, os, re, sys, time, urllib.request
from datetime import datetime, timezone

UA = {'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'), 'Accept': '*/*'}
NDW = 'https://opendata.ndw.nu/charging_point_locations_ocpi.json.gz'
CHARGY = ('https://my.chargy.lu/b2bev-external-services/resources/kml'
          '?API-KEY=486ac6e4-93b8-4369-9c6a-28f7c4e1a81f')

OCCUPIED = {'CHARGING', 'OCCUPIED', 'RESERVED'}
AVAILABLE = {'AVAILABLE'}


def get(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------ sources
def sample_nl():
    raw = get(NDW)
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    locs = json.loads(raw)
    out = {}
    for l in locs:
        lid = l.get('id')
        evses = l.get('evses') or []
        if not lid or not evses:
            continue
        av = occ = 0
        upd = 0
        for e in evses:
            s = (e.get('status') or '').upper()
            if s in AVAILABLE:
                av += 1
            elif s in OCCUPIED:
                occ += 1
            lu = e.get('last_updated') or ''
            m = re.match(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)', lu)
            if m:
                try:
                    t = int(datetime(*map(int, m.groups()), tzinfo=timezone.utc).timestamp() // 60)
                    upd = max(upd, t)
                except ValueError:
                    pass
        out['NL:' + lid] = (av, occ, len(evses), upd)
    return out


def sample_lu():
    xml = None
    for attempt in range(3):
        try:
            xml = get(CHARGY, timeout=60).decode('utf-8', 'replace')
            if '<kml' in xml:
                break
        except Exception as e:
            print('  LU retry', attempt, e, flush=True)
        time.sleep(15)
    if not xml or '<kml' not in xml:
        print('LU: unavailable this run', flush=True)
        return {}
    import xml.etree.ElementTree as ET
    ns = '{http://www.opengis.net/kml/2.2}'
    root = ET.fromstring(xml)
    out = {}
    now_min = int(time.time() // 60)
    for pm in root.iter(ns + 'Placemark'):
        nm = pm.find(ns + 'name')
        de = pm.find(ns + 'description')
        if nm is None or not (nm.text or '').strip():
            continue
        d = (de.text or '') if de is not None else ''
        m_tot = re.search(r'(\d+)\s*connectors', d)
        m_av = re.search(r'(\d+)\s*available', d)
        tot = int(m_tot.group(1)) if m_tot else 1
        av = int(m_av.group(1)) if m_av else 0
        key = 'LU:' + re.sub(r'\s+', ' ', nm.text.strip())[:80]
        out[key] = (av, max(0, tot - av), tot, now_min)
    return out


# ------------------------------------------------------------------ state
def load_state(today):
    """snapshot + today's change files, applied in order."""
    state = {}
    snap = 'state/latest.csv.gz'
    if os.path.exists(snap):
        with gzip.open(snap, 'rt', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[0] != 'id':
                    state[row[0]] = (int(row[1]), int(row[2]), int(row[3]))
    for fn in sorted(glob.glob(f'data/{today}/*.csv.gz')):
        with gzip.open(fn, 'rt', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 5 and row[0] != 'ts':
                    state[row[1]] = (int(row[2]), int(row[3]), int(row[4]))
    return state


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    stamp = now.strftime('%H%M')
    ts = int(now.timestamp() // 60)          # epoch minutes

    cur = {}
    try:
        cur.update(sample_nl())
        print(f'NL: {sum(1 for k in cur if k.startswith("NL:"))} locations', flush=True)
    except Exception as e:
        print('!! NL sample failed:', e, flush=True)
        sys.exit(1)                          # NL is the whole point; fail loudly
    try:
        cur.update(sample_lu())
        print(f'LU: {sum(1 for k in cur if k.startswith("LU:"))} locations', flush=True)
    except Exception as e:
        print('LU sample failed:', e, flush=True)

    prev = load_state(today)
    changes = []
    for lid, (av, occ, tot, upd) in cur.items():
        p = prev.get(lid)
        if p is None or p != (av, occ, tot):
            changes.append((ts, lid, av, occ, tot, upd))

    os.makedirs(f'data/{today}', exist_ok=True)
    fn = f'data/{today}/{stamp}.csv.gz'
    with gzip.open(fn, 'wt', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['ts', 'id', 'av', 'occ', 'tot', 'upd'])
        w.writerows(changes)

    # run log: one line per sample, so the aggregator knows which minutes were covered
    os.makedirs('state', exist_ok=True)
    with open('state/runs.txt', 'a') as f:
        f.write(f'{ts},{len(cur)},{len(changes)}\n')

    n_occ = sum(1 for v in cur.values() if v[1] > 0)
    summary = {
        'last_sample_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'locations': len(cur), 'changes': len(changes),
        'locations_with_any_occupied': n_occ,
        'connectors': sum(v[2] for v in cur.values()),
        'connectors_available': sum(v[0] for v in cur.values()),
        'connectors_occupied': sum(v[1] for v in cur.values()),
    }
    with open('state/summary.json', 'w') as f:
        json.dump(summary, f)
    print('SAMPLE OK', json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
