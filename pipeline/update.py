#!/usr/bin/env python3
"""Premier League pre-season data pipeline.

Scans ESPN's public club-friendly feed for matches involving 2026/27
Premier League clubs, pulls lineups and goal details for finished games,
and merges everything into data/matches.json (the same schema the
Claude artifact tracker uses).

Designed to run unattended on a schedule (GitHub Actions) or locally:

    python pipeline/update.py               # incremental (last 3 days + next 10)
    python pipeline/update.py --backfill    # full summer, 1 July onwards

The script never deletes matches it isn't re-scanning, degrades
gracefully when ESPN omits a field, and only exits non-zero if the
feed is completely unreachable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/club.friendly"
LONDON = ZoneInfo("Europe/London")

PRESEASON_START = date(2026, 7, 1)
SEASON_OPENER = date(2026, 8, 21)  # 2026/27 Premier League kicks off
DEFAULT_LOOKBACK = 3               # re-scan recent days (late lineups, corrections)
DEFAULT_LOOKAHEAD = 10             # pick up newly announced fixtures

# ESPN display names (normalised) -> the short names used in the tracker.
PL_CLUBS = {
    "arsenal": "Arsenal",
    "astonvilla": "Aston Villa",
    "afcbournemouth": "Bournemouth",
    "bournemouth": "Bournemouth",
    "brentford": "Brentford",
    "brightonhovealbion": "Brighton",
    "brighton": "Brighton",
    "chelsea": "Chelsea",
    "coventrycity": "Coventry City",
    "crystalpalace": "Crystal Palace",
    "everton": "Everton",
    "fulham": "Fulham",
    "hullcity": "Hull City",
    "ipswichtown": "Ipswich Town",
    "leedsunited": "Leeds United",
    "liverpool": "Liverpool",
    "manchestercity": "Man City",
    "mancity": "Man City",
    "manchesterunited": "Man Utd",
    "manutd": "Man Utd",
    "newcastleunited": "Newcastle",
    "newcastle": "Newcastle",
    "nottinghamforest": "Nottingham Forest",
    "sunderland": "Sunderland",
    "tottenhamhotspur": "Tottenham",
    "tottenham": "Tottenham",
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "pl-preseason-tracker (personal hobby project)",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def canonical(name: str | None) -> str:
    """Map an ESPN club name onto the tracker's short name where known."""
    return PL_CLUBS.get(norm(name), (name or "").strip())


def is_pl(name: str | None) -> bool:
    return norm(name) in PL_CLUBS


