#!/usr/bin/env python3
"""
CTVU own EPG: a complete guide built from the provider's channel list, no myepg.

Every provider live stream gets a guide channel whose display-name is the
exact stream name (that is how TiviMate links guide to channel for this
provider, since most streams have no tvg-id). If the provider gives a tvg-id
(epg_channel_id), the guide channel uses it as its id as well.

Listings, best first:
  1. provider's own XMLTV, by the stream's epg_channel_id
  2. other guides (--source), same-country unique name match
  3. placeholder: the channel name itself in PLACEHOLDER_HOURS blocks
     (what myepg does for event/PPV/24-7 channels)

Usage:
  own_epg.py --out DIR --live live.json --cats cats.json --provider-xmltv FILE_OR_URL
             [--source name=FILE_OR_URL ...]
"""

import argparse
import collections
import datetime
import gzip
import hashlib
import json
import os
import re
import sys
import time
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FILL_AHEAD_HOURS", "48")
# Size guard for 2GB TV boxes (2026-10-27: 243MB unpacked after the 5-step upgrade; the
# emulator load test passed at 197MB): long descriptions cut, short past window.
os.environ.setdefault("DESC_MAX", "300")
import build_epg as b  # noqa: E402
import smart_ph  # noqa: E402

NOW = b.NOW
PAST_HOURS = float(os.environ.get("PAST_HOURS", "2"))
AHEAD_HOURS = float(os.environ.get("AHEAD_HOURS", "48"))
PLACEHOLDER_HOURS = float(os.environ.get("PLACEHOLDER_HOURS", "12"))
MIN_REAL_HOURS = float(os.environ.get("MIN_REAL_HOURS", "6"))

# provider category prefix (this provider's own codes, not ISO) -> guide id suffixes
CC = {"US": ["us", "us2", "us_locals1", "us_sports1"], "UK": ["uk"], "CA": ["ca", "ca2"], "AU": ["au"], "NZ": ["nz"],
      "IE": ["ie"], "FR": ["fr"], "DE": ["de", "at", "ch"], "AT": ["at", "de"], "CH": ["ch", "de"], "IT": ["it"],
      "ES": ["es"], "PT": ["pt"], "NL": ["nl", "be"], "BE": ["be", "nl"], "PL": ["pl"], "HR": ["hr"], "GR": ["gr"],
      "CY": ["cy", "gr"], "SE": ["se"], "NO": ["no"], "DK": ["dk"], "FI": ["fi"], "IS": ["is"], "TR": ["tr"],
      "RO": ["ro"], "HU": ["hu"], "CZ": ["cz", "sk"], "BG": ["bg"], "AL": ["al"], "MK": ["mk"], "SI": ["si"],
      "SR": ["rs"], "BH": ["ba"], "CG": ["me", "rs"], "EXYU": ["rs", "hr", "ba"], "RU": ["ru", "ua"], "LV": ["lv"],
      "LT": ["lt"], "AM": ["am"], "AZ": ["az"], "GE": ["ge"], "KZ": ["kz"], "IL": ["il"], "IR": ["ir"],
      "AR": ["ae", "sa", "eg", "qa", "lb", "ma", "dz", "tn", "jo", "kw"], "MX": ["mx"], "BR": ["br"], "PH": ["ph"],
      "ID": ["id"], "TH": ["th"], "VI": ["vn"], "JP": ["jp"], "KR": ["kr"], "HK": ["hk"], "MA": ["mt", "my"],
      "ASIA": ["in", "pk"], "AFR": ["ng", "ke", "za", "gh"], "IN": ["in"], "SG": ["sg"], "ZA": ["za"]}
