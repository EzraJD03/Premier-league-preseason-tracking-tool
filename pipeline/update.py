#!/usr/bin/env python3
"""Premier League pre-season data pipeline.

Scans ESPN's public club-friendly feed for matches involving 2026/27
Premier League clubs, pulls lineups and goal details for finished games,
and merges everything into data/matches.json (the same schema the
Claude artifact tracker uses).

Designed to run unattended on a schedule (GitHub Actions) or locally.
Every run scans the entire summer window (1 July - 20 August), so the
complete announced fixture list is always on file and results fill in as
games are played. Per-match detail fetches (lineups, scorers) happen only
where something is new, changed, recent, or still missing, so runs stay
light. Fixtures that vanish from the feed are pruned only after a fully
clean scan; played matches are never deleted, and missed runs self-heal.

    python pipeline/update.py               # normal run
    python pipeline/update.py --backfill    # force re-fetch of all details

The script degrades gracefully when ESPN omits a field and only exits
non-zero if the feed is completely unreachable.
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

SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
WEB = "https://site.web.api.espn.com/apis/site/v2/sports/soccer"
BASE = f"{SITE}/club.friendly"
LONDON = ZoneInfo("Europe/London")

PRESEASON_START = date(2026, 7, 1)
SEASON_OPENER = date(2026, 8, 21)  # 2026/27 Premier League kicks off
DEFAULT_LOOKBACK = 3               # always refresh details for matches this recent
STRAGGLER_DAYS = 14                # keep retrying missing lineups/goals this long

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


def coerce_score(value):
    """Scores arrive as "2", 2, or {"value": 2.0, "displayValue": "2"}."""
    if isinstance(value, dict):
        value = value.get("displayValue", value.get("value"))
    try:
        return int(float(value))
    except (TypeError, ValueError):
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
    league = ((event.get("league") or {}).get("slug")) or "club.friendly"

    def score(side):
        return coerce_score(side.get("score")) if finished else None

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
        "league": league,
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
    league = match.get("league") or "club.friendly"
    summary = get_json(f"{SITE}/{league}/summary", params={"event": meta["eventId"]})
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

def collect(window_start: date, window_end: date) -> tuple[list, int]:
    """Supplementary: scan the friendly scoreboard day by day. Catches games
    where BOTH sides are non-PL... which never applies here, but also picks up
    neutral-site events indexed by date before they appear on club schedules."""
    matches, failed = [], 0
    for day in daterange(window_start, window_end):
        data = get_json(f"{BASE}/scoreboard", params={"dates": day.strftime("%Y%m%d")})
        if data is None:
            failed += 1
            continue
        for event in data.get("events") or []:
            skeleton = build_skeleton(event)
            if skeleton:
                matches.append(skeleton)
        time.sleep(0.25)  # be polite
    return matches, failed


def resolve_team_ids() -> dict:
    """Map tracker club names -> ESPN team ids via the Premier League roster."""
    data = get_json(f"{SITE}/eng.1/teams")
    ids: dict[str, str] = {}
    try:
        for entry in data["sports"][0]["leagues"][0]["teams"]:
            team = entry.get("team") or {}
            if is_pl(team.get("displayName")):
                ids[canonical(team["displayName"])] = str(team["id"])
    except (KeyError, IndexError, TypeError):
        pass
    for club in sorted(set(PL_CLUBS.values())):
        if club not in ids:
            print(f"  ! could not resolve an ESPN id for {club}", file=sys.stderr)
    return ids


# ESPN season type for pre-season club friendlies (see event.seasonType.type
# in the schedule feed). Passed explicitly so a club's schedule returns its
# summer games rather than defaulting to the league season.
FRIENDLY_SEASONTYPE = 13818


def _schedule_events(team_id: str) -> list | None:
    """Every summer event for one club, across ALL competitions and both
    played and upcoming. Tries the friendly-season schedule first, then falls
    back to the default schedule, merging whatever each returns."""
    seen, events, any_ok = set(), [], False
    for params in (
        {"seasontype": FRIENDLY_SEASONTYPE},  # 2026 Club Friendly season
        {"fixture": "true"},                  # upcoming, default season
        {},                                   # default season, played + upcoming
    ):
        data = get_json(f"{WEB}/all/teams/{team_id}/schedule", params=params)
        if data is None:
            continue
        any_ok = True
        for event in data.get("events") or []:
            eid = str(event.get("id"))
            if eid and eid not in seen:
                seen.add(eid)
                events.append(event)
    return events if any_ok else None


def collect_from_schedules(team_ids: dict, window_start: date,
                           window_end: date) -> tuple[list, int]:
    """Primary discovery: each PL club's OWN schedule. Because every match we
    care about has a PL club on one side, this finds them all - including away
    games at lower-league hosts (Wimbledon v Coventry) and games under any
    competition slug. No league filtering: the only tests are 'a PL club is
    involved' (guaranteed here) and 'date within the pre-season window'."""
    matches, failed = [], 0
    lo, hi = window_start.isoformat(), window_end.isoformat()
    for club in sorted(team_ids):
        events = _schedule_events(team_ids[club])
        if events is None:
            failed += 1
            continue
        for event in events:
            skeleton = build_skeleton(event)
            if skeleton and lo <= skeleton["date"] <= hi:
                matches.append(skeleton)
        time.sleep(0.25)
    return matches, failed


