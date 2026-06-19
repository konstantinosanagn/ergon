"""Auto-vetting aggregator scan: for each residual giant, look up its company board on The Muse
(cleaner, employer-matched) then Adzuna (broader), and SEED only if the runtime distinct-company
set is >=85% the right entity (the cleanliness gate that keeps aggregator captures clean)."""
from __future__ import annotations
import sys, json, re, anyio
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
from harvest_commoncrawl import load_seed_keys
from harvest_tokens import _core, name_match
from census_residual import brand_query
from ergon_tracker.config import get_env
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery
from ergon_tracker.providers.base import get_provider, load_builtins
load_builtins()
_GENERIC={'university','college','technologies','technology','solutions','systems','services','group',
 'holdings','global','company','corporation','industries','international','institute','school','district',
 'health','medical','center','associates','consultants','consulting','partners','foundation','of','the',
 'and','inc','llc','corp','co','national','american','america','north','state','enterprise','enterprises'}
def distinctive(name):
    words=[w for w in re.sub(r'[^a-z0-9 ]',' ',name.lower()).split() if w and w not in _GENERIC]
    return max(words, key=len) if words else ''
def clean(s): return re.sub(r'[^a-z0-9]','',(s or '').lower())
async def vet(provider, token, dw, name, f):
    try: raws=await get_provider(provider).fetch(token, SearchQuery(limit=60), f)
    except Exception: return None
    if len(raws)<6: return None
    good=sum(1 for r in raws if dw and dw in clean(r.company))
    frac=good/len(raws)
    # require >=85% share the distinctive word AND first job name_matches the giant
    if frac>=0.85 and name_match(name, raws[0].company or ''):
        return (len(raws), raws[0].company)
    return None
async def main():
    giants=json.loads((ROOT/'runs/giants.json').read_text())['uncovered_top']; sk=load_seed_keys()
    resid=[g for g in giants if _core(g['name']) not in sk]; resid.sort(key=lambda g:-(g.get('filings') or 0))
    has_adz=bool(get_env('ADZUNA_APP_ID') and get_env('ADZUNA_APP_KEY'))
    print(f'aggregator scan {len(resid)} residual (muse+{"adzuna" if has_adz else "no-adzuna"})...', flush=True)
    out=[]; done=[0]; lim=anyio.CapacityLimiter(3)
    async def probe(g, f):
        async with lim:
            name=g['name']; brand=brand_query(name); dw=distinctive(name)
            for tok in dict.fromkeys([brand, name.split()[0] if name.split() else name]):
                r=await vet('themuse', tok, dw, name, f)
                if r: out.append((g.get('filings',0),name,'themuse',tok,r[0],r[1])); break
                if has_adz:
                    r=await vet('adzuna', tok, dw, name, f)
                    if r: out.append((g.get('filings',0),name,'adzuna',tok,r[0],r[1])); break
            done[0]+=1
            if done[0]%30==0: print(f'  {done[0]}/{len(resid)} (hits {len(out)})', flush=True)
    async with AsyncFetcher(concurrency=3,per_host_rate=2,timeout=20.0,retries=1) as f:
        async with anyio.create_task_group() as tg:
            for g in resid: tg.start_soon(probe,g,f)
    out.sort(key=lambda x:-x[0])
    print(f'\n=== {len(out)} CLEAN aggregator hits ===')
    cands=[]
    for fil,name,prov,tok,n,co in out:
        print(f'  {fil:>5} {name[:28]:28} {prov:8} tok={tok[:16]:16} {n:>3}j co={co[:20]}')
        cands.append({'company':_core(name),'ats':prov,'domain':None,'token':tok})
    json.dump(cands, open(ROOT/'scripts/candidates_agg.json','w'), indent=2)
    print(f'wrote {len(cands)} candidates')
anyio.run(main)