# sub-country inside a regional group, from the category name ("LA| COLOMBIA")
SUBCOUNTRY = {"COLOMBIA": "co", "COSTA RICA": "cr", "ECUADOR": "ec", "GUATEMALA": "gt", "HONDURAS": "hn",
              "PANAMA": "pa", "PERU": "pe", "CHILE": "cl", "ARGENTINA": "ar", "URUGUAY": "uy", "PARAGUAY": "py",
              "VENEZUELA": "ve", "BOLIVIA": "bo", "DOMINICAN": "do", "PUERTO RICO": "pr", "EL SALVADOR": "sv",
              "NICARAGUA": "ni", "JAMAICA": "jm", "PAKISTAN": "pk", "INDIA": "in", "BANGLA": "bd", "SRI LANKA": "lk",
              "NEPAL": "np", "NIGERIA": "ng", "KENYA": "ke", "GHANA": "gh", "SOUTH AFRICA": "za", "MALAYSIA": "my",
              "MALTA": "mt", "EGYPT": "eg", "SAUDI": "sa", "QATAR": "qa", "UAE": "ae", "EMIRATES": "ae",
              "LEBANON": "lb", "MOROCCO": "ma", "ALGERIA": "dz", "TUNISIA": "tn", "IRAQ": "iq", "JORDAN": "jo",
              "KUWAIT": "kw", "OMAN": "om", "SYRIA": "sy", "BEIN": "qa"}


def guide_suffixes(cat):
    cc = re.split(r"[|:]", cat)[0].strip().upper()
    sfx = list(CC.get(cc, []))
    up = cat.upper()
    for word, code in SUBCOUNTRY.items():
        if word in up:
            sfx = [code] + [x for x in sfx if x != code]
    return sfx


SUPERSCRIPT = re.compile(r"[ᴀ-ᶿʰ-˿⁰-₟◉⚽]")


def strip_prefix(n):
    n = re.sub(r"^\s*[^:|]{1,12}[:|]\s*", "", n or "")
    return SUPERSCRIPT.sub("", n).strip()


# When one stream name is in several countries' folders, TiviMate can only show one guide for
# it (matching is by name). Owner rule: English wins, so these folders win name clashes.
ENGLISH_RANK = {"US": 0, "UK": 1, "CA": 2, "AU": 3, "IE": 4, "NZ": 5}

# Quality/marketing tags that do not change which channel it is (step 2: copies of channels
# we already have). East/West, +1, GOLD and bracketed cities are NOT here on purpose.
QUALITY = re.compile(r"(?:\s*(?:FHD|UHD|HD|SD|4K|8K|HEVC|H265|H\.265|RAW|VIP|BACKUP|ALT|LQ|HQ|\d+\s?FPS|◉|⚽))+\s*$", re.I)


# "MAIN EVENT" is a channel (Sky Sports Main Event), not an event-only feed (2026-10-30 ac)
SKIP_COPY = re.compile(r"PPV|(?<!MAIN )EVENT|#####|NO EVENT|\bLIVE\s*[|:]|RADIO|\bFM\b", re.I)
# 2026-10-30 ac: "#### CBS ####" rows are list dividers, not channels (they took WCBS listings), and
# "*.Dummy.*" ids in the free guide are filler blocks ("PPV EVENTS", "Love Nature"), not schedules.
HEADING = re.compile(r"^\s*#{2,}")


# US streaming-app lines and the country-less id endings their channels carry in the free guide
APP_LINE = re.compile(r"^\s*(?:AT&T|PRIME|RK|ROKU|TUBI|PEACOCK|TV)\s*[:|]", re.I)
NO_COUNTRY = ("plex", "distro", "peacock")
UHD_FOLDERS = {"4K"}
# a 4K-folder stream whose listings are an English channel's (by a name hint) is English for the fill
ENGLISH_ID = re.compile(r"\.(?:us|uk|ca|au|ie|nz)\d?$", re.I)


def is_dummy(cid):
    return ".dummy." in (cid or "").lower()


# call sign -> ["ST:city", ...] from the tvpassport/tvtv station lists (2026-10-30)
try:
    CALLSIGN_CITY = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "callsign_city.json"),
                                   encoding="utf-8"))
