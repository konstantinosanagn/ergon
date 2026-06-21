"""Deterministic S&P 500 -> seed-key crosswalk (the reliable coverage meter).

Why not fuzzy/semantic matching: a company NAME alone can't disambiguate namesakes ("Brown &
Brown" insurance vs "Brown University"; "Monster Beverage" vs Monster.com) — embeddings/fuzzy of
our slug keys collide on the shared token. So this matcher is deterministic + curated:

  1. Generate candidate keys per S&P name (normalize: drop (Class X)/(The), &->and, strip corp
     suffixes; full join + suffix-stripped prefixes; plus an explicit ALIAS map for abbreviations).
  2. Match against seed keys, but EXCLUDE a curated NAMESAKE set — generic short keys that live
     entity-checks proved are a DIFFERENT company than the S&P member (so the real S&P co only
     counts if it's present under its own distinct key, e.g. brownandbrown / monsterbeverage).
  3. A match where the key is much shorter than the name is flagged SHORT (spot-check candidate).

Run: .venv/bin/python scripts/sp500_crosswalk.py  [--write]  (writes runs/sp500_crosswalk.json)
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
seed = json.loads((ROOT / "src/ergon_tracker/registry/data/seed.json").read_text())["companies"]
sp = json.loads((ROOT / "runs/sp500.json").read_text())

# Abbreviation / brand aliases: S&P name-form -> our seed key (verified).
ALIAS = {
    "alphabet": "google", "metaplatforms": "meta", "jpmorganchase": "jpmorgan", "rtx": "raytheon",
    "raytheontechnologies": "raytheon", "advancedmicrodevices": "amd", "unitedparcelservice": "ups",
    "usbancorp": "usbank", "lillyeli": "elililly", "fidelitynationalinformationservices": "fis",
    "waltdisney": "disney", "tmobileus": "t-mobile", "hewlettpackardenterprise": "hpe",
    "unitedhealth": "optumservices",
    "pgande": "pge", "pgecorporation": "pge",  # "PG&E" -> &->and breaks the "pge" key match
}
# Generic short keys in seed that entity-checks proved are NAMESAKES (not the S&P company). The real
# S&P member only counts via its distinct key (brownandbrown, monsterbeverage, …) or is a true gap.
NAMESAKE_EXCLUDE = {
    "brown", "monster", "international", "royal", "steel", "universal", "vulcan", "cooper",
    "genuine", "ralph", "snap", "cms", "vici", "fidelity", "apollo", "bio",
    # verified-wrong matches (subsidiary / unrelated namesake — confirmed via live entity-check):
    "berkshire-hathaway-homestate-companies",  # workers-comp subsidiary, not the holding co (no central board)
    "berkshire-hathaway-homeservices-costa-blanca",  # Spanish real-estate FRANCHISE (Dénia/Altea), not BRK
    "electronica-teliar",  # a join.com board, NOT Electronic Arts (EA = gr8people, blocked)
}
GENERIC_FIRST = {"american", "united", "general", "national", "first", "new", "international", "global"}


def strip(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def toks(name: str) -> list[str]:
    s = re.sub(r"\(.*?\)", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower())
    s = s.replace("&", " and ")
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t]


GEN = {"the", "inc", "corp", "corporation", "co", "company", "companies", "group", "holdings",
       "plc", "ltd", "international", "global", "incorporated"}


def candidates(name: str) -> set[str]:
    t = toks(name)
    tt = t[:]
    while tt and tt[-1] in GEN:
        tt = tt[:-1]
    # also drop a trailing "and" left by "& Co"/"& Associates" so "KKR & Co"->"kkr"
    while tt and tt[-1] in GEN | {"and"}:
        tt = tt[:-1]
    out = {strip(name), "".join(t), "".join(tt)}  # full forms incl. suffix-stripped (any length)
    if tt and tt[0] not in GENERIC_FIRST and len(tt[0]) >= 2:
        out.add(tt[0])  # first token (PNC Financial Services->pnc, Aon plc->aon)
    for i in range(1, len(tt) + 1):  # prefixes (len>=4 to avoid generic single words)
        k = "".join(tt[:i])
        if len(k) >= 4:
            out.add(k)
    out |= {ALIAS[c] for c in list(out) if c in ALIAS}
    return {c for c in out if c}


# Normalize seed keys (strip hyphens etc.) so "snap-on"->"snapon", "coca-cola"->"cocacola" match.
nseed = {}
for k in seed:
    nseed.setdefault(strip(k), k)
nexclude = {strip(k) for k in NAMESAKE_EXCLUDE}

present, gaps, short_flags = {}, [], []
for c in sp:
    nm = c["name"]
    full = "".join(toks(nm))
    hit = None
    for cand in sorted(candidates(nm), key=len, reverse=True):  # prefer the most specific match
        if cand in nexclude:
            continue
        if cand in nseed:  # exact (normalized)
            hit = nseed[cand]
            break
        # prefix either way: handles seed key longer/shorter than name
        # (olddominion<->olddominionfreightline, hartford<->hartfordfinancial)
        if len(cand) >= 6:
            pm = next((ok for nk, ok in nseed.items()
                       if nk not in nexclude and (nk.startswith(cand) or cand.startswith(nk)) and len(nk) >= 6), None)
            if pm:
                hit = pm
                break
    if hit:
        present[nm] = {"key": hit, "ats": seed[hit]["ats"]}
        if len(strip(hit)) < max(3, int(0.5 * len(full))):
            short_flags.append((nm, hit, seed[hit]["ats"]))
    else:
        gaps.append((nm, c.get("sector")))

print(f"S&P 500: {len(present)}/{len(sp)} = {round(100*len(present)/len(sp))}% captured | {len(gaps)} gap")
print(f"\n=== TRUE GAP ({len(gaps)}) ===")
for nm, sec in sorted(gaps, key=lambda x: (x[1] or "", x[0])):
    print(f"  [{sec}] {nm}")
if short_flags:
    print(f"\n=== SHORT-KEY matches to spot-check ({len(short_flags)}) ===")
    for nm, k, a in short_flags:
        print(f"  {nm} -> {k} ({a})")

if "--write" in sys.argv:
    xwalk = {nm: present[nm] for nm in sorted(present)}
    xwalk["__gaps__"] = [nm for nm, _ in gaps]
    (ROOT / "runs/sp500_crosswalk.json").write_text(json.dumps(xwalk, indent=1))
    print("\nwrote runs/sp500_crosswalk.json")
