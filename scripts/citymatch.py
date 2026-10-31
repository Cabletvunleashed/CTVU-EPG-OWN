"""Second pass for US local stations that callsign.match() could not place, e.g.
  "TV: DENVER, CO KRMA PBS 6 A3 HD (D11) DENVER"  (bare call sign, no parentheses)
  "US: CW 7 ( WSPA) SPARTANBURG"                   (call sign with a stray space)
  "AT&T: FOX 5 LOCAL ATLANTA", "US: ABC 7 HD [FORT MYERS]"   (network + city)
matched against tvpassport.com rows like "ABC (WLOS) Asheville, NC". Strict: one answer or none.
"""
import collections
import difflib
import re

import callsign

# tvpassport local row: "<prefix> (<CALL><digit?>[-SFX]) <City>[, ST][ HD]"
ROW = re.compile(r"^(?P<pre>.*?)\s*\((?P<cs>[KW][A-Z]{2,3})(?P<num>\d*)(?:-(?P<sfx>[A-Z0-9]{1,5}))?\)\s*"
                 r"(?P<city>[^,]+?)(?:,\s*(?P<st>[A-Za-z/ ]+?))?(?:\s+HD)?\s*$")
SUBSFX = re.compile(r"^(?:TV|DT|LD|CD|CA|LP)?([2-9]|1\d)$")
# the stream is a national/streaming/regional-sports feed, not an over-the-air local station
NOT_LOCAL = re.compile(r"\b(NEWS|SPORTS?|WEATHER|BUSINESS|BRAVO|EAST|WEST|NETWORK|NATIONAL|PACIFIC|"
                       r"COLLEGE|DEPORTES|NOW|LIVE|PLUS|XTRA|ORIGINALS|ON TUBI|PPV)\b")
# all-caps words that look like call signs but are ordinary words/labels in stream names
WORDS = {"WEST", "KIDS", "WILD", "WORLD", "WOW", "WAR", "WAY", "WIFE", "KING", "KISS", "KICK", "WHO", "WHY",
         "WIN", "WINE", "WOOD", "WORK", "KNOW", "KILL", "KEEP", "WAVE", "WIRE", "WORD", "WARS", "WEEK", "WISE",
         "KIND", "KEYS", "WALK", "WIDE", "WALL", "WHAT", "WITH", "WAKE", "WARM", "WASH", "WOLF", "WEB"}
ALIASES = [(r"\bST\.?\s", "SAINT "), (r"\bFT\.?\s", "FORT "), (r"\bMT\.?\s", "MOUNT "),
           (r"\bD\.\s?C\.?", "DC"), (r"\bWASHINGTON DC\b", "WASHINGTON")]


def norm(s):
    s = " " + re.sub(r"[ᴀ-ᶿʰ-˿⁰-₟◉⚽]", " ", s).upper() + " "
    for a, b in ALIASES:
        s = re.sub(a, b, s)
    s = re.sub(r"(?<=[A-Z])-(?=[A-Z])", "", s)   # WINSTON-SALEM must not contain the city SALEM
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(name):
    m = ROW.match(name.strip())
    if not m:
        return None
    sub = bool(m.group("num")) or bool(m.group("sfx") and SUBSFX.match(m.group("sfx")))
    pre = re.sub(r"\s+HD$", "", m.group("pre").strip())
    return {"pre": pre, "cs": m.group("cs"), "sub": sub, "city": norm(m.group("city")),
            "st": (m.group("st") or "").strip().upper()[:2],
            "net": callsign.network(pre), "pure": callsign.network(pre) is not None and
            norm(pre) in {"ABC", "NBC", "CBS", "FOX", "CW", "PBS", "MYNETWORKTV", "MNT", "ION", "TELEMUNDO",
                          "UNIVISION", "UNIMAS", "ESTRELLA TV", "AZTECA", "INDEPENDENT"}}


def index(rows):
    """rows: (site, site_id, name) -> {"cs": {CALL: [row]}, "city": {(NET, CITY): [row]}}"""
    by_cs, by_city = collections.defaultdict(list), collections.defaultdict(list)
    for site, sid, nm in rows:
        p = parse(nm)
        if not p or re.search(r"DELAYED|FEED", nm, re.I):   # time-shifted copies are not the local schedule
            continue
        r = (site, sid, nm.strip(), p)
        by_cs[p["cs"]].append(r)
        if p["pure"] and p["city"]:
            by_city[(p["net"], p["city"])].append(r)
    return {"cs": by_cs, "city": by_city}


def _one(cands):
    """main channel over sub-channels, then collapse the 'X' / 'X HD' duplicate of one station"""
    main = [c for c in cands if not c[3]["sub"]]
    cands = main or cands
    by_station = {}
    for c in sorted(cands, key=lambda c: (c[2].endswith(" HD"), c[1])):
        by_station.setdefault((c[3]["cs"], c[3]["sub"], c[3]["pre"], c[3]["city"]), c)
    cands = list(by_station.values())
    return cands[0][:3] if len(cands) == 1 else None


def norm_brackets(s):
    return re.sub(r"\s+", " ", s.upper())


def _body(stream_name):
    # drop the "US:" / "TV:" / "AT&T:" label; keep what identifies the station
    return re.sub(r"^\s*[^:|]{1,12}[:|]\s*", "", stream_name or "")


# free streaming services carry stations' streaming news channels ("FOX LOCAL", Cox news on Prime),
# not the over-the-air schedule
FAST = re.compile(r"^\s*(PRIME|RK|ROKU|TUBI|PLUTO|PEACOCK|XUMO|PLEX|SAMSUNG|FREEVEE|STIRR)\s*[:|]", re.I)


