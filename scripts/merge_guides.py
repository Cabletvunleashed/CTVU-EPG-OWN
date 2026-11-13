#!/usr/bin/env python3
"""merge_guides.py MAIN EXTRA: append EXTRA's channels and programmes into MAIN (in place).
A missing, empty or broken EXTRA leaves MAIN untouched."""
import os
import re
import sys

main, extra = sys.argv[1], sys.argv[2]
try:
    x = open(extra, encoding="utf-8").read() if os.path.getsize(extra) > 0 else ""
except OSError:
    x = ""
chans = re.findall(r"<channel\b.*?</channel>", x, re.S)
progs = re.findall(r"<programme\b.*?</programme>", x, re.S)
if not progs:
    print("extended guide: nothing to merge")
    sys.exit(0)
m = open(main, encoding="utf-8").read()
cut = m.rfind("</tv>")
if cut < 0:
    sys.exit("main guide has no </tv>")
open(main + ".tmp", "w", encoding="utf-8").write(m[:cut] + "\n".join(chans + progs) + "\n</tv>\n")
os.replace(main + ".tmp", main)
print(f"extended guide: merged {len(chans)} channels, {len(progs)} programmes")
