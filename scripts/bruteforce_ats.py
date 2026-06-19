"""Deterministic ATS-host brute-force over residual giants: probe {brand}.{ats} conventions
directly (no Tavily), verify via the real provider + name_match. Catches mid-tier giants whose
board follows the naming convention but whose careers page Tavily can't find."""
from __future__ import annotations
import sys, json, re, anyio
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
from harvest_commoncrawl import load_seed_keys
from harvest_tokens import _core, name_match
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery
from ergon_tracker.providers.base import get_provider, load_builtins
load_builtins()
_STOP={'inc','llc','ltd','corp','corporation','company','co','technologies','technology','solutions','systems','services','group','holdings','global','the','and','of','llp','lp','pc','usa','us','america','north'}
def slugs(name):
    words=[w for w in re.sub(r'[^a-z0-9 ]',' ',name.lower()).split() if w]
    sig=[w for w in words if w not in _STOP]
    out=set()
    if sig:
        out.add(''.join(sig)); out.add(sig[0]); out.add(''.join(sig[:2])); out.add('-'.join(sig))
        out.add(''.join(w[0] for w in sig))  # acronym (e.g. l&t -> lt)
    if words: out.add(''.join(words)); out.add(words[0])
    return {s for s in out if len(s) >= 3}
# shared-host providers (expose company name -> name_match adjudication)
SHARED=['greenhouse','lever','ashby','smartrecruiters','workable','recruitee','breezy','personio','jazzhr','teamtailor','bamboohr','pinpoint','join','rippling']
async def probe_one(name, ss, f):
    for ats in SHARED:
        for s in list(ss)[:4]:
            try: raws=await get_provider(ats).fetch(s, SearchQuery(limit=3), f)
            except Exception: raws=[]
            if raws and name_match(name, raws[0].company or ''):
                return (ats, s, raws[0].company[:24])
    # icims (name exposed)
    for s in list(ss)[:3]:
        for h in [f'careers-{s}.icims.com', f'{s}.icims.com', f'activepostings-{s}.icims.com']:
            try: raws=await get_provider('icims').fetch(h, SearchQuery(limit=3), f)
            except Exception: raws=[]
            if raws and name_match(name, raws[0].company or ''):
                return ('icims', h, raws[0].company[:24])
    # jobvite (company name exposed)
    for s in list(ss)[:3]:
        try: raws=await get_provider('jobvite').fetch(s, SearchQuery(limit=3), f)
        except Exception: raws=[]
        if raws and name_match(name, raws[0].company or ''):
            return ('jobvite', s, raws[0].company[:24])
    # taleo/avature/phenom/eightfold (opaque company -> require slug==tenant, jobs>0)
    for s in list(ss)[:2]:
        for ats, tok in [('taleo', f'{s}.taleo.net|ext|'), ('taleo', f'{s}.taleo.net|1|101430233'),
                         ('avature', f'{s}.avature.net|careers'), ('phenom', f'{s}.phenompeople.com'),
                         ('eightfold', f'{s}.eightfold.ai'), ('eightfold', s)]:
            try: raws=await get_provider(ats).fetch(tok, SearchQuery(limit=3), f)
            except Exception: raws=[]
            if raws: return (ats, tok, (raws[0].company[:20] or s) + '(slug-host)')
    return None
async def main():
    giants=json.loads((ROOT/'runs/giants.json').read_text())['uncovered_top']; sk=load_seed_keys()
    resid=[g for g in giants if _core(g['name']) not in sk]
    resid.sort(key=lambda g:-(g.get('filings') or 0))
    print(f'brute-forcing {len(resid)} residual giants ({len(SHARED)} shared + icims + taleo/avature/phenom)...', flush=True)
    hits=[]; done=[0]; lim=anyio.CapacityLimiter(12)
    async def probe(g, f):
        async with lim:
            found=await probe_one(g['name'], slugs(g['name']), f)
            done[0]+=1
            if done[0]%30==0: print(f'  {done[0]}/{len(resid)} (hits {len(hits)})', flush=True)
            if found: hits.append((g.get('filings',0),g['name'],found)); print(f'  HIT {g.get("filings",0):>5} {g["name"][:28]:28} {found}', flush=True)
    async with AsyncFetcher(concurrency=16, per_host_rate=10, timeout=12.0, retries=1) as f:
        async with anyio.create_task_group() as tg:
            for g in resid: tg.start_soon(probe, g, f)
    print(f'\n=== {len(hits)} brute-force hits ===')
    out=[]
    for fil,name,h in sorted(hits,key=lambda x:-x[0]):
        print(f'  {fil:>5} {name[:34]:34} {h}')
        ats,tok=h[0],h[1]
        out.append({'company':_core(name),'ats':ats,'domain':None,'token':tok})
    json.dump(out, open(ROOT/'scripts/candidates_bf2.json','w'), indent=2)
    print(f'wrote {len(out)} candidates')
anyio.run(main)
