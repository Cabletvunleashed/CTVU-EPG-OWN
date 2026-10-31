#!/usr/bin/env python3
"""
Match provider streams that only have a placeholder to channels in free
iptv-org/epg grabber channel lists. Writes:
  grab/custom.channels.xml  - what the grabber should fetch (xmltv_id = our id)
  grab/map.json             - {exact stream name: grabbed xmltv_id} for own_epg.py

Usage: make_grab_map.py --live live.json --cats cats.json --current epg.xml.gz
                        --lists DIR --out DIR
"""
import argparse
import collections
import glob
import gzip
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_epg as b  # noqa: E402
from own_epg import strip_prefix, guide_suffixes  # noqa: E402
import callsign  # noqa: E402
import citymatch  # noqa: E402
import urllib.request  # noqa: E402

# Static free XMLTV files (i.mjh.nz, no login), matched by display-name. Written into
# grab/native.xml.gz with our own ids, read by own_epg.py next to the grabbed guide.
MJH = {"mjh.roku": "https://i.mjh.nz/Roku/all.xml.gz", "mjh.samsung": "https://i.mjh.nz/SamsungTVPlus/us.xml.gz",
       "mjh.plex": "https://i.mjh.nz/Plex/us.xml.gz", "mjh.pluto": "https://i.mjh.nz/PlutoTV/us.xml.gz"}
NATIVE_OUT = os.path.join("grab", "native.xml.gz")


def load_mjh():
    """-> ({tag: {key: [(tag, channel_id, lang, name)]}}, {tag: raw xml text})"""
    lists, raws = {}, {}
    for tag, url in MJH.items():
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "CTVU-EPG-OWN/1.0 (+https://github.com/Cabletvunleashed/CTVU-EPG-OWN)"})
            data = urllib.request.urlopen(req, timeout=120).read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            text = data.decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001  one file down must not stop the others
            print(f"{tag}: download failed ({type(e).__name__})")
            continue
        import html as _h
        idx = collections.defaultdict(list)
        for cid, body in re.findall(r'<channel id="([^"]*)"[^>]*>(.*?)</channel>', text, re.S):
            for nm in re.findall(r"<display-name[^>]*>(.*?)</display-name>", body):
                nm = _h.unescape(nm).strip()
                k = key(nm)
                if len(k) >= 4:
                    idx[k].append((tag, cid, "en", nm))
        lists[tag], raws[tag] = idx, text
    return lists, raws


