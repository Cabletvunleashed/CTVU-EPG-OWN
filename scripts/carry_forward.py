#!/usr/bin/env python3
"""carry_forward.py NEW_GUIDE_XML PREVIOUS_GUIDE_XML_GZ (2026-10-30 ac)

The free grabbers are flaky per channel: one run returns nothing for a channel that had two days of
listings the run before (10-30: plex gave 0 programmes for MrBeast, FilmRise ... and 54 streams fell
back to a placeholder for 6 hours). For every channel that has NO programme still to come in the new
grab, copy the previous grab's programmes that have not ended yet. Channels the new grab does cover
keep their own listings, except (2026-10-31) the stretch before their first new programme: a grab that
starts its day later than now (tvpassport begins at the next local day, so a grab after 00:00 UTC left
722 US locals blank from now until ~05:30 UTC) keeps the previous grab's programmes for that stretch,
only those ending by the first new start. Any problem leaves the new guide exactly as it was.
Staleness bound: carried programmes keep their real times and drop out once they end, so nothing
outlives the grab window (48h while the workflow grabs with --days=2; raise that and this grows too)."""
import datetime
import gzip
import json
import os
import re
import sys

PROG = re.compile(rb'<programme\b[^>]*?\bstart="([^"]*)"[^>]*?\bstop="([^"]*)"[^>]*?\bchannel="([^"]*)".*?</programme>', re.S)
CHAN = re.compile(rb'<channel\b[^>]*\bid="([^"]*)".*?</channel>', re.S)


def ts(s):
    try:
        return datetime.datetime.strptime(s.decode()[:20].strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return None


def future(data, now):
    """channel id -> list of (programme bytes) that end after now"""
    out = {}
    for m in PROG.finditer(data):
        sp = ts(m.group(2))
        if sp and sp > now:
            out.setdefault(m.group(3), []).append(m.group(0))
    return out


def mark(new_path, now, result, take=None, head=None):
    """carry_forward.json next to the new guide (committed as grab/carry_forward.json): what the
    latest grab carried, so any session can check it later without the Actions log"""
    take, head = take or {}, head or {}
    try:
        with open(os.path.join(os.path.dirname(new_path), "carry_forward.json"), "w") as f:
            json.dump({"now": now.isoformat(timespec="seconds"), "result": result, "channels_carried": len(take),
                       "programmes_kept": sum(len(v) for v in take.values()),
                       "head_gap_channels": len(head), "head_gap_programmes": sum(len(v) for v in head.values()),
                       "ids": sorted(c.decode("utf-8", "replace") for c in take)}, f, indent=1)
    except OSError:
        pass


def main(new_path, prev_path, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        new = open(new_path, "rb").read()
        prev = gzip.open(prev_path).read()
    except OSError as e:
        print(f"carry-forward: skipped ({e.__class__.__name__})")
        mark(new_path, now, "skipped")
        return 0
    have = future(new, now)
    old = future(prev, now)
    take = {c: p for c, p in old.items() if c not in have}
    # head gap: the new grab covers the channel but starts later than now
    first = {}
    for c, progs in have.items():
        starts = [ts(PROG.match(p).group(1)) for p in progs]
        starts = [x for x in starts if x]
        if starts:
            first[c] = min(starts)
    head = {}
    for c, progs in old.items():
        f0 = first.get(c)
        if c in take or not f0 or f0 <= now:
            continue
        keep, seen, last = [], set(), None
        for p in sorted(progs, key=lambda p: PROG.match(p).group(1)):
            m = PROG.match(p)
            st, sp = ts(m.group(1)), ts(m.group(2))
            if not st or not sp or sp > f0 or m.group(1) in seen or (last and st < last):
                continue                    # past the new grab's start, a duplicate, or overlapping the one before
            seen.add(m.group(1))
            last = sp
            keep.append(p)
        if keep:
            head[c] = keep
    if not take and not head:
        print("carry-forward: nothing to carry")
        mark(new_path, now, "nothing")
        return 0
    new_chans = {m.group(1) for m in CHAN.finditer(new)}
    chans = [m.group(0) for m in CHAN.finditer(prev) if m.group(1) in take and m.group(1) not in new_chans]
    cut = new.rfind(b"</tv>")
    if cut < 0:
        print("carry-forward: skipped (new guide has no </tv>)")
        mark(new_path, now, "skipped")
        return 0
    body = b"\n".join(chans + [x for c in sorted(take) for x in take[c]] + [x for c in sorted(head) for x in head[c]])
    tmp = new_path + ".cf"
    with open(tmp, "wb") as f:
        f.write(new[:cut] + body + b"\n</tv>\n")
    os.replace(tmp, new_path)
    mark(new_path, now, "carried", take, head)
    print(f"carry-forward: {len(take)} channels, {sum(len(v) for v in take.values())} programmes kept from the previous grab; "
          f"head gap filled on {len(head)} channels ({sum(len(v) for v in head.values())} programmes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1], sys.argv[2]))
    except Exception as e:  # noqa: BLE001 - never fail the build over this
        print(f"carry-forward: skipped ({e.__class__.__name__}: {str(e)[:120]})")
        sys.exit(0)
