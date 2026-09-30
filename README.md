# ev-occupancy — Benelux charger occupancy recorder

Open data only. Every 15 minutes a GitHub Actions job samples the national
live charging feeds (Netherlands: NDW / DOT-NL; Luxembourg: Chargy), and
stores what changed since the previous sample. Once a day the change logs are
replayed into an hour-of-day occupancy profile per charging location.

Layout
- `data/YYYY-MM-DD/HHMM.csv.gz` — status changes at that sample
  (`ts` = epoch minutes UTC, `id`, `av` available, `occ` occupied, `tot`
  charge points, `upd` = operator's own last_updated as epoch minutes).
- `state/latest.csv.gz` — full state after the last aggregation;
  `state/snapshots/` dated copies; `state/runs.txt` one line per sample.
- `agg/occupancy.json.gz` — per location: `avg` %, `wd`[24], `we`[24],
  `n` samples, `days`, `stale` flag. `agg/summary.json` — totals.

Occupied fraction = occupied / (available + occupied). Out-of-service
points are excluded from both sides so a broken charger never reads as busy.

Sources: NDW / DOT-NL open data (opendata.ndw.nu) · Chargy (data.public.lu, CC0).
Belgium is not included: no live feed is published there.

Part of the Benelux EV Charging Map — https://benelux-ev-map.vercel.app
