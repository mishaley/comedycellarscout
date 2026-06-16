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
# Reservation availability: the booking widget GETs this page for a short-lived
# anti-abuse token, then POSTs {date} to the getShows endpoint.
RESV_PAGE_URL = "https://www.comedycellar.com/reservations-newyork/"
RESV_API_URL = "https://www.comedycellar.com/reservations/api/getShows"
HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "scout_data.json"
CONFIG_PATH = HERE / "config.json"
LINKS_PATH = HERE / "comic_links.json"

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
    "Tom Cotter",
]

# Stained-glass header image. Drop a file at ./header.jpg to use it; otherwise
# the HTML falls back to a CSS placeholder.
HEADER_IMAGE = "header.jpg"


# ---------- Config (availability + taste) ------------------------------------

def load_config() -> tuple[set[str], list[str]]:
    """Read config.json -> (available_dates set, taste_benchmark list).

    config.json is the single source of truth, editable from the web app or by
    hand. Missing/broken -> empty availability and the default taste list.
    """
    if not CONFIG_PATH.exists():
        print(f"  ! {CONFIG_PATH.name} not found — no availability set.",
              file=sys.stderr)
        return set(), list(TASTE_BENCHMARK)
    try:
        data = json.loads(CONFIG_PATH.read_text())
        dates = {d for d in data.get("available_dates", []) if d}
        taste = [t for t in data.get("taste_benchmark", []) if t] \
            or list(TASTE_BENCHMARK)
        return dates, taste
    except Exception as e:
        print(f"  ! Could not read config: {e}", file=sys.stderr)
        return set(), list(TASTE_BENCHMARK)


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


