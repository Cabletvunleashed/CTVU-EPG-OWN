#!/usr/bin/env python3
"""
Smarter placeholder listings for streams no guide source covers.

plan(stream_name, category, now_utc) -> {
    "title":  clean human title for the plain 6h blocks,
    "desc":   optional description ("24/7 - continuous episodes"),
    "events": [(start_utc, stop_utc, title)]   timed PPV event parsed from the NAME,
    "before": filler title before the event (or None),
    "after":  filler title after the event (or None),
}

Times are only taken when the name carries both a date and a time zone we can
prove from the provider's own formats (sampled 2026-10-27 from 57,160 streams):
  A  "US (ESPN+ 135) | Title (2026-10-24 14:02:10)"      local time of the feed:
        US / FLSP / BTN+ / Peacock / Apple MLS / Fanatiz / Victory+ -> US Eastern
        (checked: ESPN+ names repeat "2:00PM ET"; Peacock Coventry v Fulham 09:50 = 15:00 BST kick-off),
        "(MX)" -> Mexico City (NBA 7pm ET game listed 17:53; Norwich-Ipswich 12:30 BST listed 05:20),
        "AU (" -> Sydney (Coventry v Fulham listed 00:50 next day = 15:00 BST).
        Year 2098 = no event.
  B  "Next | Title | ... | 25-10-2026 | 20:00 (GMT) | 8K EXCLUSIVE | ..."   explicit GMT
  C  "ENDED | TITLE | Sat 24 Oct 17:00 CEST (SE) | ..."                   explicit zone, year inferred
  D  "Edmonton vs Toronto @ Oct 25 1:05 AM :Viaplay SE  56"   the SERVICE's own local time:
        the same NHL game is "Sun 25 Oct 01:05 CEST (SE)" on Viaplay SE but "@ Oct 24 7:00 PM :Sportsnet+";
        MLS "@ Oct 24 1:30 PM :MLS" = Apple's 13:25 ET. Zone from the suffix (SE/NO/DK/FI/NL/FR/ES/IS/
        VG/MTV FI -> that country; MLS/NBA/NCAAB/Flo/MAX US/Paramount+/Milb/Golf/TSN+/Sportsnet+ -> US
        Eastern); unknown suffix -> no time. Year inferred; "18:30 PM" read as 18:30
  E  "Flo (FLSP) 198: 2025 Orebro HK vs Djurgardens IF Hockey - 23/10 13:00"  US Eastern (that SHL
        game is 19:00 CEST = 13:00 ET), day/month, year inferred
Anything else with a time but no provable zone gets the title only.
"""
import datetime
import re
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc
ET, MX, SYD = ZoneInfo("America/New_York"), ZoneInfo("America/Mexico_City"), ZoneInfo("Australia/Sydney")
EVENT_HOURS = 3
ABBR = {"GMT": 0, "UTC": 0, "BST": 1, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "WET": 0, "WEST": 1,
        "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
        "AEST": 10, "AEDT": 11}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

# also the Latin-1 superscripts ² ³ ¹ of "³⁸⁴⁰ᴾ" (2026-10-31 ac: placeholders read "ESPN ³")
SUPER = re.compile(r"[\u00b2\u00b3\u00b9\u1d00-\u1dbf\u02b0-\u02ff\u2070-\u209f\u2c7d\u2c7c\uA7F8-\uA7FF◉⚽▶★☆●•]")
TAGS = re.compile(r"\b(?:F?HD|UHD|SD|4K|8K|HEVC|H\.?265|RAW|VIP|GOLD|PLUS|BACKUP|MULTI-AUDIO|\d+\s*FPS|FHD@\d+FPS)\b",
                  re.I)
PREFIX = re.compile(r"^\s*[^:|]{1,12}[:|]\s*")
NO_EVENT = re.compile(r"NO EVENT|^\s*:|\(2098-", re.I)
SHOW_FOLDER = re.compile(r"24/7|ON AIR|SHAHID|LUXPLAY", re.I)

