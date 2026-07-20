import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")
from harvest_commoncrawl import load_seed_keys
from harvest_tokens import _core

sk = set(load_seed_keys())
with open("src/ergon_tracker/registry/data/seed.json") as fh:
    seed = json.load(fh)["companies"]
with open("runs/giants.json") as fh:
    giants = json.load(fh)["uncovered_top"]
residual = [g for g in giants if _core(g["name"]) not in sk]
captured = [g for g in giants if _core(g["name"]) in sk]
adz = [g for g in captured if seed[_core(g["name"])].get("ats") == "adzuna"]
real = [g for g in captured if seed[_core(g["name"])].get("ats") != "adzuna"]

def f(lst):
    return sum(g["filings"] for g in lst)


print(f"TOTAL giants tracked: {len(giants)}")
print(f"  CAPTURED on a REAL ATS: {len(real)}  (filings {f(real)})")
print(f"  CAPTURED only on weak ADZUNA fallback: {len(adz)}  (filings {f(adz)})")
print(f"  UNCAPTURED residual: {len(residual)}  (filings {f(residual)})")
print()

KNOWN = {
 "tata consultancy services": "Akamai (jobs hidden behind JS search, no SEO pages)",
 "optum services": "over-broad parent (UHG Taleo)",
 "dfs corporate services": "over-broad parent (Discover->Capital One)",
 "michigan state university": "PageUp + AWS-WAF (job pages no JSON-LD)",
 "florida state university": "PeopleSoft (HTML postback)",
 "nyu grossman school of medicine": "custom / SilkRoad",
 "latentview analytics": "Darwinbox (token-walled SPA)",
 "3i infotech": "HONO (login-walled)",
 "sonata software north america": "Darwinbox",
 "credit karma": "empty Greenhouse board (on watch list)",
 "st jude medical cardiology division": "over-broad parent (Abbott)",
 "varian medical systems": "over-broad parent (Siemens Healthineers)",
 "scotia capital usa": "over-broad parent (Scotiabank)",
 "sg americas operational services": "over-broad parent (Societe Generale)",
 "trustees of boston university": "SilkRoad (elusive job-list)",
 "leland stanford jr univ slac national accelerator lab": "PeopleSoft",
 "bor usga obo augusta university": "old PeopleClick .do (session-walled)",
 "board of regents of university of nebraska": "PeopleAdmin (1 job / state-vs-univ)",
 "grandison management": "no public board (healthcare intl recruiting)",
 "populus group": "Bullhorn back-office, no public board",
 "beaconfire staffing solutions": "custom Next.js, no board",
 "mediatek usa": "custom careers",
 "evercore": "custom careers",
}
walled = [g for g in residual if g["name"] in KNOWN]
rest = [g for g in residual if g["name"] not in KNOWN]
print(f"RESIDUAL {len(residual)} splits into:")
print(f"  A) Known gated/walled/over-broad recognizable cos: {len(walled)}  (filings {f(walled)})")
print(f"  B) Other (overwhelmingly tiny IT-staffing body-shops w/ NO public board): {len(rest)}  (filings {f(rest)})")
print()
print("  -- (A) the gated/walled/over-broad ones --")
for g in sorted(walled, key=lambda x: -x["filings"]):
    print(f"    {g['filings']:5d}  {g['name'][:46]:46s} {KNOWN[g['name']]}")
print()
print("  -- (B) sample of the body-shop tail (top 15 by filings) --")
for g in sorted(rest, key=lambda x: -x["filings"])[:15]:
    print(f"    {g['filings']:5d}  {g['name']}")
