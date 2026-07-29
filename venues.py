#!/usr/bin/env python3
"""
LA venue scrapers for the comedy scout.

Each venue exposes its lineups differently, so this module normalizes them all
to a single show shape the rest of the pipeline (scoring, alerts, viewer)
already understands:

    {
        "venue":      "Comedy Store",        # human-readable venue name
        "id":         "comedystore:4097:...",# stable per-show id (dedupe/alerts)
        "date":       "2026-07-28",          # ISO date (America/Los_Angeles)
        "time":       "9:00 pm",             # display time
        "room":       "Original Room",       # room within the venue
        "title":      "Headliners of the OR",# the show/series name
        "comedians":  [{"name": "...", "credits": ""}, ...],
        "ticket_url": "https://...",         # where to book
        "sold_out":   True | False | None,   # None = availability unknown
    }

Only Comedy Store and Hollywood Improv are implemented today; both are plain
server-rendered HTML we can parse robustly. Laugh Factory renders its dated
schedule through an obfuscated AJAX API and is left as a follow-up (see
scrape_laugh_factory).
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

# A real browser UA — the venue sites 200 for this and 403 some bot agents.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _split_lineup(rest: str) -> list[str]:
    """'ft. Brent Morin, Alistair Ogden and more TBA' -> ['Brent Morin', ...]."""
    rest = _clean(rest)
    rest = re.sub(r"^(ft\.?|feat\.?|featuring|with)\s+", "", rest, flags=re.I)
    # Drop trailing "and more TBA" / "& more" / "and many more".
    rest = re.sub(r"[,&]?\s*(and|&)\s+(many\s+)?more(\s+tba)?\.?$", "",
                  rest, flags=re.I)
    parts = re.split(r"\s*,\s*|\s+&\s+|\s+and\s+", rest)
    out = []
    for p in parts:
        p = _clean(p)
        if p and not re.fullmatch(r"(more|tba|special guests?)", p, re.I):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# The Comedy Store — https://thecomedystore.com/calendar/
# Server-rendered. Each show is a div.show-item with the ISO date+time in the
# detail-page slug, a room label, a full lineup, and an inline "SOLD OUT" flag.
# ---------------------------------------------------------------------------
def scrape_comedy_store() -> list[dict]:
    html = _fetch("https://thecomedystore.com/calendar/")
    soup = BeautifulSoup(html, "html.parser")
    shows = []
    for item in soup.select("div.show-item"):
        a = item.select_one("h2.show-title a")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/(\d{4}-\d{2}-\d{2})t(\d{2})(\d{2})(\d{2})", href)
        if not m:
            continue  # no parseable date — skip
        date = m.group(1)
        hh = int(m.group(2))
        ampm = "pm" if hh >= 12 else "am"
        time_s = f"{hh % 12 or 12}:{m.group(3)} {ampm}"

        room = None
        for h3 in item.select("h3"):
            sp = h3.select_one("span")
            txt = h3.get_text(strip=True)
            # Room header looks like "<span>OR</span>Original Room"; skip the
            # "The Lineup" header and anything without a short abbrev span.
            if sp and len(sp.get_text(strip=True)) <= 3 and txt != "The Lineup":
                room = _clean(txt[len(sp.get_text(strip=True)):]) or txt
                break

        comedians = [{"name": _clean(x.get_text()), "credits": ""}
                     for x in item.select(".lineup-item a")
                     if _clean(x.get_text())]
        sold_out = bool(item.find(string=re.compile(r"SOLD\s*OUT", re.I)))

        shows.append({
            "venue": "Comedy Store",
            "id": f"comedystore:{_clean(href)}",
            "date": date,
            "time": time_s,
            "room": room or "Comedy Store",
            "title": _clean(a.get_text()),
            "comedians": comedians,
            "ticket_url": "https://thecomedystore.com" + href
            if href.startswith("/") else href,
            "sold_out": sold_out,
        })
    return shows


# ---------------------------------------------------------------------------
# Hollywood Improv — https://improv.com/hollywood/calendar/
# Server-rendered. Each show is an a.item.wtimes with a month/day block, a
# showtime, a room ("@ The Lab"), a lineup ("ft. ..."), and a status class
# ("buynow" vs "soldout"). No year on the page, so we infer it.
# ---------------------------------------------------------------------------
def scrape_improv(today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    html = _fetch("https://improv.com/hollywood/calendar/")
    soup = BeautifulSoup(html, "html.parser")
    shows = []
    for a in soup.select("a.item.wtimes"):
        dtel = a.select_one(".maindate dt")
        dd = a.select_one(".maindate dd")
        if not dtel or not dd:
            continue
        mon = _MONTHS.get(_clean(dtel.get_text()))
        dmatch = re.match(r"\d+", _clean(dd.get_text()))
        if not mon or not dmatch:
            continue
        day = int(dmatch.group())
        # Infer year: calendar only runs forward, so a month earlier than the
        # current month means it has rolled into next year.
        year = today.year + (1 if mon < today.month else 0)
        try:
            date = dt.date(year, mon, day).isoformat()
        except ValueError:
            continue

        t = a.select_one(".times span")
        time_s = _fmt_time(t.get_text(strip=True)) if t else ""
        city = a.select_one(".city")
        room = _clean(city.get_text()).lstrip("@").strip() if city else "Improv"

        main = a.select_one(".lt-main")
        rest = a.select_one(".lt-rest")
        title = _clean(main.get_text()) if main else ""
        comedians = [{"name": n, "credits": ""}
                     for n in _split_lineup(rest.get_text() if rest else "")]
        # Fall back to the event slug for a title when the markup is bare.
        if not title:
            slug = re.search(r"/event/([^/]+)/", a.get("href", ""))
            if slug:
                title = _clean(urllib.parse.unquote(slug.group(1)).replace("+", " "))

        status = a.select_one(".status")
        classes = " ".join(status.get("class", [])) if status else ""
        sold_out = True if "soldout" in classes else (
            False if "buynow" in classes else None)

        href = a.get("href", "")
        shows.append({
            "venue": "Hollywood Improv",
            "id": f"improv:{_clean(href)}",
            "date": date,
            "time": time_s,
            "room": room,
            "title": title,
            "comedians": comedians,
            "ticket_url": "https://improv.com" + href
            if href.startswith("/") else href,
            "sold_out": sold_out,
        })
    return shows


# ---------------------------------------------------------------------------
# Laugh Factory — https://www.laughfactory.com/hollywood
# The static HTML lists recurring show *series* (e.g. "Rubee Tuesdays") with
# their comedians, but the dated instances, showtimes, and availability load
# via an obfuscated jQuery AJAX API. Reverse-engineering that endpoint is the
# planned next step; until then this returns nothing so we never emit undated
# shows the availability/calendar logic can't place.
# ---------------------------------------------------------------------------
def scrape_laugh_factory(today: dt.date | None = None) -> list[dict]:
    return []


def _fmt_time(t: str) -> str:
    """Normalize '7:30pm' -> '7:30 pm' to match the rest of the app."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", _clean(t), re.I)
    if not m:
        return _clean(t)
    return f"{int(m.group(1))}:{m.group(2)} {m.group(3).lower()}m"


_SCRAPERS = [scrape_comedy_store, scrape_improv, scrape_laugh_factory]


def scrape_all(scan_dates: set[str], today_iso: str) -> list[dict]:
    """Scrape every venue, keep only shows on the user's available dates that
    are today-or-future. Never lets one venue's failure sink the others."""
    out = []
    for fn in _SCRAPERS:
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001 — best-effort per venue
            print(f"  ! {fn.__name__} failed: {e}", file=sys.stderr)
            continue
        kept = [s for s in got
                if s["date"] >= today_iso and s["date"] in scan_dates]
        print(f"  · {fn.__name__}: {len(got)} scraped, {len(kept)} on your dates")
        out.extend(kept)
    return out


if __name__ == "__main__":
    # Quick manual check: print a few shows per venue.
    for fn in (scrape_comedy_store, scrape_improv):
        rows = fn()
        print(f"\n=== {fn.__name__}: {len(rows)} shows ===")
        for s in rows[:4]:
            names = ", ".join(c["name"] for c in s["comedians"][:4])
            print(f"  {s['date']} {s['time']:>8} [{s['room']}] "
                  f"sold={s['sold_out']} — {s['title']} | {names}")