A = re.compile(r"^(?P<pre>.*?)\|\s*(?P<title>.*?)\s*\((?P<d>\d{4}-\d\d-\d\d) (?P<t>\d\d:\d\d):\d\d\)\s*$")
B = re.compile(r"^\s*(?P<st>Next|End|Live)\s*\|(?P<body>.*?)\|\s*(?:all\s*\|\s*)?"
               r"(?P<d>\d\d-\d\d-\d{4}|\d{4}-\d\d-\d\d)\s*\|\s*(?P<t>\d{1,2}:\d\d)\s*\((?P<z>GMT|UTC)\)", re.I)
D = re.compile(r"^(?P<title>.+?)\s*@\s*(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}) (?P<h>\d{1,2}):(?P<mi>\d\d)\s*(?P<ap>[AP]M)?"
               r"\s*:\s*(?P<svc>.*?)\s*\d*\s*$")
SVC_CC = {"SE": "Europe/Stockholm", "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
          "NL": "Europe/Amsterdam", "FR": "Europe/Paris", "ES": "Europe/Madrid", "IS": "Atlantic/Reykjavik",
          "DE": "Europe/Berlin", "IT": "Europe/Rome", "PL": "Europe/Warsaw", "UK": "Europe/London", "US": "America/New_York"}
SVC_ET = re.compile(r"^(?:MLS|NBA|NCAAB|Flo\b.*|Paramount\+|Milb|Golf|TSN\+|Sportsnet\+)$", re.I)


def _svc_zone(svc):
    if svc == "VG":
        return ZoneInfo("Europe/Oslo")
    if SVC_ET.match(svc):
        return ET
    cc = svc.split()[-1].upper() if svc.split() else ""
    return ZoneInfo(SVC_CC[cc]) if cc in SVC_CC and len(svc.split()) > 1 else None
E = re.compile(r"^Flo \(FLSP\) \d+:\s*(?P<title>.+?)\s+-\s+(?P<day>\d\d)/(?P<mon>\d\d) (?P<h>\d\d):(?P<mi>\d\d)\s*$")
C = re.compile(r"^\s*(?P<st>ENDED|NEXT|LIVE)\s*\|\s*(?P<title>.*?)\s*\|\s*(?:[A-Z][a-z]{2}\s+)?(?P<day>\d{1,2})\s+"
               r"(?P<mon>[A-Za-z]{3})\s+(?P<t>\d{1,2}:\d\d)\s+(?P<z>[A-Z]{2,4})\b", re.I)


def clean(name):
    n = PREFIX.sub("", name or "")
    n = re.sub(r"\(\s*(?:FHD|HD|UHD|4K|8K)[^)]*\)", " ", n, flags=re.I)
    n = SUPER.sub(" ", n)
    n = TAGS.sub(" ", n)
    n = re.sub(r"\s*\|\s*8K EXCLUSIVE.*$", "", n, flags=re.I)
    n = re.sub(r"[\s_]+", " ", n).strip(" -:|*&")     # "SKY SPORTS F1 ⁴ᵏ & ³⁸⁴⁰ᴾ" leaves a "&"
    return n or (name or "").strip()


def _title(t):
    t = re.sub(r"[_`]", lambda m: " " if m.group(0) == "_" else "'", t or "")
    t = re.sub(r"\s+", " ", t).strip(" -:|")
    return t


def _year_for(day, mon, hm, now):
    """the year that puts day/mon nearest to now (names carry no year)"""
    best = None
    for y in (now.year - 1, now.year, now.year + 1):
        try:
            d = datetime.datetime(y, mon, day, *hm)
        except ValueError:
            continue
        if best is None or abs((d - now.replace(tzinfo=None)).days) < abs((best - now.replace(tzinfo=None)).days):
            best = d
    return best