except (OSError, ValueError):
    CALLSIGN_CITY = {}


def tag_conflict(sname, cid):
    """2026-10-30 ac: the provider's tag names another channel number or call sign than the stream
    itself ("TSN 4" -> TSN2.ca, "DAZN LA LIGA 1" -> daznlaliga2.es, "FOX 4 (KDFW)" -> KDFI.us)."""
    n = strip_prefix(sname)
    n = re.sub(r"\+\s?\d+\b|\b\d{3,4}p\b|\b[48]k\b|\b\d+\s?fps\b", " ", n, flags=re.I)
    stem = re.sub(r"\.[A-Za-z_]{2,12}\d?$", "", re.sub(r"~\d+$", "", cid or ""))   # drop ".uk", ".us_locals1"
    stem = re.sub(r"(\+|plus|minus)\d+$", "", stem, flags=re.I)                     # "+1" timeshift
    # US locals: only when both call signs are known stations in different cities (sister stations and
    # renamed ones share a city: WGCL -> WANF Atlanta, WABM -> WBMA Birmingham)
    cs = re.search(r"\(([KW][A-Z]{2,3})(?:-[A-Z0-9]+)?\)", n)
    if cs and re.fullmatch(r"[KW][A-Z]{2,3}(DT\d*|TV|LD|CD)?", stem, re.I) and not stem.upper().startswith(cs.group(1)):
        here = CALLSIGN_CITY.get(cs.group(1))
        there = next((CALLSIGN_CITY[k] for k in (stem.upper()[:4], stem.upper()[:3]) if k in CALLSIGN_CITY), None)
        if here and there and not set(here) & set(there):
            return True
    a, m = re.findall(r"\d+", n), re.search(r"(\d+)$", stem)
    if a and m and not re.fullmatch(r"[KW][A-Z]{2,3}(DT|TV|LD|CD)\d*", stem, re.I):   # WCHSDT2 = subchannel
        x, y = a[-1], m.group(1)
        return not (x.endswith(y) or y.endswith(x))
    return False


def base_name(n):
    """Channel identity without folder prefix and quality tags: 'AT&T: FOX NEWS ᴿᴬᵂ' -> 'foxnews'."""
    x = strip_prefix(n)
    prev = None
    while prev != x:
        prev, x = x, QUALITY.sub("", x).strip()
    return b.name_key(x)


COUNTRY_WORDS = {"FRANCE": "fr", "GERMANY": "de", "DEUTSCH": "de", "ESPANA": "es", "SPAIN": "es", "ITALIA": "it",
                 "TURK": "tr", "POLSKA": "pl", "ENGLISH": None}


def name_country(n):
    """Country the stream NAME itself claims ('UK: ZEE TV' -> 'uk', 'beIN 2 FRANCE' -> 'fr'), else None."""
    up = (n or "").upper()
    for word, code in COUNTRY_WORDS.items():
        if code and word in up:
            return code
    m = re.match(r"\s*([A-Za-z]{2,4})\s*[:|]", n or "")
    if m and m.group(1).upper() in CC:
        return CC[m.group(1).upper()][0]
    return None


def folder(cat):
    return re.split(r"[|:]", cat or "")[0].strip().upper()


def slug(n):
    s = re.sub(r"[^a-z0-9]+", "", SUPERSCRIPT.sub("", n or "").lower())
    return s[:60] or "channel"


def suffix(k):
    m = re.search(r"\.([A-Za-z0-9_]+)$", k or "")
    return m.group(1).lower() if m else ""


