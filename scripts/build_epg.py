#!/usr/bin/env python3
"""
CTVU EPG builder: myepg guide + automatic gap filling.

- myepg is the main guide. Its listings always win.
- If myepg sends a cut-off file, the good part is kept (trimmed to the
  last complete programme) and channels left short are refilled from the
  previous good build.
- Real (non-placeholder) channels whose myepg guide has run out (or runs
  out within FILL_BELOW_HOURS) are topped up from other guides, only for
  the time AFTER myepg's last listing, so nothing overlaps or duplicates.
  Sources, in order: free epgshare01 guide, then the provider's own guide.
  Matching: exact channel ID first, then same-country channel name.
- Optional: drop programmes that ended more than KEEP_PAST_HOURS ago.

Writes epg.xml.gz (deterministic gzip) + report.json. Exits 1 without
writing epg.xml.gz if the result fails the same checks fetch_epg.py uses.

Usage:
  build_epg.py --myepg FILE_OR_URL --out DIR [--source FILE_OR_URL ...]
               [--previous epg.xml.gz] [--keep-past-hours N]
"""

import argparse
import collections
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request
from xml.etree.ElementTree import iterparse
from xml.sax.saxutils import escape, quoteattr

MIN_CHANNELS = int(os.environ.get("MIN_CHANNELS", "10000"))
MIN_HOURS_AHEAD = float(os.environ.get("MIN_HOURS_AHEAD", "12"))
FILL_BELOW_HOURS = float(os.environ.get("FILL_BELOW_HOURS", "6"))
FILL_AHEAD_HOURS = float(os.environ.get("FILL_AHEAD_HOURS", "30"))
DESC_MAX = int(os.environ.get("DESC_MAX", "0"))   # 0 = keep descriptions whole
MIN_REPAIRED_RATIO = float(os.environ.get("MIN_REPAIRED_RATIO", "0.8"))
ALERT_BLANK_CHANNELS = int(os.environ.get("ALERT_BLANK_CHANNELS", "250"))

# EPG_NOW (ISO time, e.g. 2026-10-31T00:00:00+00:00) pins the clock for A/B runs: every "hours ahead"
# gate reads NOW, so two unpinned runs minutes apart disagree (2026-10-31 ac/REA). Unset in the workflow.
NOW = (datetime.datetime.fromisoformat(os.environ["EPG_NOW"]) if os.environ.get("EPG_NOW")
       else datetime.datetime.now(datetime.timezone.utc))

PROG_RE = re.compile(rb"<programme\b[^>]*>.*?</programme>", re.S)
CHAN_ATTR_RE = re.compile(rb'channel="([^"]*)"')
STOP_ATTR_RE = re.compile(rb'stop="([^"]*)"')


# ── helpers ──────────────────────────────────────────────────────────────────
def parse_ts(s):
    """XMLTV time -> aware UTC datetime. Tolerates minute/second 60 and no tz."""
    if isinstance(s, bytes):
        s = s.decode("ascii", "ignore")
    s = (s or "").strip()
    try:
        t = datetime.datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                              tzinfo=datetime.timezone.utc)
        t += datetime.timedelta(hours=int(s[8:10]), minutes=int(s[10:12]),
                                seconds=int(s[12:14] or 0))
        tz = s[14:].strip()
        if tz:
            sign = -1 if tz[0] == "-" else 1
            t -= sign * datetime.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        return t
    except (ValueError, IndexError):
        return None


def fmt_ts(t):
    return t.strftime("%Y%m%d%H%M%S +0000")