def get_json(url: str, params: dict | None = None) -> dict | None:
    """GET a JSON document with retries. Returns None on failure."""
    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - log and retry
            wait = 1.5 * (attempt + 1)
            print(f"  ! fetch failed ({exc}); retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    return None


def kickoff_date_london(iso: str) -> str:
    """ESPN gives kickoff in UTC; bucket the match by UK calendar date."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(LONDON).date().isoformat()


def parse_minute(display: str | None, clock_value=None):
    """"67'" -> 67, "90'+3'" -> 93. Falls back to the raw clock in seconds."""
    nums = [int(n) for n in re.findall(r"\d+", display or "")]
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] + nums[1]
    if nums:
        return nums[0]
    try:
        if clock_value:
            return int(float(clock_value)) // 60
    except (TypeError, ValueError):
        pass
    return None


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def build_skeleton(event: dict) -> dict | None:
    """Turn a scoreboard event into a tracker match record (no lineups yet)."""
    try:
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c.get("homeAway") == "home")
        away = next(c for c in competitors if c.get("homeAway") == "away")
    except (KeyError, IndexError, StopIteration):
        return None

    raw_home = (home.get("team") or {}).get("displayName")
    raw_away = (away.get("team") or {}).get("displayName")
    if not (is_pl(raw_home) or is_pl(raw_away)):
        return None
    home_name = canonical(raw_home)
    away_name = canonical(raw_away)

    status = ((comp.get("status") or {}).get("type")) or {}
    finished = status.get("state") == "post" or status.get("completed") is True

    def score(side):
        if not finished:
            return None
        try:
            return int(side.get("score"))
        except (TypeError, ValueError):
            return None

    venue = comp.get("venue") or {}
    note = ((venue.get("address") or {}).get("city") or venue.get("fullName") or "").strip()

    return {
        "id": f"espn-{event['id']}",
        "date": kickoff_date_london(event.get("date", comp.get("date", ""))),
        "note": note[:40],
        "homeTeam": home_name,
        "awayTeam": away_name,
        "homeScore": score(home),
        "awayScore": score(away),
        "homeLineup": [],
        "awayLineup": [],
        "goals": [],
        "_espn": {
            "eventId": str(event["id"]),
            "homeId": str((home.get("team") or {}).get("id", "")),
            "awayId": str((away.get("team") or {}).get("id", "")),
            "finished": finished,
            "detailsFallback": comp.get("details") or [],
        },
    }


def extract_lineups(summary: dict, home_id: str, away_id: str):
    """Starting XIs from the summary rosters. Empty lists when unpublished."""
    home, away = [], []
    rosters = summary.get("rosters") or []
    for index, roster in enumerate(rosters):
        entries = roster.get("roster") or []
        starters = []
        for pos, entry in enumerate(entries):
            if not entry.get("starter"):
                continue
            athlete = entry.get("athlete") or {}
            name = (athlete.get("displayName") or athlete.get("fullName") or "").strip()
            if not name:
                continue
            place = entry.get("formationPlace")
            try:
                order = int(place)
            except (TypeError, ValueError):
                order = 100 + pos
            starters.append((order, name))
        starters = [name for _, name in sorted(starters, key=lambda t: t[0])][:11]

        side = roster.get("homeAway")
        team_id = str((roster.get("team") or {}).get("id", ""))
        if side == "home" or (side is None and team_id == home_id):
            home = starters
        elif side == "away" or (side is None and team_id == away_id):
            away = starters
        elif side is None:  # documented convention: rosters[0]=home, rosters[1]=away
            if index == 0:
                home = starters
            else:
                away = starters
    return home, away


def _goal_from_play(play: dict, home_id: str, away_id: str, event_id: str, i: int) -> dict | None:
    ptype = play.get("type") or {}
    text = (ptype.get("text") or "").lower()

    if play.get("shootout") or ((play.get("period") or {}).get("number") or 0) >= 5:
        return None  # penalty shoot-outs don't count as goals
    scoring = bool(play.get("scoringPlay")) or text.startswith("goal") or "own goal" in text
    if not scoring or "missed" in text or "disallowed" in text or "cancelled" in text:
        return None

    og = bool(play.get("ownGoal")) or "own goal" in text

    names = []
    for p in play.get("participants") or play.get("athletesInvolved") or []:
        athlete = p.get("athlete") if isinstance(p.get("athlete"), dict) else p
        name = ((athlete or {}).get("displayName") or "").strip()
        if name:
            names.append(name)

    team_id = str((play.get("team") or {}).get("id", ""))
    if team_id == home_id:
        side = "home"
    elif team_id == away_id:
        side = "away"
    else:
        return None

    clock = play.get("clock") or {}
    return {
        "id": f"{event_id}-g{i}",
        "team": side,
        "scorer": names[0] if names else "",
        "assist": names[1] if (len(names) > 1 and not og) else "",
        "minute": parse_minute(clock.get("displayValue"), clock.get("value")),
        "og": og,
    }


def extract_goals(plays: list, home_id: str, away_id: str, event_id: str) -> list:
    goals = []
    for i, play in enumerate(plays or []):
        goal = _goal_from_play(play, home_id, away_id, event_id, i)
        if goal:
            goals.append(goal)
    goals.sort(key=lambda g: g["minute"] if g["minute"] is not None else 999)
    return goals


def reconcile_goal_sides(goals: list, home_score, away_score) -> list:
    """ESPN attaches own goals to the scoring player's team; our schema wants
    the side the goal counted FOR. If the per-side totals disagree with the
    final score, flipping the own goals usually fixes it - verify and apply."""
    if home_score is None or away_score is None or not goals:
        return goals

    def counts(items):
        h = sum(1 for g in items if g["team"] == "home")
        return h, len(items) - h

    if counts(goals) == (home_score, away_score):
        return goals
    if not any(g["og"] for g in goals):
        return goals
    flipped = [
        {**g, "team": "away" if g["team"] == "home" else "home"} if g["og"] else g
        for g in goals
    ]
    if counts(flipped) == (home_score, away_score):
        return flipped
    return goals


def enrich(match: dict) -> None:
    """For a finished match, pull the summary for lineups and goal details."""
    meta = match["_espn"]
    summary = get_json(f"{BASE}/summary", params={"event": meta["eventId"]})
    if summary:
        home_xi, away_xi = extract_lineups(summary, meta["homeId"], meta["awayId"])
        match["homeLineup"], match["awayLineup"] = home_xi, away_xi
        goals = extract_goals(
            summary.get("keyEvents"), meta["homeId"], meta["awayId"], meta["eventId"]
        )
    else:
        print(f"  ! no summary for {match['homeTeam']} v {match['awayTeam']}", file=sys.stderr)
        goals = []

    if not goals:  # fall back on the scoreboard's own scoring plays
        goals = extract_goals(
            meta["detailsFallback"], meta["homeId"], meta["awayId"], meta["eventId"]
        )
    match["goals"] = reconcile_goal_sides(goals, match["homeScore"], match["awayScore"])

    expected = (match["homeScore"] or 0) + (match["awayScore"] or 0)
    if match["homeScore"] is not None and len(match["goals"]) != expected:
        print(
            f"  ~ {match['homeTeam']} v {match['awayTeam']}: score says {expected} "
            f"goals, feed lists {len(match['goals'])}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------

def collect(window_start: date, window_end: date) -> tuple[list, int, int]:
    """Scan the scoreboard day by day; return (matches, days_ok, days_failed)."""
    matches, ok, failed = [], 0, 0
    for day in daterange(window_start, window_end):
        data = get_json(f"{BASE}/scoreboard", params={"dates": day.strftime("%Y%m%d")})
        if data is None:
            failed += 1
            continue
        ok += 1
        for event in data.get("events") or []:
            skeleton = build_skeleton(event)
            if skeleton:
                matches.append(skeleton)
        time.sleep(0.25)  # be polite
    return matches, ok, failed


def run(args) -> int:
    out = Path(args.out)
    existing = {"meta": {}, "matches": []}
    if out.exists():
        try:
            existing = json.loads(out.read_text())
        except json.JSONDecodeError:
            print("! existing data file unreadable, starting fresh", file=sys.stderr)
    by_id = {m["id"]: m for m in existing.get("matches", [])}

    today = datetime.now(LONDON).date()
    window_end = min(today + timedelta(days=args.lookahead), SEASON_OPENER - timedelta(days=1))
    if args.backfill or not by_id:
        window_start = PRESEASON_START
    else:
        window_start = max(today - timedelta(days=args.lookback), PRESEASON_START)

    if window_start > window_end:
        print("Pre-season window has passed - nothing to scan.")
        return 0

    print(f"Scanning club friendlies {window_start} .. {window_end}")
    scanned, days_ok, days_failed = collect(window_start, window_end)
    if days_ok == 0:
        print("All scoreboard requests failed - is the feed down?", file=sys.stderr)
        return 1

    new = updated = with_xi = 0
    for match in scanned:
        if match["_espn"]["finished"]:
            enrich(match)
            time.sleep(0.35)
        if match["homeLineup"] and match["awayLineup"]:
            with_xi += 1
        match.pop("_espn", None)
        if match["id"] not in by_id:
            new += 1
        elif by_id[match["id"]] != match:
            updated += 1
        by_id[match["id"]] = match

    merged = sorted(by_id.values(), key=lambda m: (m.get("date") or "9999", m["id"]))
    payload = {
        "meta": {
            "lastUpdated": datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="seconds"),
            "source": "ESPN club friendlies feed",
        },
        "matches": merged,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(
        f"Done: {len(scanned)} PL matches in window ({new} new, {updated} updated, "
        f"{with_xi} with both XIs), {len(merged)} total on file."
        + (f" {days_failed} day(s) failed to fetch." if days_failed else "")
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="rescan from 1 July 2026")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, metavar="DAYS")
    parser.add_argument("--lookahead", type=int, default=DEFAULT_LOOKAHEAD, metavar="DAYS")
    parser.add_argument("--out", default="data/matches.json")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