def load_json(src):
    return json.loads(b.read_bytes(src))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--live", required=True)
    ap.add_argument("--cats", required=True)
    ap.add_argument("--provider-xmltv", required=True)
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--map", help="map.json {stream name: id in the 'grab' source} from make_grab_map.py")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.monotonic()
    rep = {"built_at": NOW.isoformat(timespec="seconds")}

    live = load_json(a.live)
    cats = {str(c["category_id"]): c["category_name"] for c in load_json(a.cats)}
    rep["provider_streams"] = len(live)

    provider = b.Source("provider", b.local_copy(a.provider_xmltv, a.out))
    others = []
    for spec in a.source:
        name, _, loc = spec.partition("=")
        try:
            others.append(b.Source(name, b.local_copy(loc, a.out)))
        except Exception as e:  # noqa: BLE001  a missing extra source must not stop the build
            rep.setdefault("source_errors", {})[name] = str(e)[:200]

    def index(src):
        idx = collections.defaultdict(set)
        for k, names in src.chans.items():
            stem = re.sub(r"\.[A-Za-z0-9_]+$", "", k).replace(".", " ")
            keys = {b.name_key(stem)} | {b.name_key(strip_prefix(n)) for n in names}
            if suffix(k) == "uk":
                # UKTV renamed its channels "U&Dave", "U&Drama" ... (2024); the line still says "DAVE"
                keys |= {x[4:] for x in keys if x.startswith("uand") and len(x) > 4}
            for key in keys:
                if key:
                    idx[(suffix(k), key)].add(k)
        return idx

    grab = next((x for x in others if x.name == "grab"), None)
    others = [x for x in others if x.name != "grab"]
    # mjh.nz channels chosen by make_grab_map.py live in grab/native.xml.gz (ids g.mjh.*)
    native = None
    if grab and os.path.exists(os.path.join("grab", "native.xml.gz")):
        try:
            native = b.Source("native", os.path.join("grab", "native.xml.gz"))
        except Exception as e:  # noqa: BLE001
            rep.setdefault("source_errors", {})["native"] = str(e)[:200]
    smap = {}
    if a.map and os.path.exists(a.map):
        smap = json.load(open(a.map, encoding="utf-8"))
    rep["grab_map_entries"] = len(smap)
    idx = {s.name: index(s) for s in [provider] + others}
    # name_hints.json (next to this script): {exact stream name: source channel id}, learned once
    # from which channel myepg shows for a name (2026-10-27), filtered so the id's words and
    # numbers all appear in the name. Listings still come from our own free sources.
    hints = {}
    hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "name_hints.json")
    if os.path.exists(hp):
        hints = json.load(open(hp, encoding="utf-8"))
    rep["name_hints"] = len(hints)

    # one guide channel per real source channel, or per unique stream name (placeholder)
    real = {}          # (source name, source id) -> {"id":..., "names": set(), "icon":...}
    placeholder = {}   # slug -> {"id":..., "names": set(), "icon":..., "title":...}
    how = collections.Counter()
    choice = {}        # stream name -> (hit or None, epg id, icon); one entry per exact name
    # Provider tags shared by many unrelated channels are junk (2026-10-27: tag "TS" sat on 448
    # streams - BBC 3, ITV London ... - and carried a Belgian channel's listings). Ignore them.
    tag_names = collections.defaultdict(set)
    for s in live:
        if (s.get("epg_channel_id") or "").strip():
            tag_names[s["epg_channel_id"].strip()].add(base_name(s.get("name") or ""))
    junk_tags = {t for t, names in tag_names.items() if len(names) > 25}
    rep["ignored_provider_tags"] = {t: len(tag_names[t]) for t in sorted(junk_tags)}
    for s in live:
        sname = (s.get("name") or "").strip()
        if not sname:
            continue
        cat = cats.get(str(s.get("category_id")), "")
        icon = s.get("stream_icon") or ""
        eid = (s.get("epg_channel_id") or "").strip()
        hit = None
        sfx = guide_suffixes(cat)
        if eid and eid not in junk_tags:
            pid = provider.by_id.get(eid.lower())
            if pid and provider.hours_ahead(pid) >= MIN_REAL_HOURS and not tag_conflict(sname, pid):
                hit = ("provider", pid, "provider:id")
        # The provider's own tag sometimes points at another country's channel
        # ("US: THE WEATHER CHANNEL" -> TheWeatherNetwork.ca). Then a same-country name match wins.
        wrong_country = bool(hit and sfx and re.fullmatch(r"[a-z]{2}", suffix(hit[1]))
                             and not any(suffix(hit[1]) == x or x.startswith(suffix(hit[1])) for x in sfx)
                             and suffix(hit[1]) != name_country(sname))
        if not hit or wrong_country:
            key = b.name_key(strip_prefix(sname))
            for src in others + [provider]:
                cands = {c for x in sfx for c in idx[src.name].get((x, key), ())
                         if src.hours_ahead(c) >= MIN_REAL_HOURS and not is_dummy(c) and not tag_conflict(sname, c)}
                if len(cands) == 1:
                    hit = (src.name, cands.pop(), src.name + (":name" if not wrong_country else ":name(country fix)"))
                    break
        if not hit and sname in hints:
            for src in [provider] + others:
                hid = src.by_id.get(hints[sname].lower())
                if hid and src.hours_ahead(hid) >= MIN_REAL_HOURS and not is_dummy(hid):
                    hit = (src.name, hid, "hint:" + src.name)
                    break
        if not hit and grab and sname in smap:
            gid = smap[sname]
            if tag_conflict(sname, gid):
                pass                     # grabber channel numbered differently than the stream
            elif gid.startswith("g.mjh."):
                if native and native.hours_ahead(gid) >= MIN_REAL_HOURS:
                    hit = ("native", gid, "native:map")
            elif grab.hours_ahead(gid) >= MIN_REAL_HOURS:
                hit = ("grab", gid, "grab:map")
        # The free guide files streaming-app channels with no country ("plex.tv.Lawless.plex",
        # "Schwab.Network.distro", "[NBCNN].NBC.News.NOW.PEACOCK"), so the country name pass never
        # reaches them (2026-10-31 ac). Only the US app lines (AT&T/PRIME/ROKU/TUBI ...), exact key,
        # one candidate; the hours check drops "To Be Announced"-only channels.
        if not hit and folder(cat) == "US" and APP_LINE.match(sname):
            key = b.name_key(strip_prefix(sname))
            for src in others:
                cands = {c for x in NO_COUNTRY for c in idx[src.name].get((x, key), ())
                         if src.hours_ahead(c) >= MIN_REAL_HOURS and not is_dummy(c)} if len(key) >= 4 else set()
                if len(cands) == 1:
                    hit = (src.name, cands.pop(), src.name + ":name(app)")
                    break
        # TiviMate 5.1.0 (A/B 2026-10-25): exact names, ALL display-names checked, and when two
        # channels in one file carry the same name the LAST one wins. So every stream name is
        # emitted exactly once: the first real listing found wins over any placeholder.
        if HEADING.match(sname):
            hit = None
        rank = ENGLISH_RANK.get(folder(cat), 9)
        prev = choice.get(sname)
        # English folders decide a shared name outright (even if that means its own placeholder
        # instead of another country's listings); otherwise the first real listing wins.
        if prev is None or rank < prev[3] or (rank == prev[3] and prev[0] is None and hit):
            choice[sname] = (hit, eid, icon, rank, folder(cat), cat)

    # Step 2: a placeholder that is a copy of a channel we already have a guide for, in the same
    # country folder, under another prefix or quality ("AT&T: FOX NEWS ᴿᴬᵂ" = "US: FOX NEWS HD").
    # Only when every real copy in that folder points at the same listings.
    sib = collections.defaultdict(set)
    sib_canon = collections.defaultdict(set)
    sib_how = {}
    for sname, (hit, eid, icon, rank, fold, cat) in choice.items():
        if hit:
            sib[(fold, base_name(sname))].add((hit[0], hit[1]))
            if re.match(re.escape(fold) + r"\s*[:|]", sname):     # the folder's own label ("US: ESPN HD")
                sib_canon[(fold, base_name(sname))].add((hit[0], hit[1]))
            sib_how[(hit[0], hit[1])] = hit[2]
    copy_how = collections.Counter()
    for sname, (hit, eid, icon, rank, fold, cat) in list(choice.items()):
        if hit or SKIP_COPY.search(sname):
            continue
        k = base_name(sname)
        cands = sib.get((fold, k), set())
        src_fold = fold
        if not cands and fold in UHD_FOLDERS:
            # the provider's all-4K folder ("4K| UHD 3840P": Sportsnet 360, ESPN, QVC) has no country,
            # so it copies from the English folders and then counts as that folder for the myepg fill.
            # Only this folder: other country-less ones ("8K| SPORT ON AIR", "LA| GENERAL") hold
            # foreign feeds of the same brand (DE Sky Sport, FR beIN, Latin American HBO).
            by_fold = {f: sib[(f, k)] for f in ENGLISH_RANK if sib.get((f, k))}
            cands = set().union(*by_fold.values()) if by_fold else set()
            if len(by_fold) == 1 and len(cands) > 1:
                # one English folder, two sources for one channel ("US: ESPN HD" -> ESPN.us from the
                # provider, "AT&T: ESPN" -> ESPN.HD.us2): take the one its "US:"-labelled copy uses
                f = next(iter(by_fold))
                if len(sib_canon.get((f, k), ())) == 1:
                    cands = set(sib_canon[(f, k)])
            if len(cands) == 1:
                src_fold = min(by_fold, key=ENGLISH_RANK.get)
        if len(k) >= 3 and len(cands) == 1:
            src_name, sid = next(iter(cands))
            choice[sname] = ((src_name, sid, "copy:" + src_name), eid, icon, rank, src_fold, cat)
            copy_how[sib_how.get((src_name, sid), "?")] += 1
    for sname, (hit, eid, icon, rank, fold, cat) in choice.items():
        if hit:
            k = (hit[0], hit[1])
            ch = real.setdefault(k, {"id": eid or hit[1], "names": set(), "icon": icon})
            ch["names"].add(sname)
            how[hit[2]] += 1
        else:
            k = slug(sname)
            pid_ = eid if eid and eid not in junk_tags else "ph." + k
            ch = placeholder.setdefault(k, {"id": pid_, "names": set(), "icon": icon,
                                            "plan": smart_ph.plan(sname, cat, NOW)})
            ch["names"].add(sname)
            how["placeholder"] += 1
    if os.environ.get("OWN_EPG_DUMP"):
        with open(os.environ["OWN_EPG_DUMP"], "w", encoding="utf-8") as f:
            for sname, (hit, eid, icon, rank, fold, cat) in choice.items():
                f.write("\t".join([sname, fold, hit[2] if hit else "placeholder", hit[1] if hit else ""]) + "\n")
    rep["streams_by_listing_type"] = dict(how)
    rep["copies_by_source_method"] = dict(copy_how)
    rep["unique_stream_names"] = len(choice)
    rep["guide_channels_real"] = len(real)
    rep["guide_channels_placeholder"] = len(placeholder)

    # make ids unique
    used = collections.Counter()
    for ch in list(real.values()) + list(placeholder.values()):
        used[ch["id"]] += 1
        if used[ch["id"]] > 1:
            ch["id"] = f'{ch["id"]}~{used[ch["id"]]}'

    out_parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="CTVU own EPG">\n']
    for ch in list(real.values()) + list(placeholder.values()):
        names = "".join(f"<display-name>{escape(n)}</display-name>" for n in sorted(ch["names"]))
        icon = f"<icon src={quoteattr(ch['icon'])} />" if ch["icon"] else ""
        out_parts.append(f"<channel id={quoteattr(ch['id'])}>{names}{icon}</channel>\n")
    body = ["".join(out_parts).encode("utf-8")]

    after = NOW - datetime.timedelta(hours=PAST_HOURS)
    n_prog = 0
    for src in [provider] + others + ([grab] if grab else []) + ([native] if native else []):
        wanted = {sid: (ch["id"], after) for (sn, sid), ch in real.items() if sn == src.name}
        progs = src.programmes_for(wanted)
        n_prog += len(progs)
        body.append(b"\n".join(progs) + b"\n")

    # placeholders, aligned to whole blocks so consecutive builds produce the same slots
    step = datetime.timedelta(hours=PLACEHOLDER_HOURS)
    epoch = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    first = epoch + ((after - epoch) // step) * step
    slots = []
    t = first
    while t < NOW + datetime.timedelta(hours=AHEAD_HOURS):
        slots.append((b.fmt_ts(t), b.fmt_ts(t + step)))
        t += step
    grid = [first + i * step for i in range(len(slots) + 1)]
    ph = []
    n_events = 0

    def emit(cid, st, sp, title, desc=None):
        d = f"<desc>{escape(desc)}</desc>" if desc else ""
        ph.append(f'<programme start="{b.fmt_ts(st)}" stop="{b.fmt_ts(sp)}" channel={cid}>'
                  f'<title>{escape(title)}</title>{d}</programme>')

    for ch in placeholder.values():
        cid, p = quoteattr(ch["id"]), ch["plan"]
        evs = [(max(s0, first), min(s1, grid[-1]), t) for s0, s1, t in p["events"] if s1 > first and s0 < grid[-1]]
        if not evs:
            for st, sp in zip(grid, grid[1:]):
                emit(cid, st, sp, p["title"], p["desc"])
            continue
        # one timed event: filler on the 6h grid before and after it, cut at the event edges,
        # so the channel's timeline has no gaps and no overlaps
        s0, s1, t = evs[0]
        n_events += 1
        cuts = sorted({first, s0, s1, grid[-1]} | {g for g in grid if first < g < grid[-1]})
        for st, sp in zip(cuts, cuts[1:]):
            if st >= s0 and sp <= s1:
                emit(cid, st, sp, t, "Live event")
            else:
                emit(cid, st, sp, p["before"] if sp <= s0 else p["after"])
    rep["placeholder_events"] = n_events
    body.append("\n".join(ph).encode("utf-8"))
    body.append(b"\n</tv>\n")
    final = b"".join(body)
    rep["programmes_real"] = n_prog
    rep["programmes_placeholder"] = len(ph)
    rep["xml_mb"] = round(len(final) / 1e6, 1)

    problems = []
    if len(real) + len(placeholder) < 0.8 * len(live) / 1.3:
        problems.append("too few channels")
    if n_prog < 50000:
        problems.append(f"only {n_prog} real programmes")
    rep["problems"] = problems
    if not problems:
        path = os.path.join(a.out, "epg.xml.gz")
        with open(path + ".tmp", "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as g:
            g.write(final)
        os.replace(path + ".tmp", path)
        rep["gz_mb"] = round(os.path.getsize(path) / 1e6, 1)
        # ids of channels with real listings, for the gap fill in the myepg feeds (fill_gaps.py);
        # the sha256 lets the reader check both files come from the same build
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(a.out, "real_ids.json"), "w", encoding="utf-8") as f:
            json.dump({"epg_sha256": sha, "built_at": rep["built_at"],
                       "ids": sorted(ch["id"] for ch in real.values()),
                       # stream names in the English folders: the English feeds fill only these,
                       # the EU feeds only the rest
                       "english_names": sorted(n for n, c in choice.items() if c[4] in ENGLISH_RANK
                                               or (c[4] in UHD_FOLDERS and c[0] and ENGLISH_ID.search(c[0][1])))}, f)
    rep["seconds"] = round(time.monotonic() - t0, 1)
    json.dump(rep, open(os.path.join(a.out, "report.json"), "w"), indent=1)
    print(json.dumps(rep, indent=1))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
