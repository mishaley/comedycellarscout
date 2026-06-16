#!/usr/bin/env python3
"""
Comedy Cellar Scout — scrapes upcoming MacDougal Street lineups and uses Claude
to score each show on three axes: drop-in likelihood, taste, and crowd-work.

Outputs scout_data.json next to this file. The companion HTML viewer reads it.

Run:
    ANTHROPIC_API_KEY=sk-ant-... python3 scout.py
    # or with --no-ai to just dump the raw lineups
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

API_URL = "https://www.comedycellar.com/lineup/api/"
HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "scout_data.json"
AVAILABILITY_PATH = HERE / "availability.json"

# How many days ahead to scan. Comedy Cellar posts lineups Thursday-ish for the
# coming weekend, so most far-out dates will simply be empty.
DAYS_AHEAD = 60

# Filter: only the MacDougal Street room. Not Village Underground, not Fat
# Black Pussycat (Bar or Lounge), not anything else.
MACDOUGAL_ROOM = "MacDougal Street"

# How many shows to keep per night. The first two; the late show is excluded.
SHOWS_PER_NIGHT = 2

# Claude model used for scoring.
CLAUDE_MODEL = "claude-sonnet-4-5"

# Taste benchmark — comics the user likes. Update freely.
TASTE_BENCHMARK = [
    "Sam Morril",
    "Jeff Arcuri",
    "Mark Normand",
]

# Stained-glass header image. Drop a file at ./header.jpg to use it; otherwise
# the HTML falls back to a CSS placeholder.
HEADER_IMAGE = "header.jpg"


# ---------- Availability ------------------------------------------------------

def load_availability() -> set[str]:
    """Read the user's available nights from availability.json. Returns a set
    of ISO date strings. Missing/broken file -> empty set (scout scans nothing
    and the page just shows an empty calendar)."""
    if not AVAILABILITY_PATH.exists():
        print(f"  ! {AVAILABILITY_PATH.name} not found — no availability set.",
              file=sys.stderr)
        return set()
    try:
        data = json.loads(AVAILABILITY_PATH.read_text())
        return {d for d in data.get("available_dates", []) if d}
    except Exception as e:
        print(f"  ! Could not read availability: {e}", file=sys.stderr)
        return set()


# ---------- Scraping ----------------------------------------------------------

def fetch_date(date_str: str) -> dict:
    """Hit the lineup API for a single date. Returns the parsed JSON."""
    payload = json.dumps({
        "date": date_str,
        "venue": "newyork",
        "type": "lineup",
    })
    body = urllib.parse.urlencode({
        "action": "cc_get_shows",
        "json": payload,
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (comedy-cellar-scout)",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


HEADER_RE = re.compile(
    r'<div class="set-header">.*?data-lineup-id="(?P<id>\d+)".*?<h2>(?P<header>.*?)</h2>',
    re.S,
)
NAME_RE = re.compile(
    r'<span class="name">(?P<name>.*?)</span>(?P<credits>[^<]*)',
    re.S,
)


def strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_shows(html: str) -> list[dict]:
    """Pull (time, room, title, comedians) tuples out of the API HTML blob.

    Strategy: split by '<div class="set-header">' so each chunk is one show.
    Within a chunk, the header parses cleanly, and every '<span class="name">'
    before the next set-header (or end) is a comedian on that bill.
    """
    # Re-attach the delimiter so HEADER_RE matches.
    chunks = re.split(r'(?=<div class="set-header">)', html)
    shows = []
    for chunk in chunks:
        hm = HEADER_RE.match(chunk)
        if not hm:
            continue
        header_text = strip_tags(hm.group("header"))
        # Header looks like:  "6:00 pm show  -  MacDougal Street"
        # Sometimes:           "7:00 pm show  -  An Hour with Jim Norton"
        # Split on the centered dash.
        parts = re.split(r"\s*-\s*", header_text, maxsplit=1)
        time_part = parts[0].strip()
        title_part = parts[1].strip() if len(parts) > 1 else ""

        time_match = re.match(r"(\d{1,2}:\d{2}\s*[ap]m)", time_part, re.I)
        showtime = time_match.group(1).lower() if time_match else time_part.lower()

        # The room name appears as the title for stock shows; named shows
        # (Jim Norton, special events) have their own title but live in a
        # specific room implicitly. We treat the title as the room indicator.
        # Anything that isn't exactly "MacDougal Street" gets filtered out.
        room = title_part

        comedians = []
        for n in NAME_RE.finditer(chunk):
            comedians.append({
                "name": strip_tags(n.group("name")),
                "credits": strip_tags(n.group("credits")),
            })

        shows.append({
            "id": hm.group("id"),
            "time": showtime,
            "room": room,
            "comedians": comedians,
        })
    return shows


def time_to_minutes(t: str) -> int:
    """'9:30 pm' -> 1290. Used for sorting."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap]m)", t.strip(), re.I)
    if not m:
        return 0
    h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mn