def needs_details(skel: dict, stored: dict | None, today: date,
                  lookback: int, backfill: bool) -> bool:
    """Decide whether a finished match is worth a summary fetch this run."""
    if not skel["_espn"]["finished"]:
        return False
    if backfill or stored is None or stored.get("homeScore") is None:
        return True  # forced, brand new, or newly finished
    if (stored.get("homeScore"), stored.get("awayScore")) != (
        skel["homeScore"], skel["awayScore"]
    ):
        return True  # upstream score correction
    try:
        age = (today - date.fromisoformat(skel["date"])).days
    except ValueError:
        return True
    if age <= lookback:
        return True  # fresh: lineups and details often land late
    missing_xi = not (stored.get("homeLineup") and stored.get("awayLineup"))
    expected = (skel["homeScore"] or 0) + (skel["awayScore"] or 0)
    missing_goals = expected > 0 and len(stored.get("goals") or []) < expected
    return (missing_xi or missing_goals) and age <= STRAGGLER_DAYS


def finalize_from_summary(match: dict) -> bool:
    """For a stored fixture whose date has passed but which the (sometimes
    stale or incomplete) scoreboard never marked finished: ask the match
    summary directly and, if it's full time, take everything from there."""
    league = match.get("league") or "club.friendly"
    event_id = match["id"][len("espn-"):]
    summary = get_json(f"{SITE}/{league}/summary", params={"event": event_id})
    if not summary:
        return False
    try:
        comp = (summary.get("header") or {}).get("competitions", [{}])[0]
        state = (((comp.get("status") or {}).get("type")) or {}).get("state")
        if state != "post":
            return False
        competitors = comp.get("competitors") or []
        home = next(c for c in competitors if c.get("homeAway") == "home")
        away = next(c for c in competitors if c.get("homeAway") == "away")
    except (StopIteration, IndexError, AttributeError):
        return False
    home_id = str(home.get("id") or (home.get("team") or {}).get("id", ""))
    away_id = str(away.get("id") or (away.get("team") or {}).get("id", ""))
    match["homeScore"] = coerce_score(home.get("score"))
    match["awayScore"] = coerce_score(away.get("score"))
    xi = extract_lineups(summary, home_id, away_id)
    match["homeLineup"], match["awayLineup"] = xi
    goals = extract_goals(summary.get("keyEvents"), home_id, away_id, event_id)
    match["goals"] = reconcile_goal_sides(goals, match["homeScore"], match["awayScore"])
    return True