def _event(name, now):
    m = A.match(name)
    if m:
        if m.group("d").startswith("2098"):
            return None
        pre = m.group("pre")
        tz = MX if "(MX)" in pre else SYD if pre.strip().startswith("AU") else ET
        local = datetime.datetime.strptime(m.group("d") + " " + m.group("t"), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        title = _title(re.sub(r"\s*\b[A-Z][a-z]{2} \d{1,2} \d{1,2}:\d\d\s*[AP]M ET\b", "", m.group("title")))
        title = re.sub(r"^\w+:\s+", lambda x: x.group(0).capitalize(), title)   # "volleyball:  X" -> "Volleyball: X"
        return local.astimezone(UTC), title
    m = B.match(name)
    if m:
        d = m.group("d")
        fmt = "%d-%m-%Y" if d[2] == "-" else "%Y-%m-%d"
        start = datetime.datetime.strptime(d + " " + m.group("t"), fmt + " %H:%M").replace(tzinfo=UTC)
        parts = [p.strip() for p in m.group("body").split("|") if p.strip() and p.strip().lower() != "all"]
        return start, _title(" | ".join(parts[:2]))
    m = C.match(name)
    if m and m.group("z").upper() in ABBR and m.group("mon")[:3].upper() in MON:
        hm = tuple(int(x) for x in m.group("t").split(":"))
        d = _year_for(int(m.group("day")), MON[m.group("mon")[:3].upper()], hm, now)
        if d:
            start = (d - datetime.timedelta(hours=ABBR[m.group("z").upper()])).replace(tzinfo=UTC)
            t = _title(m.group("title"))
            if t.isupper():   # "NHL EDMONTON - TORONTO" -> "NHL Edmonton - Toronto" (keep short acronyms)
                t = " ".join(w if len(w) <= 3 and w.isalpha() else w.capitalize() for w in t.split())
            return start, t
    m = D.match(name)
    if m and m.group("mon").upper() in MON and _svc_zone(m.group("svc")):
        tz = _svc_zone(m.group("svc"))
        h = int(m.group("h"))
        if m.group("ap") and h <= 12:
            h = h % 12 + (12 if m.group("ap") == "PM" else 0)
        d = _year_for(int(m.group("day")), MON[m.group("mon").upper()], (h, int(m.group("mi"))), now)
        if d:
            return d.replace(tzinfo=tz).astimezone(UTC), _title(m.group("title").replace("\u2068", "").replace("\u2069", ""))
    m = E.match(name)
    if m:
        d = _year_for(int(m.group("day")), int(m.group("mon")), (int(m.group("h")), int(m.group("mi"))), now)
        if d:
            return d.replace(tzinfo=ET).astimezone(UTC), _title(re.sub(r"^\d{4}\s+", "", m.group("title")))
    return None


def plan(stream_name, category, now_utc):
    name = (stream_name or "").strip()
    out = {"title": clean(name), "desc": None, "events": [], "before": None, "after": None}
    if NO_EVENT.search(name):
        out["title"] = "No event scheduled"
        return out
    ev = _event(name, now_utc)
    if ev:
        start, title = ev
        if title:
            out["events"] = [(start, start + datetime.timedelta(hours=EVENT_HOURS), title)]
            out["before"] = "Up next: " + title   # the player shows the event's own start time
            out["after"] = "Event ended"
            out["title"] = title
            return out
    # status words with no usable time: keep the event text, drop the time
    m = re.match(r"^\s*(?:Next|End|Live|ENDED|NEXT|LIVE)\s*\|\s*(.*?)\s*\|", name)
    if m:
        out["title"] = _title(m.group(1)) or out["title"]
        return out
    m = re.match(r"^(.*?)\s*@\s*[A-Z][a-z]{2} \d{1,2} \d{1,2}:\d\d", name)
    if m and m.group(1).strip():
        out["title"] = _title(m.group(1))
        return out
    if SHOW_FOLDER.search(category or "") or name.upper().startswith("24/7"):
        out["title"] = re.sub(r"^24/7\s*:?\s*", "", out["title"], flags=re.I)
        out["desc"] = "24/7 - continuous episodes"
    return out
