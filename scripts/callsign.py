"""Match US local-station streams ("US: NBC 11 (WBAL) BALTIMORE") to grabber channels by
call sign + network, e.g. tvtv.us "NBC (WBAL-TV) Baltimore, MD". Strict: one answer or none."""
import collections
import re

CS = re.compile(r"\(([KW][A-Z]{2,3})(?:-[A-Z0-9]{1,4})?\)")
NETS = ["TELEMUNDO", "UNIVISION", "UNIMAS", "ESTRELLA", "AZTECA", "NBC", "CBS", "ABC", "FOX", "CW", "PBS",
        "MYNETWORK", "MNT", "ION", "INDEPENDENT", "IND"]
SUB = re.compile(r"-(?:TV|DT|LD|CD|CA)([2-9]|1\d)\b", re.I)   # sub-channel 2+ (secondary network)


def network(name):
    up = name.upper()
    for n in NETS:
        if re.search(r"\b" + n + r"\b", up):
            return n
    return None


def index(rows):
    """rows: iterable of (site, site_id, name) -> {CALLSIGN: [(site, sid, name)]}"""
    idx = collections.defaultdict(list)
    for site, sid, nm in rows:
        for c in set(CS.findall(nm)):
            idx[c].append((site, sid, nm))
    return idx


def match(stream_name, idx):
    m = CS.search(stream_name)
    if not m:
        return None
    net = network(stream_name.split(":", 1)[-1])
    cands = idx.get(m.group(1), [])
    if net:
        cands = [c for c in cands if network(c[2]) == net]
    main = [c for c in cands if not SUB.search(c[2])]
    cands = main or cands
    # same feed listed twice ("X HD" and "X") -> keep one
    by_base = {}
    for c in sorted(cands, key=lambda c: (" HD" in c[2], c[1])):
        by_base.setdefault(re.sub(r"\s+HD$", "", c[2].strip()), c)
    cands = list(by_base.values())
    return cands[0] if len(cands) == 1 else None