def write_native(chosen, raws, path):
    """Copy the chosen mjh channels' programmes, renamed to our ids, into one XMLTV file."""
    want = {}
    for (site, sid), e in chosen.items():
        if site in raws:
            want.setdefault(site, {})[sid] = e["id"]
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n']
    for site, ids in want.items():
        for sid, our in ids.items():
            out.append(f'<channel id={quoteattr(our)}><display-name>{escape(our)}</display-name></channel>\n')
    n = 0
    for site, ids in want.items():
        for m in re.finditer(r'<programme\b[^>]*\bchannel="([^"]*)"[^>]*>.*?</programme>', raws[site], re.S):
            if m.group(1) in ids:
                prog = m.group(0)
                out.append(prog.replace(f'channel="{m.group(1)}"', f'channel={quoteattr(ids[m.group(1)])}', 1) + "\n")
                n += 1
    out.append("</tv>\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as g:
        g.write("".join(out).encode("utf-8"))
    os.replace(path + ".tmp", path)
    return n

# provider group -> grabber lists to try, best first (bulk APIs before per-channel scrapers).
# tvtv.us left out: it blocks the grabber (iptv-org/epg #3187, every request 404) and labels
# US Eastern times as UTC (#3147). Add it back once upstream fixes both.
SITES = {
    "US": ["directv.com", "plex.tv_us", "pluto.tv_us", "tvpassport.com",
           "mjh.roku", "mjh.samsung", "mjh.plex", "mjh.pluto"],
    "CA": ["tvpassport.com", "plex.tv_ca", "pluto.tv_ca"],
    "UK": ["plex.tv_uk", "pluto.tv_uk"],
    "AU": ["plex.tv_au", "pluto.tv_au"],
    "IE": ["plex.tv_uk", "pluto.tv_uk"],
}
CS_SITES = ("tvpassport.com",)   # call-sign lists (tvtv.us out, see above)
SKIP = re.compile(r"PPV|\bLIVE\s*\||EVENT|^#####|NO EVENT|ᴰᴼᵂᴺ", re.I)


def key(name):
    n = re.sub(r"\([^)]*\)", " ", name or "")
    return b.name_key(n)


TAIL = re.compile(r"(tv|hd)$")
# QUAL minus the feed words: ALT/BACKUP name a different feed (NFL Network Alt is not NFL Network)
_QUAL_ALT = []
# Keys held out of the last pass until checked against the channel's own schedule. Empty since
# 2026-10-30: GET (plex = GetTV, airs The Rockford Files) was confirmed by REA.
ALT_HOLD = set()


def alt_key(name):
    """Second-chance key (2026-10-30 ac), used only by the last pass: quality tags and a trailing
    'TV' dropped on both sides ('AXS' = 'AXS TV HD', 'BOUNCE' = 'BounceTV'); '+' kept as 'plus'
    so AMC+ never meets AMC. 'Channel'/'Network' are kept (NFL Network is not NFL Channel)."""
    if not _QUAL_ALT:
        _QUAL_ALT.append(re.compile(QUAL.pattern.replace("BACKUP|ALT|", ""), re.I))
    x, prev = strip_prefix(name or ""), None
    while prev != x:
        prev, x = x, _QUAL_ALT[0].sub("", x).strip()
    x = re.sub(r"\s*\+", " plus", re.sub(r"\([^)]*\)", " ", x))
    k = b.name_key(x)
    while TAIL.search(k) and len(TAIL.sub("", k)) >= 3:
        k = TAIL.sub("", k)
    return k


def fold_key(name):
    """key() with accents folded first ("RTÉ News" -> rtenews, not rtnews = RT News). Only the sky.com
    name pass uses it: folding in the shared name_key made "Cinemáx" (Spanish) meet "Cinemax" (A/B 10-31)."""
    return key("".join(c for c in unicodedata.normalize("NFD", name or "") if not unicodedata.combining(c)))


SYN = re.compile(r"^\s*SYN\s*:", re.I)
HD_TAIL = re.compile(r"\s+HD$", re.I)


def one_of_sd_hd(hits):
    """{(site, sid): row} -> the same dict, or one row when the hits are only the SD and HD rows of one
    channel ("Madison Square Garden" / "Madison Square Garden HD"): same schedule, take the HD row
    (2026-10-31 ac; same rule as citymatch._one)."""
    if len(hits) != 2 or len({HD_TAIL.sub("", x[3]).strip().lower() for x in hits.values()}) != 1:
        return hits
    hd = [k for k, x in hits.items() if HD_TAIL.search(x[3])]
    return {hd[0]: hits[hd[0]]} if len(hd) == 1 else hits   # two rows of the very same name: refuse


# Regional sports networks renamed since the provider named its streams (2026-10-31 ac): FOX Sports ->
# Bally Sports (2021) -> FanDuel Sports Network (2024); AT&T SportsNet Southwest -> Space City Home
# Network (2023); AT&T SportsNet Pittsburgh -> SportsNet Pittsburgh (2023); NBC Sports Chicago ->
# Chicago Sports Network (2024). One-to-one only: Tennessee (South or Southeast?), SportsTime Ohio /
# Great Lakes, Arizona and the "PL"/Plus feeds have no single successor and stay as they are.
RSN_RENAMES = {
    "foxsportswest": "FanDuel Sports Network West", "ballysportswest": "FanDuel Sports Network West",
    "foxsportsprimeticket": "FanDuel Sports Network SoCal", "ballysportsprimeticket": "FanDuel Sports Network SoCal",
    "foxsportssouthwest": "FanDuel Sports Network Southwest",
    "foxsportsmidwest": "FanDuel Sports Network Midwest",
    "foxsportskansascity": "FanDuel Sports Network Kansas City",
    "foxsportscarolinas": "FanDuel Sports Network South- Carolinas",
    "attsportsnetsouthwest": "Space City Home Network",
    "attsportsnetpittsburgh": "SportsNet Pittsburgh",
    "nbcsportschicago": "Chicago Sports Network", "nbcsportschicagoplus": "Chicago Sports Network Plus",
}


def load_lists(d):
    lists = {}
    for f in glob.glob(os.path.join(d, "grab_*.channels.xml")):
        tag = os.path.basename(f)[5:-len(".channels.xml")]          # e.g. plex.tv_us, directv.com
        rows = re.findall(r'<channel site="([^"]+)" site_id="([^"]*)" lang="([^"]*)"[^>]*>([^<]+)</channel>',
                          open(f, encoding="utf-8", errors="ignore").read())
        idx = collections.defaultdict(list)
        for site, sid, lang, nm in rows:
            if SYN.match(nm):
                continue        # tvpassport "SYN: Storage Wars" = one show's syndicated airings, not a channel
            k = key(nm.strip())
            if len(k) >= 4:
                idx[k].append((site, sid, lang, nm.strip()))
            ak = alt_key(nm.strip())
            if len(ak) >= 3:
                idx["~" + ak].append((site, sid, lang, nm.strip()))
        lists[tag] = idx
    return lists


# ---- iptv-org database pass (any country) --------------------------------------------
# Last resort for streams still on a placeholder: match the stream to an iptv-org channel
# (name or alt name, in the folder's country), then grab it from a working iptv-org site.
# One-or-none, like every other pass: anything ambiguous stays a placeholder.
API_SKIP = re.compile(r"PPV|\bLIVE\s*\||EVENT|^#####|NO EVENT|ᴰᴼᵂᴺ|RADIO|24/7|24-7|\+\s?\d\b", re.I)
API_EXCLUDE_SITES = {"tvtv.us"}   # blocked upstream (#3187) and wrong time zone (#3147)
API_MAX_PER_SITE = 300            # one broken site must not dominate the grab
API_MAX_TOTAL = 2500
API_BUDGET_SECONDS = 1500         # extended grab runs under its own 25-min timeout
QUAL = re.compile(r"(?:\s*(?:ᴴᴰ|ᶠᴴᴰ|ᵁᴴᴰ|ᴿᴬᵂ|⁴ᴷ|⁸ᴷ|ⱽᴵᴾ|ᴳᴼᴸᴰ|⁶⁰ᶠᵖˢ|ʰᵉᵛᶜ|◉|FHD|UHD|HD|SD|4K|8K|HEVC|H265|RAW|VIP|"
                  r"GOLD|BACKUP|ALT|LQ|HQ|\d+\s?FPS|\*[^*]*\*|\[[^\]]*\]))+\s*$", re.I)


def base_key(name):
    x, prev = strip_prefix(name), None
    while prev != x:
        prev, x = x, QUAL.sub("", x).strip()
    return key(x)


def countries(cat, grp):
    """Folder -> iptv-org country codes (ISO, lower case)."""
    if grp == "US":
        return ["us"]
    out = []
    for x in guide_suffixes(cat):
        x = {"uk": "gb"}.get(x, x)
        if len(x) == 2 and x not in out:
            out.append(x)
    return out


def load_api(d, sites_dir=None):
    chans = json.load(open(os.path.join(d, "channels.json"), encoding="utf-8"))
    guides = json.load(open(os.path.join(d, "guides.json"), encoding="utf-8"))
    feeds = json.load(open(os.path.join(d, "feeds.json"), encoding="utf-8"))
    sites = {}
    for site, n, st, note in re.findall(r'<a href="sites/([^"]+)">[^<]*</a></td><td[^>]*>(\d*)</td>'
                                        r'<td[^>]*>([^<]*)</td><td>(.*?)</td>',
                                        open(os.path.join(d, "SITES.md"), encoding="utf-8").read()):
        if st.strip() == "🟢" and not note.strip() and site not in API_EXCLUDE_SITES:
            sites[site] = int(n or 0)
    delay = {}
    for site in sites:
        f = os.path.join(sites_dir or "", site, site + ".config.js")
        m = re.search(r"delay:\s*(\d+)", open(f, encoding="utf-8").read()) if sites_dir and os.path.exists(f) else None
        delay[site] = int(m.group(1)) / 1000 if m else 0
    main_feed = {f["channel"]: f["id"] for f in feeds if f.get("is_main")}
    feed_name = {(f["channel"], f["id"]): (f["id"] + " " + f.get("name", "")).lower() for f in feeds}
    idx = collections.defaultdict(set)
    meta = {}
    for c in chans:
        if c.get("closed") or c.get("is_nsfw"):
            continue
        cc = (c.get("country") or "").lower()
        meta[c["id"]] = c
        for nm in [c["name"]] + list(c.get("alt_names") or []):
            k = base_key(nm)
            if len(k) >= 3:
                idx[(cc, k)].add(c["id"])
    gby = collections.defaultdict(list)
    for g in guides:
        if g.get("channel") in meta and g.get("site") in sites and g.get("site_id"):
            gby[g["channel"]].append(g)
    return {"idx": idx, "meta": meta, "guides": gby, "sites": sites, "delay": delay,
            "main_feed": main_feed, "feed_name": feed_name}


def api_guide(api, cid, stream_name, ccs):
    """Best working guide for channel cid, right feed only (East/West have different times)."""
    west = re.search(r"\b(WEST|PACIFIC)\b", stream_name, re.I)
    ok = []
    for g in api["guides"].get(cid, []):
        feed = g.get("feed")
        if west:
            if not feed or not re.search(r"west|pacific", api["feed_name"].get((cid, feed), ""), re.I):
                continue
        elif feed and feed != api["main_feed"].get(cid):
            continue
        ok.append(g)
    if not ok:
        return None
    tld = lambda s: s.rsplit(".", 1)[-1]  # noqa: E731
    # prefer a site from the channel's own country, then the bigger (better maintained) site
    ok.sort(key=lambda g: (tld(g["site"]) not in ccs + (["uk"] if "gb" in ccs else []),
                           -api["sites"].get(g["site"], 0), g["site"], g["site_id"]))
    return ok[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", required=True)
    ap.add_argument("--cats", required=True)
    ap.add_argument("--current", required=True, help="current own epg.xml.gz (to know which streams are placeholders)")
    ap.add_argument("--lists", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-native", action="store_true", help="skip the mjh.nz static files")
    ap.add_argument("--api", help="dir with iptv-org api channels.json, guides.json, feeds.json + SITES.md")
    ap.add_argument("--sites-dir", help="iptv-org sites/ dir, only to read per-site request delays")
    ap.add_argument("--real-ids", help="real_ids.json written with --current (ids that have real listings)")
    a = ap.parse_args()

    live = json.load(open(a.live))
    cats = {str(c["category_id"]): c["category_name"] for c in json.load(open(a.cats))}
    raw = gzip.open(a.current).read()
    # Which guide channels have real listings. A placeholder can carry the provider's tag as its id
    # ("SkyCinemaPremiere.uk" with no data), so the id alone does not say it (2026-10-30 ac: those
    # streams were never offered to the grabbers). real_ids.json says it, when it belongs to --current.
    real_ids = None
    if a.real_ids and os.path.exists(a.real_ids):
        try:
            j = json.load(open(a.real_ids, encoding="utf-8"))
            if j.get("epg_sha256") == hashlib.sha256(open(a.current, "rb").read()).hexdigest():
                real_ids = {x.encode("utf-8") for x in j.get("ids", [])}
        except (OSError, ValueError):
            real_ids = None
    stats_real = "real_ids.json" if real_ids is not None else "id prefix"
    real_names = set()
    for m in re.finditer(rb'<channel id="([^"]*)">(.*?)</channel>', raw):
        if m.group(1).startswith(b"g.") or (m.group(1) not in real_ids if real_ids is not None
                                            else m.group(1).startswith(b"ph.")):
            continue                                       # placeholder or grab-filled: keep them mapped
        for n in re.findall(rb"<display-name>(.*?)</display-name>", m.group(2)):
            real_names.add(n.decode("utf-8"))
    lists = load_lists(a.lists)
    mjh_lists, mjh_raw = ({}, {}) if a.no_native else load_mjh()
    lists.update(mjh_lists)
    cs_idx = {}
    city_idx = None
    for tag in CS_SITES:
        f = os.path.join(a.lists, f"grab_{tag}.channels.xml")
        if os.path.exists(f):
            rows = re.findall(r'<channel site="([^"]+)" site_id="([^"]*)"[^>]*>([^<]+)</channel>',
                              open(f, encoding="utf-8").read())
            cs_idx[tag] = callsign.index(rows)
            if tag == "tvpassport.com":
                city_idx = citymatch.index(rows)

    chosen = {}       # (site, site_id) -> {"id":..., "lang":..., "name":...}
    smap = {}         # stream name -> our xmltv id
    stats = collections.Counter()
    stats["real names from " + stats_real] = len(real_names)
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        if grp not in SITES or escape(name) in real_names or SKIP.search(name):
            continue
        stats["candidates"] += 1
        # local stations: call sign + network ("US: NBC 11 (WBAL) BALTIMORE" -> "NBC (WBAL-TV) Baltimore, MD")
        if grp == "US" and callsign.CS.search(name):
            for tag in CS_SITES:
                r = callsign.match(name, cs_idx.get(tag, {}))
                if r:
                    site, sid, nm = r
                    ent = chosen.setdefault((site, sid), {"id": f"g.{tag}.cs.{callsign.CS.search(nm).group(1).lower()}."
                                                               f"{key(nm)[:40]}", "lang": "en", "name": nm})
                    smap[name] = ent["id"]
                    stats[f"matched:{tag}:callsign"] += 1
                    break
            if name in smap:
                continue
        # local stations named by bare call sign or network + city ("US: ABC 7 HD [FORT MYERS]")
        if grp == "US" and city_idx:
            r = citymatch.match(name, city_idx)
            if r:
                site, sid, nm, how = r
                cs = re.search(r"\(([KW][A-Z]{2,3})", nm).group(1).lower()
                ent = chosen.setdefault((site, sid), {"id": f"g.tvpassport.com.city.{cs}.{key(nm)[:40]}",
                                                      "lang": "en", "name": nm})
                smap[name] = ent["id"]
                stats[f"matched:tvpassport.com:{how}"] += 1
                continue
        k = key(strip_prefix(name))
        if len(k) < 4:
            continue
        for tag in SITES[grp]:
            hits = {(x[0], x[1]): x for x in lists.get(tag, {}).get(k, [])}
            if len(hits) == 1:
                (site, sid), x = next(iter(hits.items()))
                # stable id (same every run): grabber list + name key
                ent = chosen.setdefault((site, sid), {"id": f"g.{tag}.{k[:50]}", "lang": x[2], "name": x[3]})
                smap[name] = ent["id"]
                stats[f"matched:{tag}"] += 1
                break
        else:
            stats["no match"] += 1

    # iptv-org database pass: every folder country, only streams still without a listing
    if a.api:
        api = load_api(a.api, a.sites_dir)
        per_site = collections.Counter()
        api_total = 0
        cost = collections.Counter()           # estimated seconds per site
        for s in live:
            name = (s.get("name") or "").strip()
            cat = cats.get(str(s.get("category_id")), "")
            grp = re.split(r"[|:]", cat)[0].strip().upper()
            if (not name or name in smap or escape(name) in real_names or s.get("is_adult")
                    or API_SKIP.search(name) or API_SKIP.search(cat) or "ADULT" in cat.upper()):
                continue
            ccs = countries(cat, grp)
            k = base_key(name)
            if not ccs or len(k) < 3:
                continue
            stats["api:candidates"] += 1
            cids = {c for cc in ccs for c in api["idx"].get((cc, k), ())}
            if len(cids) != 1:
                stats["api:ambiguous" if cids else "api:no match"] += 1
                continue
            cid = cids.pop()
            # "(WWJ)", "(TAMPICO)", "(NA)": a local station or regional/time-shifted feed of the
            # matched channel. Only accept it when the channel's own name carries the same words.
            paren = [b.name_key(x) for x in re.findall(r"\(([^)]*)\)", strip_prefix(name)) if b.name_key(x)]
            names = [b.name_key(x) for x in [api["meta"][cid]["name"]] + list(api["meta"][cid].get("alt_names") or [])]
            if paren and not all(any(p in nm for nm in names) for p in paren):
                stats["api:regional/local variant"] += 1
                continue
            g = api_guide(api, cid, name, ccs)
            if not g:
                stats["api:no working guide"] += 1
                continue
            site, sid = g["site"], g["site_id"]
            if (site, sid) not in chosen:
                step = 2 * (api["delay"].get(site, 0) + 1.0) / 10   # 2 days, ~1s/request, 10 connections
                if (per_site[site] >= API_MAX_PER_SITE or api_total >= API_MAX_TOTAL
                        or sum(cost.values()) + step > API_BUDGET_SECONDS):
                    stats["api:capped"] += 1
                    continue
                per_site[site] += 1
                api_total += 1
                cost[site] += step
                chosen[(site, sid)] = {"id": f"g.{site}.{cid.lower()}"[:90], "lang": g.get("lang") or "en",
                                       "name": g.get("site_name") or api["meta"][cid]["name"], "api": True}
            smap[name] = chosen[(site, sid)]["id"]
            stats["api:matched"] += 1
            stats[f"api:site:{site}"] += 1
        stats["api:est grab seconds"] = round(sum(cost.values()))

    # Last pass (2026-10-30 ac): streams still unmapped after every pass above get one more try
    # with alt_key, same grabber lists, same one-or-none rule. Never changes an existing mapping.
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        if (grp not in SITES or not name or name in smap or escape(name) in real_names
                or SKIP.search(name) or s.get("is_adult")):
            continue
        ak = "~" + alt_key(name)
        if len(ak) < 4 or ak[1:] in ALT_HOLD:
            continue
        for tag in SITES[grp]:
            # a prefixed list name ("SYN: Mom" = syndicated airings) is a different channel
            hits = {(x[0], x[1]): x for x in lists.get(tag, {}).get(ak, []) if not re.match(r"^[^:|]{1,12}[:|]", x[3])}
            if len(hits) == 1:
                (site, sid), x = next(iter(hits.items()))
                ent = chosen.setdefault((site, sid), {"id": f"g.{tag}.{ak[1:51]}", "lang": x[2], "name": x[3]})
                smap[name] = ent["id"]
                stats[f"matched:{tag}:alt"] += 1
                break
            if hits:
                stats["alt:ambiguous"] += 1
                break

    # Tag pass (2026-10-30 ac): UK streams still unmapped whose provider tag is the xmltv_id of exactly
    # one sky.com UK channel ("SkyCinemaPremiere.uk" = sky.com GB#4021 "SkyPremiereHD"); the names
    # differ too much to meet by name. One-or-none, never changes an existing mapping.
    # Channels renamed since the provider set its tags (2026-10-30 ac): the old tag has no listings
    # anywhere, the slot now carries the new channel (myepg shows the new channel's shows under the
    # old name too). ITVBe -> ITV Quiz 2025-06-09; Syfy UK -> Sky Sci-Fi 2022-07-26;
    # CBS Reality UK -> True Crime 2023-11-22.
    RENAMED_TAGS = {"itvbe.uk": "itvquiz.uk", "syfy.uk": "skyscifi.uk", "cbsreality.uk": "truecrime.uk"}
    tag_idx = collections.defaultdict(set)
    f = os.path.join(a.lists, "grab_sky.com.channels.xml")
    if os.path.exists(f):
        for sid, lang, xid, var, nm in re.findall(r'<channel site="sky\.com" site_id="(GB#[^"]*)" lang="([^"]*)" '
                                                  r'xmltv_id="([^"@]+)@?([^"]*)">([^<]+)</channel>',
                                                  open(f, encoding="utf-8", errors="ignore").read()):
            tag_idx[xid.lower()].add((sid, lang, nm, var.lower()))
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        tag = (s.get("epg_channel_id") or "").strip().lower()
        if grp not in ("UK", "IE") or not tag or not name or name in smap or escape(name) in real_names:
            continue
        tag = RENAMED_TAGS.get(tag, tag)
        # a +1 stream only takes a +1 listing and the other way round (times would be an hour off)
        plus1 = bool(re.search(r"\+\s?1\b", name))
        hits = {h for h in tag_idx.get(tag, set()) if (h[3] == "plus1") == plus1}
        if len(hits) > 1:        # same channel in HD and SD: same schedule, take the HD entry
            hd = {h for h in hits if h[3] == "hd"}
            hits = hd if len(hd) == 1 else hits
        if len(hits) > 1 and len({(h[2], h[3]) for h in hits}) == 1:   # regional copies, same name
            hits = {min(hits)}
        if len({h[0] for h in hits}) == 1:
            sid, lang, nm, _ = next(iter(hits))
            ent = chosen.setdefault(("sky.com", sid), {"id": f"g.sky.com.{tag[:50]}" + (".plus1" if plus1 else ""), "lang": lang or "en",
                                                        "name": html.unescape(nm)})
            smap[name] = ent["id"]
            stats["matched:sky.com:tag"] += 1

    # SD/HD pass (2026-10-31 ac): streams still unmapped because the name met a channel's SD row and its
    # HD row ("Madison Square Garden" / "... HD") take the HD row. Runs after every pass above, so it
    # never changes an existing mapping.
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        if (grp not in SITES or not name or name in smap or escape(name) in real_names or SKIP.search(name)
                or s.get("is_adult") or name.startswith("#")):
            continue
        k = key(strip_prefix(name))
        if len(k) < 4:
            continue
        for tag in SITES[grp]:
            raw_hits = {(x[0], x[1]): x for x in lists.get(tag, {}).get(k, [])}
            hits = one_of_sd_hd(raw_hits)
            if len(raw_hits) == 2 and len(hits) == 1:
                (site, sid), x = next(iter(hits.items()))
                ent = chosen.setdefault((site, sid), {"id": f"g.{tag}.{k[:50]}", "lang": x[2], "name": x[3]})
                smap[name] = ent["id"]
                stats[f"matched:{tag}:sd/hd"] += 1
                break

    # Renamed-RSN pass (2026-10-31 ac): US streams still unmapped whose name is an old regional sports
    # network name look for the current name instead (RSN_RENAMES), same lists, same one-or-none rule.
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        if grp != "US" or not name or name in smap or escape(name) in real_names or SKIP.search(name):
            continue
        new = RSN_RENAMES.get(key(strip_prefix(name)))
        if not new:
            continue
        nk = key(new)
        for tag in SITES[grp]:
            hits = one_of_sd_hd({(x[0], x[1]): x for x in lists.get(tag, {}).get(nk, [])})
            if len(hits) == 1:
                (site, sid), x = next(iter(hits.items()))
                ent = chosen.setdefault((site, sid), {"id": f"g.{tag}.{nk[:50]}", "lang": x[2], "name": x[3]})
                smap[name] = ent["id"]
                stats[f"matched:{tag}:rsn-rename"] += 1
                break
            if hits:
                stats["rsn:ambiguous"] += 1

    # sky.com name pass (2026-10-31 ac): UK/IE streams still unmapped whose name is exactly one sky.com
    # channel ("UK: SKY SHOWCASE ◉" = "Sky Showcase HD", "UK: HGTV" = "HGTV"). The tag pass above only
    # reads the provider tag. SD/HD and regional copies of one channel collapse; +1 only meets +1.
    sky_by_key = collections.defaultdict(set)
    for tag_, rows in tag_idx.items():
        for sid, lang, nm, var in rows:
            nk = fold_key(HD_TAIL.sub("", html.unescape(nm)).strip())
            if len(nk) >= 4:
                sky_by_key[nk].add((sid, lang, html.unescape(nm), var, tag_))
    for s in live:
        name = (s.get("name") or "").strip()
        grp = re.split(r"[|:]", cats.get(str(s.get("category_id")), ""))[0].strip().upper()
        if (grp not in ("UK", "IE") or not name or name in smap or escape(name) in real_names or SKIP.search(name)
                or name.startswith("#")):         # "#### DISCOVERY+ HD/4K ####" is a list divider
            continue
        plus1 = bool(re.search(r"\+\s?1\b", name))
        k = fold_key(strip_prefix(name))
        hits = {h for h in sky_by_key.get(k, set()) if (h[3] == "plus1") == plus1}
        if len({h[4] for h in hits}) != 1:      # rows of more than one sky.com channel (xmltv_id): refuse
            continue
        hd = {h for h in hits if h[3] == "hd"}
        sid, lang, nm, _, tag_ = min(hd if len(hd) == 1 else hits)
        ent = chosen.setdefault(("sky.com", sid), {"id": f"g.sky.com.{tag_[:50]}" + (".plus1" if plus1 else ""),
                                                    "lang": lang or "en", "name": nm})
        smap[name] = ent["id"]
        stats["matched:sky.com:name"] += 1

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "custom.ext.channels.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n')
        for (site, sid), e in sorted(chosen.items()):
            if e.get("api"):
                f.write(f'  <channel site={quoteattr(site)} site_id={quoteattr(sid)} lang={quoteattr(e["lang"] or "en")} '
                        f'xmltv_id={quoteattr(e["id"])}>{escape(e["name"])}</channel>\n')
        f.write("</channels>\n")
    with open(os.path.join(a.out, "custom.channels.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n')
        for (site, sid), e in sorted(chosen.items()):
            if site.startswith("mjh.") or e.get("api"):
                continue
            f.write(f'  <channel site={quoteattr(site)} site_id={quoteattr(sid)} lang={quoteattr(e["lang"] or "en")} '
                    f'xmltv_id={quoteattr(e["id"])}>{escape(e["name"])}</channel>\n')
        f.write("</channels>\n")
    json.dump(smap, open(os.path.join(a.out, "map.json"), "w"), indent=0, ensure_ascii=False)
    if mjh_raw:
        stats["native programmes"] = write_native(chosen, mjh_raw, NATIVE_OUT)
    stats["streams mapped"] = len(smap)
    stats["grabber channels"] = len(chosen)
    print(json.dumps(dict(sorted(stats.items())), indent=1))


if __name__ == "__main__":
    main()