def read_bytes(src, retries=3):
    """Local path or URL -> raw (decompressed) bytes."""
    if re.match(r"^https?://", src):
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (compatible; EPG-Fetcher/1.0)"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(10 * (i + 1))
        else:
            raise RuntimeError(f"download failed: {type(last).__name__}")
    else:
        with open(src, "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def country_of(cid):
    m = re.search(r"\.([a-z]{2})$", (cid or "").lower())
    return m.group(1) if m else ""


QUALITY = r"(fhd|uhd|hd|sd|lq|hq|4k|8k|hevc|h265|raw|backup|vip)"


def name_key(name):
    n = (name or "").lower()
    n = re.sub(r"^\s*[a-z]{2,4}\s*[:|]\s*", "", n)   # "US: ", "en: "
    n = re.sub(r"^\s*(ar|in|us|uk|ca)\s+", "", n)     # "AR AL OULA"
    n = re.sub(r"\b" + QUALITY + r"\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def id_key(cid):
    """'en:.Abu.Dhabi.TV.ae' / 'abudhabitv.ae' -> 'abudhabi' style key."""
    stem = re.sub(r"\.[a-z_0-9]{2,12}$", "", (cid or "").lower())
    return name_key(stem.replace(".", " "))


def local_copy(src, workdir):
    """URL -> downloaded file path (kept compressed); local path returned as-is."""
    if not re.match(r"^https?://", src):
        return src
    path = os.path.join(workdir, "src_" + re.sub(r"[^A-Za-z0-9]", "_", src.split("?")[0])[-60:])
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (compatible; EPG-Fetcher/1.0)"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            return path
        except Exception:  # noqa: BLE001
            time.sleep(10 * (i + 1))
    raise RuntimeError("download failed")


def open_stream(path):
    f = open(path, "rb")
    if f.read(2) == b"\x1f\x8b":
        f.close()
        return gzip.open(path, "rb")
    f.seek(0)
    return f


def stream_elements(path):
    """Low-memory iterparse: yields channel/programme elements, freeing them after use."""
    with open_stream(path) as f:
        it = iterparse(f, events=("start", "end"))
        root = None
        for ev, el in it:
            if root is None and ev == "start":
                root = el
            if ev == "end" and el.tag in ("channel", "programme"):
                yield el
                root.clear()


# ── myepg (main guide) ───────────────────────────────────────────────────────
def repair_if_truncated(raw):
    """Return (raw, repaired: bool). Keeps everything up to the last complete programme."""
    if raw.rstrip().endswith(b"</tv>"):
        return raw, False
    cut = raw.rfind(b"</programme>")
    if cut < 0 or b"<tv" not in raw[:2000]:
        raise RuntimeError("source is cut off before any complete programme; cannot repair")
    return raw[:cut + len(b"</programme>")] + b"\n</tv>\n", True


def scan_programmes(raw):
    """channel id -> latest stop (UTC) over all programmes."""
    last = {}
    for m in PROG_RE.finditer(raw):
        head = m.group(0)[:600]
        c = CHAN_ATTR_RE.search(head)
        s = STOP_ATTR_RE.search(head)
        if not c or not s:
            continue
        t = parse_ts(s.group(1))
        if t is None:
            continue
        cid = c.group(1).decode("utf-8", "replace")
        if cid not in last or t > last[cid]:
            last[cid] = t
    return last


def scan_channels(raw):
    """channel id -> [display names] (channels are small; full parse of that block only)."""
    chans = {}
    for m in re.finditer(rb"<channel\b[^>]*>.*?</channel>", raw, re.S):
        block = m.group(0)
        cid = re.search(rb'id="([^"]*)"', block)
        if not cid:
            continue
        names = [n.decode("utf-8", "replace") for n in re.findall(rb"<display-name[^>]*>(.*?)</display-name>", block, re.S)]
        chans[cid.group(1).decode("utf-8", "replace")] = names
    return chans


def drop_old(raw, keep_past_hours):
    cutoff = NOW - datetime.timedelta(hours=keep_past_hours)
    dropped = 0

    def keep(m):
        nonlocal dropped
        s = STOP_ATTR_RE.search(m.group(0)[:600])
        t = parse_ts(s.group(1)) if s else None
        if t is not None and t < cutoff:
            dropped += 1
            return b""
        return m.group(0)

    return PROG_RE.sub(keep, raw), dropped


# ── filler sources ───────────────────────────────────────────────────────────
# Titles that mean "no listing" (2026-10-28 live guide: tvpassport "Test Channel 1" on 29 US
# channels, "Channel No Longer Available" on Disney/Nick/MTV CA, "Sendepause", "SIGN OFF" ...)
DEAD_TITLE = re.compile(r"\s*(test channel\s*\d*|channel no longer available|tv guide is not available|"
                        r"cha[iî]ne non disponible|canal non disponible|no information|sign ?off|(channel )?off air|"
                        r"sendepause|to be announced|tba)\s*$", re.I)


class Source:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.chans = {}
        self.last = {}
        self.has_real = set()
        for el in stream_elements(path):
            if el.tag == "channel":
                self.chans[el.get("id")] = [d.text or "" for d in el.findall("display-name")]
            else:
                t = parse_ts(el.get("stop"))
                c = el.get("channel")
                if t and (c not in self.last or t > self.last[c]):
                    self.last[c] = t
                # a channel whose upcoming listings are all filler has no real guide, so the next
                # source or our placeholder (the channel's name) is used instead
                if t and t > NOW and not DEAD_TITLE.match(el.findtext("title") or ""):
                    self.has_real.add(c)
        self.by_id = {k.lower(): k for k in self.chans}
        self.by_key = collections.defaultdict(list)   # (country, key) -> ids
        for k, names in self.chans.items():
            cc = country_of(k)
            keys = {id_key(k)} | {name_key(n) for n in names}
            for key in keys:
                if key:
                    self.by_key[(cc, key)].append(k)

    def hours_ahead(self, cid):
        t = self.last.get(cid) if cid in self.has_real else None
        return (t - NOW).total_seconds() / 3600 if t else -1e9

    def match(self, cid, names, need_hours):
        """Return (source_channel_id, how) or (None, None)."""
        exact = self.by_id.get(cid.lower())
        if exact and self.hours_ahead(exact) >= need_hours:
            return exact, "id"
        cc = country_of(cid)
        if not cc:
            return None, None  # without a country, name matching is too risky
        keys = {id_key(cid)} | {name_key(n) for n in names}
        cands = set()
        for key in keys:
            if key:
                cands.update(self.by_key.get((cc, key), []))
        cands = [c for c in cands if self.hours_ahead(c) >= need_hours]
        if len(cands) == 1:
            return cands[0], "name"
        return None, None   # zero or ambiguous -> leave blank rather than guess

    def programmes_for(self, wanted):
        """wanted: {source_id: (target_id, after_time)} -> list of programme XML bytes."""
        out = []
        per_target = collections.defaultdict(list)
        if not wanted:
            return out
        for el in stream_elements(self.path):
            if el.tag != "programme":
                continue
            src = el.get("channel")
            if src in wanted:
                target, after = wanted[src]
                start, stop = parse_ts(el.get("start")), parse_ts(el.get("stop"))
                if start and stop and stop > after and start >= after - datetime.timedelta(minutes=1) \
                        and start < NOW + datetime.timedelta(hours=FILL_AHEAD_HOURS):
                    parts = [f'<programme start="{fmt_ts(start)}" stop="{fmt_ts(stop)}" channel={quoteattr(target)}>']
                    for child in el:
                        if child.tag in ("title", "sub-title", "desc", "category", "episode-num", "icon", "rating", "date"):
                            attrs = "".join(f" {k}={quoteattr(v)}" for k, v in child.attrib.items())
                            raw = child.text or ""
                            if DESC_MAX and child.tag == "desc" and len(raw) > DESC_MAX:
                                raw = raw[:DESC_MAX].rsplit(" ", 1)[0] + "…"
                            text = escape(raw)
                            parts.append(f"<{child.tag}{attrs}>{text}</{child.tag}>" if (child.text or child.tag != "icon") else f"<{child.tag}{attrs} />")
                    parts.append("</programme>")
                    per_target[target].append((start, stop, "".join(parts).encode("utf-8")))
        # sources that merge several feeds list some shows twice: keep one per time slot
        for target, items in per_target.items():
            items.sort(key=lambda x: (x[0], -(x[1] - x[0]).total_seconds()))
            busy_until = None
            for start, stop, xml in items:
                if busy_until is None or start >= busy_until - datetime.timedelta(minutes=1):
                    out.append(xml)
                    busy_until = stop
        return out


# ── previous good build (used only after a repair) ───────────────────────────
def previous_programmes(prev_raw, channels_after):
    """channels_after: {cid: after_time} -> programme bytes from the previous build."""
    out = []
    for m in PROG_RE.finditer(prev_raw):
        head = m.group(0)[:600]
        c = CHAN_ATTR_RE.search(head)
        if not c:
            continue
        cid = c.group(1).decode("utf-8", "replace")
        if cid not in channels_after:
            continue
        st = re.search(rb'start="([^"]*)"', head)
        start = parse_ts(st.group(1)) if st else None
        if start and start >= channels_after[cid]:
            out.append(m.group(0))
    return out


# ── validation (same rules as fetch_epg.py) ──────────────────────────────────
def validate(raw):
    problems = []
    if b"<tv" not in raw[:2000] or not raw.rstrip().endswith(b"</tv>"):
        problems.append("not a complete XMLTV guide")
    ch = raw.count(b"<channel ")
    if ch < MIN_CHANNELS:
        problems.append(f"only {ch} channels (minimum {MIN_CHANNELS})")
    last = scan_programmes(raw)
    if not last:
        problems.append("no programme times found")
    else:
        ahead = (max(last.values()) - NOW).total_seconds() / 3600
        if ahead < MIN_HOURS_AHEAD:
            problems.append(f"guide only {ahead:.1f}h ahead")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--myepg", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", action="append", default=[], help="filler guide, in priority order (name=FILE_OR_URL)")
    ap.add_argument("--previous", help="previous good epg.xml.gz (used to refill after a repair)")
    ap.add_argument("--keep-past-hours", type=float, default=None)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rep = {"built_at": NOW.isoformat(timespec="seconds")}
    t0 = time.monotonic()

    raw = read_bytes(a.myepg)
    rep["myepg_mb"] = round(len(raw) / 1e6, 1)
    raw, repaired = repair_if_truncated(raw)
    rep["repaired_truncated_source"] = repaired

    chans = scan_channels(raw)
    last = scan_programmes(raw)
    rep["channels"] = len(chans)
    rep["placeholder_channels"] = sum(1 for c in chans if c.startswith("dummy-"))

    def ahead(cid):
        t = last.get(cid)
        return (t - NOW).total_seconds() / 3600 if t else -1e9

    extra = []

    # 1) after a repair, refill short channels from the previous good build
    if repaired and a.previous and os.path.exists(a.previous):
        prev = read_bytes(a.previous)
        short = {c: (last.get(c) or NOW - datetime.timedelta(days=1)) for c in chans if ahead(c) < MIN_HOURS_AHEAD}
        got = previous_programmes(prev, short)
        extra += got
        for p in got:
            c = CHAN_ATTR_RE.search(p[:600]).group(1).decode("utf-8", "replace")
            s = parse_ts(STOP_ATTR_RE.search(p[:600]).group(1))
            if s and (c not in last or s > last[c]):
                last[c] = s
        rep["refilled_from_previous"] = {"channels": len({CHAN_ATTR_RE.search(p[:600]).group(1) for p in got}), "programmes": len(got)}

    # 2) top up real channels whose guide runs out soon
    gaps = [c for c in chans if not c.startswith("dummy-") and ahead(c) < FILL_BELOW_HOURS]
    rep["real_channels_running_out"] = len(gaps)
    filled = collections.Counter()
    remaining = set(gaps)
    for spec in a.source:
        sname, _, loc = spec.partition("=")
        try:
            src = Source(sname, local_copy(loc, a.out))
        except Exception as e:  # noqa: BLE001  a filler source failing must never stop the build
            rep.setdefault("source_errors", {})[sname] = str(e)[:200]
            continue
        wanted = {}
        for c in sorted(remaining):
            after = max(last.get(c) or NOW - datetime.timedelta(hours=2), NOW - datetime.timedelta(hours=2))
            sid, how = src.match(c, chans[c], need_hours=FILL_BELOW_HOURS)
            if sid and sid not in wanted:
                wanted[sid] = (c, after)
                filled[f"{sname}:{how}"] += 1
                rep.setdefault("matches", []).append(
                    f"{c} [{(chans[c] or [''])[0]}] <= {sname}:{how} {sid} [{(src.chans.get(sid) or [''])[0]}]")
        progs = src.programmes_for(wanted)
        got_ch = {CHAN_ATTR_RE.search(p[:600]).group(1).decode("utf-8", "replace") for p in progs}
        remaining -= got_ch
        extra += progs
        del src
    rep["filled"] = dict(filled)
    rep["filled_programmes"] = len(extra) - rep.get("refilled_from_previous", {}).get("programmes", 0)
    rep["still_blank"] = len(remaining)
    rep["still_blank_sample"] = sorted(f"{c} ({(chans[c] or [''])[0]})" for c in remaining)[:40]
    rep["alert"] = len(remaining) > ALERT_BLANK_CHANNELS

    # 3) optional: drop old programmes
    if a.keep_past_hours is not None:
        raw, dropped = drop_old(raw, a.keep_past_hours)
        rep["dropped_old_programmes"] = dropped

    # 4) assemble: myepg first, filler appended before </tv>
    body = raw.rstrip()
    assert body.endswith(b"</tv>")
    final = body[:-len(b"</tv>")] + b"\n".join(extra) + b"\n</tv>\n"

    problems = validate(final)
    # A repaired file is only better than keeping the old guide if we could refill it.
    if repaired:
        if not (a.previous and os.path.exists(a.previous)):
            problems.append("source was cut off and no previous guide to refill from; keeping the old guide")
        else:
            prev_count = read_bytes(a.previous).count(b"<programme ")
            new_count = final.count(b"<programme ")
            rep["programmes_vs_previous"] = round(new_count / max(prev_count, 1), 3)
            if new_count < MIN_REPAIRED_RATIO * prev_count:
                problems.append(f"repaired guide has only {new_count} programmes vs {prev_count} before; keeping the old guide")
    rep["problems"] = problems
    rep["xml_mb"] = round(len(final) / 1e6, 1)
    if not problems:
        path = os.path.join(a.out, "epg.xml.gz")
        with open(path + ".tmp", "wb") as out, gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as g:
            g.write(final)
        os.replace(path + ".tmp", path)
        rep["gz_mb"] = round(os.path.getsize(path) / 1e6, 1)
    rep["seconds"] = round(time.monotonic() - t0, 1)
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(rep, f, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "still_blank_sample"}, indent=1))
    if problems:
        print("NOT WRITTEN: " + "; ".join(problems), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
