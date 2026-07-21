# PL Pre-Season Tracker · hands-off pipeline

Tracks every 2026/27 Premier League club's summer friendlies — results,
fixtures, starting XIs, goalscorers, assists — and keeps itself up to date
with no manual input. A scheduled GitHub Action pulls fresh data from
ESPN's public club-friendly feed twice a day and commits it to this repo;
GitHub Pages serves a tracker site that reads straight from that data.

```
ESPN feed ──▶ pipeline/update.py ──▶ data/matches.json ──▶ index.html
              (GitHub Actions,        (committed to           (GitHub Pages,
               07:00 & 15:00 UK)       the repo)               viewable anywhere)
```

## One-time setup (~10 minutes)

1. **Create a public GitHub repo** (public keeps Actions and Pages free),
   e.g. `pl-preseason`.
2. **Push these files**, keeping the folder structure:

   ```bash
   git init
   git add .
   git commit -m "PL pre-season tracker pipeline"
   git branch -M main
   git remote add origin https://github.com/<your-username>/pl-preseason.git
   git push -u origin main
   ```

   (Or upload the files through the GitHub web UI — the `.github/workflows/`
   folder must survive the upload.)
3. **Run the first backfill.** Repo → *Actions* tab → enable workflows if
   prompted → select **Update pre-season data** → *Run workflow*. The first
   run scans back to 1 July and takes a couple of minutes; it commits
   `data/matches.json` when done.
4. **Turn on Pages.** Repo → *Settings* → *Pages* → Source: *Deploy from a
   branch* → Branch: `main`, folder `/ (root)` → Save.
5. Open `https://<your-username>.github.io/pl-preseason/` — bookmark it on
   your phone. That's it; the schedule takes over from here.

## How the schedule behaves

- Runs at **06:00 and 14:00 UTC** (07:00 / 15:00 UK in summer). The morning
  run catches overnight results from the US tours; the afternoon run catches
  Asia-tour games and newly announced fixtures.
- Discovery is driven by **each Premier League club's own ESPN schedule**
  (team ids resolved at runtime from the PL roster), queried across all
  competitions and both played and upcoming games. Because every match that
  matters has a PL club on one side, this finds them all - including away
  games at lower-league hosts (e.g. Wimbledon v Coventry) and games filed
  under any competition slug, not just `club.friendly`. The friendly
  scoreboard is scanned too as a supplement. The only filters applied are
  "a PL club is involved" and "the date is before the 21 August opener".
- Any overdue fixture the scoreboard never flips to full time gets checked
  **directly against its match summary**, so schedule-only discoveries and
  stale scoreboard caches still finalise with scores, XIs and goals.
- Per-match detail fetches (lineups, scorers) only happen where needed: a
  match is newly finished, its score changed upstream, it finished within
  the last 3 days (lineups often land late), or details are still missing
  (retried for up to 14 days). Everything else is carried forward
  untouched, so runs stay quick and light on the feed.
- Fixtures that disappear from the feed (cancellations, duplicates) are
  removed only after a fully clean scan; played matches are never deleted,
  and any missed runs self-heal on the next scan.
- In-progress matches stay listed as fixtures until full time; the next run
  finalises them.
- The window automatically closes at the league opener (21 August), after
  which runs no-op. Feel free to disable the workflow or archive the repo
  then.

Change the cadence in `.github/workflows/update.yml` (the `cron` lines) and
the scan window or club list at the top of `pipeline/update.py`.

## Running locally instead

No GitHub needed if you'd rather keep it on your own machine:

```bash
pip install -r pipeline/requirements.txt
python pipeline/update.py                 # every run scans the full summer
python pipeline/update.py --backfill      # optional: re-fetch all details
python -m http.server 8000                # then open http://localhost:8000
```

(`index.html` loads `data/matches.json` via fetch, so it needs to be served
over HTTP rather than opened as a bare file.)

## Data notes & honest limitations

- **Sources**: ESPN's public, key-free JSON feeds (the `club.friendly`
  scoreboard plus per-club schedule and match-summary endpoints). They're
  unofficial, so ESPN could change them without notice — the script fails
  soft and logs rather than crashing, and the Actions log will make any
  breakage obvious.
- Friendlies ESPN doesn't carry **anywhere** (typically behind-closed-doors
  training-ground games) can't be discovered from any endpoint. You can add
  those by hand to `data/matches.json` using an `id` that doesn't start
  with `espn-` — the pipeline will preserve them and never prune them.
- **Coverage** of the big pre-season games (tours, Summer Series, televised
  friendlies) is strong; small behind-closed-doors friendlies sometimes
  aren't listed anywhere, including here.
- **Lineups** appear when ESPN publishes them — usually around kick-off.
  A match can therefore show a result before its XIs; the pipeline keeps
  retrying recent matches, and anything still missing details for up to
  two weeks.
- **Own goals** are recorded against the side they counted for and excluded
  from the scorer's personal tally. The script cross-checks goal events
  against the final score and logs any mismatch it can't resolve.
- **Names** follow ESPN's spellings.
- Data volume is tiny and requests are throttled (~1 every 300ms) with an
  identifying User-Agent — a polite, low-impact personal use of the feed.

## Relationship to the Claude artifact tracker

`data/matches.json` uses exactly the same match schema as the Claude
artifact version of this tracker, so the two stay interchangeable — you can
paste pipeline output into a conversation with Claude for analysis, or keep
using the artifact for anything you want to log by hand.