def hms_to_minutes(t: str) -> int:
    """'19:30:00' -> 1170."""
    m = re.match(r"(\d{1,2}):(\d{2})", t.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else -1


# ---------- Reservation availability (sold-out check) ------------------------

def get_reservation_token() -> tuple[str, str]:
    """Fetch the booking page and pull its short-lived (IP-bound) token.

    Returns (cca, created) for the X-Code-Localize / X-Page-Creation headers
    the getShows endpoint requires. Raises on failure.
    """
    req = urllib.request.Request(
        RESV_PAGE_URL, headers={"User-Agent": "Mozilla/5.0 (comedy-cellar-scout)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", "ignore")
    cca = re.search(r'"cca":"([^"]+)"', html)
    created = re.search(r'"created":(\d+)', html)
    if not cca or not created:
        raise RuntimeError("could not parse reservation token")
    return cca.group(1), created.group(1)


def fetch_availability(date_str: str, cca: str, created: str) -> dict[int, dict]:
    """Return {minutes-since-midnight: {sold_out, seats_left}} for MacDougal
    St. shows on `date_str`, per the live reservation system."""
    body = json.dumps({"date": date_str}).encode()
    req = urllib.request.Request(
        RESV_API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (comedy-cellar-scout)",
            "X-Code-Localize": cca,
            "X-Page-Creation": str(created),
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    out: dict[int, dict] = {}
    for s in data.get("data", {}).get("showInfo", {}).get("shows", []):
        # roomId 1 == "MacDougal St."; match by description to be safe.
        if "MacDougal" not in (s.get("description") or ""):
            continue
        seats_left = s.get("max", 0) - s.get("totalGuests", 0)
        # The widget's own rule: explicit flag OR no seats remaining.
        sold_out = bool(s.get("soldout")) or seats_left < 1
        out[hms_to_minutes(s.get("time", ""))] = {
            "sold_out": sold_out,
            "seats_left": max(seats_left, 0),
        }
    return out


def annotate_availability(shows: list[dict]) -> None:
    """Tag each show with sold_out / seats_left from the reservation system.
    Best-effort: on any failure, shows are simply left unannotated."""
    dates = sorted({s["date"] for s in shows})
    if not dates:
        return
    try:
        cca, created = get_reservation_token()
    except Exception as e:
        print(f"  ! availability check skipped (token: {e})", file=sys.stderr)
        return
    by_date: dict[str, dict[int, dict]] = {}
    for d in dates:
        try:
            by_date[d] = fetch_availability(d, cca, created)
            time.sleep(0.3)
        except Exception as e:
            print(f"  ! availability check failed for {d}: {e}", file=sys.stderr)
    for s in shows:
        avail = by_date.get(s["date"], {}).get(time_to_minutes(s["time"]))
        if avail:
            s["sold_out"] = avail["sold_out"]
            s["seats_left"] = avail["seats_left"]
    n_sold = sum(1 for s in shows if s.get("sold_out"))
    print(f"Availability: checked {len(shows)} show(s), {n_sold} sold out.")


# ---------- Claude scoring ----------------------------------------------------

def build_rubric(taste: list[str]) -> str:
    return f"""\
You are a comedy-club scout rating Comedy Cellar (MacDougal Street) shows for a
fan. Score each show on three axes from 1-5 (integers). Be honest and use the
full range — 3 is "fine, not special." Save 5s for real standouts.

1. DROP-IN (1-5): Likelihood of a major celebrity drop-in (Chappelle, Chris
   Rock, Louis CK, Jerry Seinfeld, John Mulaney, Bill Burr, etc.). Big names use
   the Cellar to TEST NEW MATERIAL, so they favor quieter weeknights over packed
   weekends — they won't bump a paying Friday/Saturday crowd, and they want a
   low-pressure, phone-free room. Historical pattern (a 10-year analysis of
   Cellar lineups, plus the documented all-star nights):
     - Mon-Wed: HIGHEST. Early week is prime. The legendary all-star nights
       (the 2013 Rock -> Chappelle set, the 2017 Seinfeld/Rock/Chappelle/Schumer
       night) both landed on WEDNESDAYS.
     - Sunday: good — quiet, low-pressure room.
     - Thursday: LOWEST — many regulars leave town to headline weekend gigs.
     - Fri/Sat: only moderate. Bigger crowds and the occasional marquee surprise,
       but regulars are least likely to bump a sold-out early show.
   Other signals that raise the score: an A-list headliner already on the bill,
   lineups stacked with NYC heavyweights, comics visibly working out a special.
   Seasonal nudge: spring (especially May) skews higher; Nov-Dec lower (comics
   decamp to LA). The first two shows are slightly less drop-in-prone than the
   late show but still get them.

2. TASTE (1-5): How well the bill matches the fan's taste. The fan's CURRENT
   benchmark comics are: {", ".join(taste)}.
   This list is edited over time, so DERIVE the fan's sensibility from these
   specific comics rather than any fixed notion of taste. Figure out what they
   have in common — joke-writing density, delivery, club vs. alt sensibility,
   crowd-work tendency, subject matter, tone — and score how closely each bill's
   comics share that profile. If the list shifts toward a different style, follow
   it. A bill featuring a benchmark comic (or a close stylistic sibling) scores
   high; a clear stylistic mismatch scores low.

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


def score_shows_with_claude(shows: list[dict], taste: list[str]) -> list[dict]:
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
        system=build_rubric(taste),
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


def _lineup_sig(s: dict) -> tuple:
    """Identity of a show's bill for the score cache: date, time, comic names.
    If any of these differ from a prior scan, the bill is re-scored."""
    return (s["date"], s["time"],
            tuple(c.get("name", "") for c in s.get("comedians", [])))


# ---------- Comic web-property links -----------------------------------------
# Each comic chip in the viewer links to that comic's best web property, in
# priority order: Instagram > YouTube > TikTok > personal website. We resolve
# links with Claude's web-search tool so they're identity-verified (the right
# NYC Comedy Cellar performer, not a same-named stranger), and cache them in
# comic_links.json. Any new comic — added to the taste list or appearing in a
# scraped lineup — gets resolved automatically on the next run.

LINK_SYSTEM_PROMPT = """\
You find the single best official web property for one specific working stand-up
comedian who performs at New York's Comedy Cellar (117 MacDougal Street). Because
this person is a confirmed Comedy Cellar performer, an account almost always exists
— your job is to find it and return the best one. Use web search to look for their
comedy presence: stand-up clips, a comedy bio, tour dates, Comedy Cellar / NYC
comedy mentions, lineup listings.

Pick ONE URL in strict priority order — use the highest that exists and shows
genuine comedy activity for this comedian:
  1. Instagram
  2. YouTube channel
  3. TikTok
  4. personal website

Identity: names sometimes collide. Choose the candidate whose content clearly shows
THIS person doing stand-up comedy. Prefer the account with comedy signal. Only
return null if EVERY candidate you find plainly belongs to an unrelated person (a
different profession or city with zero comedy connection) — do not return null
merely because verification is imperfect. When two comedy candidates exist, pick the
more active / higher-following one.

Respond with ONLY a compact JSON object and nothing else:
  {"url": "https://..."}    the best match you found, or
  {"url": null}             only if no candidate has any comedy connection."""


def load_comic_links() -> tuple[dict, str]:
    """Read comic_links.json -> (name->url dict, _comment string)."""
    if LINKS_PATH.exists():
        try:
            data = json.loads(LINKS_PATH.read_text())
            return dict(data.get("links", {})), data.get("_comment", "")
        except Exception as e:
            print(f"  ! Could not read comic links: {e}", file=sys.stderr)
    return {}, ""


def _resolve_one_link(name: str, client) -> str | None:
    """One Claude call (with web search) to find a verified best URL."""
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=LINK_SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": 5}],
        messages=[{
            "role": "user",
            "content": f"Comedian: {name} — performs stand-up at the Comedy "
                       f"Cellar in New York City.",
        }],
    )
    # The model emits search/tool blocks; the final text block holds the JSON.
    text = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text = block.text
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        url = json.loads(text).get("url")
    except Exception:
        m = re.search(r'https?://[^\s"\']+', text)
        url = m.group(0) if m else None
    if url and isinstance(url, str) and url.startswith("http"):
        return url.strip()
    return None


def resolve_comic_links(names: set[str], links: dict) -> dict:
    """Fill links for any names not already cached. Returns the updated dict."""
    missing = sorted({n for n in names if n and n not in links})
    if not missing:
        return links
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"  ! {len(missing)} comic(s) need links but ANTHROPIC_API_KEY "
              f"is unset — skipping link lookup.", file=sys.stderr)
        return links

    import anthropic  # lazy import so --no-ai stays dependency-free
    client = anthropic.Anthropic()
    print(f"Resolving web links for {len(missing)} new comic(s)...")
    for name in missing:
        try:
            url = _resolve_one_link(name, client)
        except Exception as e:
            print(f"  ! link lookup failed for {name}: {e}", file=sys.stderr)
            continue
        if url:
            links[name] = url
            print(f"  + {name} -> {url}")
        else:
            print(f"  · no verified link found for {name} (will retry next run)")
        time.sleep(0.3)
    return links


def write_comic_links(links: dict, comment: str) -> None:
    payload = {
        "_comment": comment or (
            "Verified best web property per comic, priority Instagram > YouTube "
            "> TikTok > website. Auto-resolved and identity-checked by scout.py "
            "via web search. Names without a confident match are retried each run."
        ),
        "links": dict(sorted(links.items())),
    }
    LINKS_PATH.write_text(json.dumps(payload, indent=2) + "\n")


# ---------- WhatsApp alerts --------------------------------------------------
# When a standout (avg of the 3 scores >= threshold) posts while still bookable,
# ping the user once on WhatsApp via CallMeBot. De-duped by show id so the
# every-30-min evening scans don't spam the same show.
ALERTED_PATH = HERE / "alerted.json"
STANDOUT_THRESHOLD = 4.0  # selective enough to differentiate; keep in sync with the HTML viewer
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


# What makes a great Cellar night, in priority order: crowd work first, an
# A-list drop-in next, taste as a guide. Weights sum to 1 so the score stays
# on the 1-5 scale. Keep in sync with SCORE_WEIGHTS in the HTML viewer.
SCORE_WEIGHTS = {"crowd_work": 0.5, "drop_in": 0.3, "taste": 0.2}
# Crowd work is the top priority, so *exceptional* crowd work alone (a perfect
# 5) qualifies a night even if the weighted total just misses. A merely-good 4
# no longer auto-qualifies. Keep in sync with the viewer.
CROWD_FLOOR = 5


def weighted_score(s: dict) -> float:
    return sum(s.get(k, 0) * w for k, w in SCORE_WEIGHTS.items())


def is_standout(s: dict) -> bool:
    return (weighted_score(s) >= STANDOUT_THRESHOLD
            or s.get("crowd_work", 0) >= CROWD_FLOOR)


def load_alerted() -> set[str]:
    try:
        return set(json.loads(ALERTED_PATH.read_text()).get("alerted_ids", []))
    except Exception:
        return set()


def save_alerted(ids: set[str]) -> None:
    ALERTED_PATH.write_text(
        json.dumps({"alerted_ids": sorted(ids)}, indent=2) + "\n"
    )


def _alert_text(s: dict) -> str:
    names = [c.get("name", "") for c in s.get("comedians", []) if c.get("name")]
    if len(names) > 4:
        who = ", ".join(names[:4]) + f", +{len(names) - 4} more"
    else:
        who = ", ".join(names)
    when = f"{s.get('weekday', '')} {s.get('date', '')} · {s.get('time', '')}".strip()
    if s.get("sold_out") is False and isinstance(s.get("seats_left"), int) \
            and 0 < s["seats_left"] <= 10:
        avail = f"Almost gone · {s['seats_left']} seats left"
    elif s.get("sold_out") is False:
        avail = "Seats available"
    else:
        avail = "Availability unconfirmed — check now"
    return (
        f"🎤 Standout at the Comedy Cellar!\n"
        f"{when}\n"
        f"{who}\n"
        f"Crowd {s.get('crowd_work', 0)} · A-list {s.get('drop_in', 0)} · "
        f"Taste {s.get('taste', 0)}\n"
        f"{avail}\n"
        f"Book: {RESV_PAGE_URL}"
    )


def _send_whatsapp(text: str, phone: str, apikey: str) -> bool:
    qs = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": apikey})
    try:
        with urllib.request.urlopen(f"{CALLMEBOT_URL}?{qs}", timeout=30) as r:
            r.read()
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; never fail the scan
        print(f"WhatsApp send failed: {e}", file=sys.stderr)
        return False


def notify_standouts(shows: list[dict]) -> None:
    """Send a one-time WhatsApp for each newly-posted, still-bookable standout."""
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not (phone and apikey):
        return  # alerts not configured — silently skip

    today = dt.date.today().isoformat()
    alerted = load_alerted()
    sent = 0
    for s in shows:
        sid = str(s.get("id", ""))
        if not sid or sid in alerted:
            continue
        if s.get("date", "") < today:
            continue
        if not is_standout(s):
            continue
        if s.get("sold_out") is True:          # only ping while bookable
            continue
        if _send_whatsapp(_alert_text(s), phone, apikey):
            alerted.add(sid)
            sent += 1
            time.sleep(1)  # be gentle with the free relay
    if sent:
        save_alerted(alerted)
        print(f"WhatsApp: alerted on {sent} new standout(s).")


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

    # Load YOUR availability + taste. The scout only scans nights you're free,
    # and the viewer highlights these on the calendar regardless of whether a
    # lineup has been posted yet.
    avail, taste = load_config()
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

    # Load the prior run once — reused for both the score cache and the
    # persistence merge below.
    out_path = Path(args.out)
    prior_data = {}
    if out_path.exists():
        try:
            prior_data = json.loads(out_path.read_text())
        except Exception as e:
            print(f"  ! Could not read prior data: {e}", file=sys.stderr)

    # Score cache: reuse prior scores for any bill we've already scored so the
    # frequent evening scans don't re-pay Claude for unchanged lineups. A new
    # or changed lineup is NOT in the cache, so it's always freshly scored (and
    # can still trigger an alert). The cache is dropped entirely if the taste
    # list changed, since taste drives the scoring.
    score_cache = {}
    if prior_data.get("taste_benchmark") == taste:
        for s in prior_data.get("shows", []):
            if all(k in s for k in ("drop_in", "taste", "crowd_work")):
                score_cache[_lineup_sig(s)] = {
                    "drop_in": s["drop_in"], "taste": s["taste"],
                    "crowd_work": s["crowd_work"], "blurb": s.get("blurb", ""),
                }

    if not all_shows:
        pass
    elif args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.no_ai:
            print("ANTHROPIC_API_KEY not set — running without scoring.",
                  file=sys.stderr)
        for s, sc in zip(all_shows, fallback_scores(all_shows)):
            s.update(sc)
    else:
        to_score, reused = [], 0
        for s in all_shows:
            cached = score_cache.get(_lineup_sig(s))
            if cached:
                s.update(cached)
                reused += 1
            else:
                to_score.append(s)
        if reused:
            print(f"Reused cached scores for {reused} unchanged bill(s).")
        if to_score:
            print(f"Scoring {len(to_score)} new/changed show(s) with Claude...")
            try:
                for s, sc in zip(to_score,
                                 score_shows_with_claude(to_score, taste)):
                    s.update(sc)
            except Exception as e:
                print(f"  ! Claude scoring failed: {e}", file=sys.stderr)
                for s, sc in zip(to_score, fallback_scores(to_score)):
                    s.update(sc)
        else:
            print("All bills unchanged — no Claude scoring needed this run.")

    # Merge with prior run so scored shows persist. The Comedy Cellar API only
    # exposes the next ~3-4 days, so a date scraped today won't be re-scrapable
    # tomorrow until it comes back into the window — but we still want to keep
    # its scored lineup. We:
    #   - keep prior shows whose date is today or future and not re-scraped
    #   - replace any date we successfully re-scraped (fresh data wins)
    #   - drop anything in the past
    fresh_dates = {s["date"] for s in all_shows}
    merged = list(all_shows)
    for s in prior_data.get("shows", []):
        if s["date"] < today_iso:
            continue  # past
        if s["date"] in fresh_dates:
            continue  # already replaced with fresh data
        merged.append(s)
    merged.sort(key=lambda s: (s["date"], time_to_minutes(s["time"])))

    # Check live reservation availability so the viewer can flag sold-out shows.
    annotate_availability(merged)

    # Ping WhatsApp once for any newly-posted, still-bookable standout.
    notify_standouts(merged)

    # Resolve web-property links for every comic on the bills plus the taste
    # list, caching new ones. Skipped entirely in --no-ai mode.
    if not args.no_ai:
        comic_names = set(taste)
        for s in merged:
            for c in s.get("comedians", []):
                if c.get("name"):
                    comic_names.add(c["name"])
        links, links_comment = load_comic_links()
        before = len(links)
        links = resolve_comic_links(comic_names, links)
        if len(links) != before or not LINKS_PATH.exists():
            write_comic_links(links, links_comment)
            print(f"Comic links: {len(links)} cached "
                  f"(+{len(links) - before} new).")

    payload = {
        # UTC with an explicit offset so the viewer can convert to Eastern Time
        # reliably (a naive timestamp would be misread as the viewer's local tz).
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        # The calendar reflects YOUR availability — every night you're free,
        # whether or not a lineup has posted yet.
        "available_dates": sorted(avail),
        "taste_benchmark": taste,
        "shows": merged,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out_path} ({len(merged)} shows on "
          f"{len(set(s['date'] for s in merged))} dates; "
          f"{len(avail)} availability dates)")


if __name__ == "__main__":
    main()