# ---------- Claude scoring ----------------------------------------------------

RUBRIC = f"""\
You are a comedy-club scout rating Comedy Cellar (MacDougal Street) shows for a
fan. Score each show on three axes from 1-5 (integers). Be honest and use the
full range — 3 is "fine, not special." Save 5s for real standouts.

1. DROP-IN (1-5): Likelihood of a major celebrity drop-in (Chappelle, Chris
   Rock, Louis CK, Jerry Seinfeld, John Mulaney, Bill Burr, etc.).
   Pattern signals: weekend nights, A-list headliners already on the bill,
   line-ups stacked with NYC heavyweights, "specials" being worked out. Weekday
   early shows are usually low. The first two shows of the night are less
   drop-in-prone than the late show but still get them.

2. TASTE (1-5): How well the bill matches the fan's taste. Benchmark comics:
   {", ".join(TASTE_BENCHMARK)}.
   Look for: sharp joke-writers, punchy club comics, modern New York alt-club
   sensibility, observational and self-aware over preachy or political.

3. CROWD-WORK (1-5): Likelihood the comics on the bill engage with the front
   row. The fan likes to sit front-row and get pulled in. Some comics are
   famously crowd-work-heavy; others stick rigidly to their set. Use what you
   know about each comic's stage habit.

Also write ONE short blurb (1-2 sentences) capturing the bill's vibe and
whether it's worth showing up for. Be specific — name a comic if there's a
real reason to come.

Respond with ONLY a JSON array, one object per show in the same order given:
[{{"drop_in": int, "taste": int, "crowd_work": int, "blurb": "..."}}]
No prose, no markdown fences."""


def score_shows_with_claude(shows: list[dict]) -> list[dict]:
    """Send the whole batch in one call. Returns list of score dicts."""
    import anthropic  # imported lazily so --no-ai works without the SDK

    client = anthropic.Anthropic()
    bill_text = []
    for i, s in enumerate(shows):
        comics = "\n".join(f"  - {c['name']} ({c['credits']})" if c["credits"]
                           else f"  - {c['name']}" for c in s["comedians"])
        bill_text.append(
            f"Show #{i+1}: {s['date']} {s['time']} — {s['room']}\n{comics}"
        )

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=RUBRIC,
        messages=[{
            "role": "user",
            "content": "Score these shows:\n\n" + "\n\n".join(bill_text),
        }],
    )
    text = msg.content[0].text.strip()
    # Strip accidental code fences just in case.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)


def fallback_scores(shows: list[dict]) -> list[dict]:
    """Used when --no-ai is set."""
    return [{
        "drop_in": 0, "taste": 0, "crowd_work": 0,
        "blurb": "AI scoring disabled — raw lineup only.",
    } for _ in shows]


