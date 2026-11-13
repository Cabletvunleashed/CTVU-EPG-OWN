#!/usr/bin/env python3
"""
Grab the extra iptv-org sites one site at a time, each under its own time limit, and keep
the last good copy of any site that fails.

One slow or dead site used to time out the whole extra grab and throw away every site's
listings (2026-10-28 05:23 UTC: epg.iptvx.one 948 s for 0 programmes -> ~560 channels lost).

Usage: grab_ext.py CHANNELS_XML EPG_DIR STORE_DIR OUT_XML
  CHANNELS_XML  custom.ext.channels.xml from make_grab_map.py
  EPG_DIR       iptv-org/epg checkout (npm ci already done)
  STORE_DIR     grab/ext in the repo: <site>.xml.gz + status.json, kept between runs
  OUT_XML       all sites joined into one XMLTV file, for merge_guides.py
"""
import collections
import gzip
import json
import os
import re
import signal
import subprocess
import sys
import time

SITE_SECONDS = int(os.environ.get("EXT_SITE_SECONDS", "150"))     # per site
TOTAL_SECONDS = int(os.environ.get("EXT_TOTAL_SECONDS", "1500"))  # whole pass; later sites reuse last good
KEEP_HOURS = int(os.environ.get("EXT_KEEP_HOURS", "36"))          # grabs cover 2 days, so 36h old is still useful


def safe(site):
    return re.sub(r"[^A-Za-z0-9._-]", "_", site)


def main():
    chans_xml, epg_dir, store, out_xml = sys.argv[1:5]
    os.makedirs(store, exist_ok=True)
    tmp = os.path.join(os.path.dirname(os.path.abspath(out_xml)), "ext_sites")
    os.makedirs(tmp, exist_ok=True)
    status_path = os.path.join(store, "status.json")
    try:
        status = json.load(open(status_path))
    except (OSError, ValueError):
        status = {}

    by_site = collections.defaultdict(list)
    if os.path.exists(chans_xml):
        for line in open(chans_xml, encoding="utf-8"):
            m = re.search(r'<channel site="([^"]+)"', line)
            if m:
                by_site[m.group(1)].append(line.rstrip("\n"))

    start, now = time.time(), int(time.time())
    result = {}
    # fewest channels first: small sites finish fast, a slow big one can only cost its own slot
    for site in sorted(by_site, key=lambda s: len(by_site[s])):
        name = safe(site)
        cfile, ofile = os.path.join(tmp, name + ".channels.xml"), os.path.join(tmp, name + ".xml")
        left = TOTAL_SECONDS - (time.time() - start)
        why = None
        if left < 20:
            why = "no time left"
        else:
            with open(cfile, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + "\n".join(by_site[site]) + "\n</channels>\n")
            t0 = time.time()
            # own process group, so a time-out also stops the node process npm started
            p = subprocess.Popen(["npm", "run", "grab", "--", f"--channels={cfile}", f"--output={ofile}",
                                  "--days=2", "--maxConnections=10", "--timeout=20000"],
                                 cwd=epg_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            try:
                p.wait(timeout=min(SITE_SECONDS, left))
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                why = f"timed out after {int(time.time() - t0)}s"
            n = 0
            if why is None and p.returncode != 0:
                why = f"grabber exit {p.returncode}"   # partial output is not stored as good
            if os.path.exists(ofile):
                n = open(ofile, encoding="utf-8", errors="ignore").read().count("<programme ")
            if why is None and n == 0:
                why = "0 programmes"
            if why is None:
                with open(ofile, "rb") as src, open(os.path.join(store, name + ".xml.gz"), "wb") as raw, \
                        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
                    dst.write(src.read())
                status[site] = {"ok": now, "programmes": n, "seconds": int(time.time() - t0)}
                result[site] = f"new {n}"
                continue
        prev = status.get(site, {})
        kept = os.path.join(store, name + ".xml.gz")
        if os.path.exists(kept) and now - prev.get("ok", 0) < KEEP_HOURS * 3600:
            result[site] = f"kept last good ({why})"
        else:
            result[site] = f"none ({why})"
            if os.path.exists(kept):
                os.remove(kept)
        status.setdefault(site, {})["last_fail"] = why

    # sites no longer asked for: drop them
    for f in os.listdir(store):
        if f.endswith(".xml.gz") and f[:-7] not in {safe(s) for s in by_site}:
            os.remove(os.path.join(store, f))
    status = {s: v for s, v in status.items() if s in by_site}
    json.dump(status, open(status_path, "w"), indent=1, sort_keys=True)

    parts = []
    for site in by_site:
        kept = os.path.join(store, safe(site) + ".xml.gz")
        if os.path.exists(kept):
            x = gzip.open(kept).read().decode("utf-8", "ignore")
            parts += re.findall(r"<channel\b.*?</channel>", x, re.S) + re.findall(r"<programme\b.*?</programme>", x, re.S)
    with open(out_xml, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n' + "\n".join(parts) + "\n</tv>\n")
    summary = collections.Counter(v.split(" ")[0] for v in result.values())
    print(f"extra sites: {dict(summary)} in {int(time.time() - start)}s")
    for site in sorted(result):
        if not result[site].startswith("new"):
            print(f"  {site}: {result[site]}")


if __name__ == "__main__":
    main()