# The provider's per-city affiliate package: "US: FOX NET [BINGHAMPTON NY]", "US: FOX NET [WAUSAU]".
# The name itself says "this network's station for this city", so it gets its own looser step
# (2026-10-30 ac, after 92's report: 12 of 21 such streams were placeholders). A city typo
# (BINGHAMPTON), a cut city (CHARLES LA = Lake Charles), a wrong state (MINOT MD) and simulcast
# pairs (WAHU + WCAV-DT2 both FOX Charlottesville) used to make the strict step refuse. APLENA
# (Alpena) stays unmatched: tvpassport has no FOX Alpena row at all.
NET_CITY = re.compile(r"\b([A-Z]+) NET \[([^\]]+)\]")


def _net_city(net, place, idx):
    words = place.split()
    st = words[-1] if len(words) > 1 and len(words[-1]) == 2 else ""
    city = " ".join(words[:-1]) if st else place
    cities = {c: rows for (n, c), rows in idx["city"].items() if n == net}
    rows = cities.get(city)
    if rows is None:            # "CHARLES" -> "LAKE CHARLES": one city of that network ending in it
        tail = [c for c in cities if c.endswith(" " + city)]
        rows = cities[tail[0]] if len(tail) == 1 else None
    if rows is None:            # typo: one close spelling, close enough that a different town can't pass
        near = difflib.get_close_matches(city, list(cities), n=2, cutoff=0.9)
        rows = cities[near[0]] if len(near) == 1 else None
    if not rows:
        return None
    if st and len({c[3]["st"] for c in rows}) > 1:
        rows = [c for c in rows if c[3]["st"] == st] or rows   # state only breaks a tie, never rejects
    stations = {c[3]["cs"] for c in rows}
    if len(stations) > 1:       # simulcast pair: the full-power/main station, then the one with an HD feed
        main = {c[3]["cs"] for c in rows if not c[3]["sub"]}
        if len(main) > 1:
            main = {c[3]["cs"] for c in rows if c[3]["cs"] in main and c[2].endswith(" HD")}
        if len(main) != 1:
            return None
        rows = [c for c in rows if c[3]["cs"] in main]
    return _one(rows)


def match(stream_name, idx):
    """-> (site, site_id, name, how) or None.  how = 'bare-callsign' | 'network-city' | 'net-city'"""
    nc = NET_CITY.search(norm_brackets(stream_name or ""))
    if nc and callsign.network(nc.group(1)) and not FAST.search(stream_name or ""):
        r = _net_city(callsign.network(nc.group(1)), norm(nc.group(2)), idx)
        return r + ("net-city",) if r else None
    if FAST.search(stream_name or "") or re.search(r"\bLOCAL\b", (stream_name or "").upper()):
        return None
    body = _body(stream_name)
    up = norm(body)
    net = callsign.network(body)
    # TV City lines are "CITY, ST CALL NET N A3 HD ORIGIN-CITY": only the first city is the station's
    lead = re.match(r"^\s*([A-Z .'-]+),\s*([A-Z]{2})\b", body.upper())
    lead_city = norm(lead.group(1)) if lead else None
    if NOT_LOCAL.search(up):
        return None             # news/sports/streaming spin-offs ("CBS WASHINGTON DC NEWS (WUSA 9)")

    # (a) bare call sign (or "( WSPA)"): must be a real call sign in the index; network must agree
    raw = [t for t in re.findall(r"\b([KW][A-Z]{2,3})(?:-(?:TV|DT|LD|CD))?\b", re.sub(r"\(\s+", "(", body.upper()))
           if t not in WORDS]
    toks = list(dict.fromkeys(t for t in raw if t in idx["cs"]))
    if raw and not toks:
        return None             # names a call sign we don't have ("WQPT PBS 24"): never guess by city
    if len(toks) == 1:
        cands = idx["cs"][toks[0]]
        if net:   # the station row must BE that network ("PBS"), not a spin-off ("PBS Kids", "FOX 35 Plus")
            cands = [c for c in cands if c[3]["net"] == net and c[3]["pure"]]
        elif lead_city:
            cands = [c for c in cands if c[3]["city"] == lead_city]
        else:
            cands = []          # a lone call-sign-shaped word with no network and no city: too weak
        if lead_city:
            cands = [c for c in cands if c[3]["city"] == lead_city] or []
        r = _one(cands)
        return r + ("bare-callsign",) if r else None   # a named call sign that disagrees: never fall back to city
    if len(toks) > 1:
        return None             # two different call signs in one name: refuse

    # (b) network + city, for real over-the-air locals only
    if not net:
        return None
    hits = []
    for (n, city), rows in idx["city"].items():
        if n == net and (lead_city == city if lead_city else re.search(r"\b" + re.escape(city) + r"\b", up)):
            hits.append((city, rows))
    if not hits:
        return None
    # "NEW YORK" also contains "YORK": keep only the longest city names that matched
    best = max(len(c) for c, _ in hits)
    hits = [h for h in hits if len(h[0]) == best]
    if len(hits) != 1:
        return None
    # one station only: two call signs (or two states: Charleston SC/WV) with that network in that city = refuse
    if len({c[3]["cs"] for c in hits[0][1]}) != 1:
        return None
    rows = hits[0][1]
    # only a sub-channel carries that network here while a full-power station of the same network sits
    # elsewhere in the same state (Seattle: KZJO-DT2 "FOX 13+" vs FOX 13 = KCPQ Tacoma): too unsure, refuse
    if all(c[3]["sub"] for c in rows):
        st = rows[0][3]["st"]
        if st and any(not c[3]["sub"] and c[3]["st"] == st for (n, _), rr in idx["city"].items() if n == net
                      for c in rr):
            return None
    r = _one(rows)
    return r + ("network-city",) if r else None