# ---------- Main -------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DAYS_AHEAD,
                   help=f"How many days ahead to scan (default {DAYS_AHEAD}).")
    p.add_argument("--no-ai", action="store_true",
                   help="Skip Claude scoring — just dump raw lineups.")
    p.add_argument("--out", default=str(OUT_PATH),
                   help="Output JSON path.")
    args = p.parse_args()

    today = dt.date.today()
    today_iso = today.isoformat()
    all_shows = []
    scraped_dates = []

    # Load YOUR availability. The scout only scans nights you're free, and the
    # viewer highlights these on the calendar regardless of whether a lineup
    # has been posted yet.
    avail = load_availability()
    # Only scan availability dates that are today-or-future and within the
    # scan window.
    horizon = today + dt.timedelta(days=args.days)
    scan_dates = sorted(
        d for d in avail
        if today_iso <= d <= horizon.isoformat()
    )
    print(f"Availability: {len(avail)} date(s) on file, "
          f"{len(scan_dates)} upcoming to scan.\n")

    for date_str in scan_dates:
        date = dt.date.fromisoformat(date_str)
        try:
            resp = fetch_date(date_str)
        except Exception as e:
            print(f"  ! {date_str}: fetch failed ({e})", file=sys.stderr)
            continue

        html = resp.get("show", {}).get("html", "")
        if "no-shows" in html or not html.strip():
            print(f"  · {date_str}: available night — lineup not yet posted")
            continue

        shows = parse_shows(html)
        # Filter to MacDougal Street, drop late show.
        mac = [s for s in shows if s["room"] == MACDOUGAL_ROOM]
        mac.sort(key=lambda s: time_to_minutes(s["time"]))
        mac = mac[:SHOWS_PER_NIGHT]

        if not mac:
            print(f"  · {date_str}: no MacDougal shows yet")
            continue

        for s in mac:
            s["date"] = date_str
            s["weekday"] = date.strftime("%a").upper()
        all_shows.extend(mac)
        scraped_dates.append(date_str)
        print(f"  ✓ {date_str}: {len(mac)} MacDougal show(s)")

        # Be polite to the API.
        time.sleep(0.4)

    print(f"\nFound {len(all_shows)} shows across {len(scraped_dates)} dates.")

    if not all_shows:
        scores = []
    elif args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.no_ai:
            print("ANTHROPIC_API_KEY not set — running without scoring.",
                  file=sys.stderr)
        scores = fallback_scores(all_shows)
    else:
        print("Scoring with Claude...")
        try:
            scores = score_shows_with_claude(all_shows)
        except Exception as e:
            print(f"  ! Claude scoring failed: {e}", file=sys.stderr)
            scores = fallback_scores(all_shows)

    # Merge scores back onto shows.
    for s, sc in zip(all_shows, scores):
        s.update(sc)

    # Merge with prior run so scored shows persist. The Comedy Cellar API only
    # exposes the next ~3-4 days, so a date scraped today won't be re-scrapable
    # tomorrow until it comes back into the window — but we still want to keep
    # its scored lineup. We:
    #   - keep prior shows whose date is today or future and not re-scraped
    #   - replace any date we successfully re-scraped (fresh data wins)
    #   - drop anything in the past
    fresh_dates = {s["date"] for s in all_shows}
    out_path = Path(args.out)
    merged = list(all_shows)
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
            for s in prior.get("shows", []):
                if s["date"] < today_iso:
                    continue  # past
                if s["date"] in fresh_dates:
                    continue  # already replaced with fresh data
                merged.append(s)
        except Exception as e:
            print(f"  ! Could not merge prior data: {e}", file=sys.stderr)
    merged.sort(key=lambda s: (s["date"], time_to_minutes(s["time"])))

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        # The calendar reflects YOUR availability — every night you're free,
        # whether or not a lineup has posted yet.
        "available_dates": sorted(avail),
        "taste_benchmark": TASTE_BENCHMARK,
        "shows": merged,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out_path} ({len(merged)} shows on "
          f"{len(set(s['date'] for s in merged))} dates; "
          f"{len(avail)} availability dates)")


if __name__ == "__main__":
    main()