def run(args) -> int:
    out = Path(args.out)
    existing = {"meta": {}, "matches": []}
    if out.exists():
        try:
            existing = json.loads(out.read_text())
        except json.JSONDecodeError:
            print("! existing data file unreadable, starting fresh", file=sys.stderr)
    by_id = {m["id"]: m for m in existing.get("matches", [])}

    override = getattr(args, "today", None)
    today = date.fromisoformat(override) if override else datetime.now(LONDON).date()
    window_start = PRESEASON_START
    window_end = SEASON_OPENER - timedelta(days=1)
    if today > window_end + timedelta(days=7):
        print("Pre-season window has passed - nothing to scan.")
        return 0

    print(f"Scanning club friendlies {window_start} .. {window_end}")
    scanned, days_failed = collect(window_start, window_end)
    total_days = (window_end - window_start).days + 1
    if days_failed >= total_days:
        print("All scoreboard requests failed - is the feed down?", file=sys.stderr)
        return 1

    team_ids = resolve_team_ids()
    from_schedules, sched_failed = collect_from_schedules(
        team_ids, window_start, window_end
    )

    # de-duplicate across sources; the scoreboard copy wins when both have it
    scanned = list({m["id"]: m for m in from_schedules + scanned}.values())

    seen_ids, finished_ids = set(), set()
    new = updated = fetched = carried = with_xi = 0
    for match in scanned:
        seen_ids.add(match["id"])
        if match["_espn"]["finished"]:
            finished_ids.add(match["id"])
        stored = by_id.get(match["id"])

        # never let a feed glitch downgrade a stored result back to a fixture
        if (
            stored is not None
            and stored.get("homeScore") is not None
            and not match["_espn"]["finished"]
        ):
            carried += 1
            continue

        if needs_details(match, stored, today, args.lookback, args.backfill):
            enrich(match)
            fetched += 1
            time.sleep(0.35)
        elif stored is not None:  # keep previously gathered detail
            match["homeLineup"] = stored.get("homeLineup") or []
            match["awayLineup"] = stored.get("awayLineup") or []
            match["goals"] = stored.get("goals") or []
            if not match["note"]:
                match["note"] = stored.get("note", "")
            carried += 1

        if match["homeLineup"] and match["awayLineup"]:
            with_xi += 1
        match.pop("_espn", None)
        if stored is None:
            new += 1
        elif stored != match:
            updated += 1
        by_id[match["id"]] = match

    # overdue fixtures the scoreboard never flipped to full time: ask the
    # match summary directly (also finalizes schedule-only discoveries)
    finalized = 0
    for mid, match in list(by_id.items()):
        if not mid.startswith("espn-") or mid in finished_ids:
            continue
        if match.get("homeScore") is not None or match.get("awayScore") is not None:
            continue
        try:
            age = (today - date.fromisoformat(match.get("date") or "")).days
        except ValueError:
            continue
        if 0 < age <= STRAGGLER_DAYS:
            if finalize_from_summary(match):
                finalized += 1
            time.sleep(0.35)

    # prune fixtures that vanished from every source - only after a fully
    # clean run, only unplayed matches, never hand-added (non-ESPN) records
    pruned = 0
    prune_safe = (
        days_failed == 0
        and sched_failed == 0
        and len(team_ids) == len(set(PL_CLUBS.values()))
    )
    if prune_safe:
        for mid in list(by_id):
            m = by_id[mid]
            if not mid.startswith("espn-") or mid in seen_ids:
                continue
            if m.get("homeScore") is not None or m.get("awayScore") is not None:
                continue
            if window_start.isoformat() <= (m.get("date") or "") <= window_end.isoformat():
                print(
                    f"  - removing vanished fixture: {m.get('homeTeam')} v "
                    f"{m.get('awayTeam')} ({m.get('date')})",
                    file=sys.stderr,
                )
                del by_id[mid]
                pruned += 1

    merged = sorted(by_id.values(), key=lambda m: (m.get("date") or "9999", m["id"]))
    payload = {
        "meta": {
            "lastUpdated": datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="seconds"),
            "source": "ESPN club friendlies + club schedules",
        },
        "matches": merged,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(
        f"Done: {len(scanned)} PL matches in feed "
        f"({len(from_schedules)} via club schedules; {new} new, {updated} updated, "
        f"{fetched} detail fetches, {carried} carried forward, "
        f"{finalized} finalized from summaries, {with_xi} with both XIs)"
        + (f", {pruned} vanished fixture(s) removed" if pruned else "")
        + f", {len(merged)} total on file."
        + (f" {days_failed} scoreboard day(s) failed." if days_failed else "")
        + (f" {sched_failed} schedule fetch(es) failed." if sched_failed else "")
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill", action="store_true",
        help="force a fresh detail fetch for every finished match",
    )
    parser.add_argument(
        "--lookback", type=int, default=DEFAULT_LOOKBACK, metavar="DAYS",
        help="always refresh details for matches this recent",
    )
    parser.add_argument("--out", default="data/matches.json")
    parser.add_argument("--today", default=None, help=argparse.SUPPRESS)  # tests
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
