"""S&P 500 flagship-coverage meter: diff the S&P 500 constituent list against seed.json.

Gives a measurable coverage % + the exact gap list (the recall-free way to drive the flagship
cron). Reads runs/sp500.json (constituents; refresh from the datahub CSV). Fuzzy + alias matched
to reconcile brand-vs-legal names (Alphabet->google, RTX->raytheon, AMD, UPS, etc.).
Usage: .venv/bin/python scripts/sp500_coverage.py
"""
import json, re, sys, unicodedata
from pathlib import Path
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parents[1]
seed = json.loads((ROOT/"src/ergon_tracker/registry/data/seed.json").read_text())["companies"]
sp = json.loads((ROOT/"runs/sp500.json").read_text())

def strip(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())
def norm(name):
    n = re.sub(r"\(.*?\)", "", name.lower()).replace("&", "and")
    n = re.sub(r"\b(inc|corp|corporation|co|company|companies|holdings|group|the|plc|ltd|llc|sa|nv|"
               r"incorporated|enterprise|technologies|technology|systems|industries|international|"
               r"financial|services|pharmaceuticals|laboratories|brands|beverage|stores)\b", "", n)
    return strip(n)

nkeys = {strip(k): k for k in seed}
ALIAS = {"alphabet": "google", "metaplatforms": "meta", "jpmorganchase": "jpmorgan", "rtx": "raytheon",
         "advancedmicrodevices": "amd", "unitedparcelservice": "ups", "usbancorp": "usbank",
         "lillyeli": "elililly", "fidelitynationalinformation": "fis", "waltdisney": "disney",
         "tmobileus": "t-mobile", "unitedhealth": None, "berkshirehathaway": None}

matched, missing = 0, []
for c in sp:
    nm = c["name"]; full = strip(nm); n = norm(nm); hit = None
    for cand in (full, n):
        if cand in ALIAS: hit = ALIAS[cand]; break
        if cand in nkeys: hit = nkeys[cand]; break
    if not hit:
        for nk, ok in nkeys.items():
            if len(nk) >= 4 and (nk in full or (n and nk in n)): hit = ok; break
    if not hit:
        m = process.extractOne(n or full, list(nkeys), scorer=fuzz.ratio)
        if m and m[1] >= 88: hit = nkeys[m[0]]
    if hit or full in ALIAS: matched += 1
    else: missing.append((nm, c.get("sector")))

print(f"S&P 500 coverage: {matched}/{len(sp)} = {round(100*matched/len(sp))}%  | gap: {len(missing)}")
for nm, sec in sorted(missing, key=lambda x: (x[1] or "", x[0])):
    print(f"  [{sec}] {nm}")
