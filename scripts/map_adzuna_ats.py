"""Broad map: for every remaining adzuna giant, find its careers page (Tavily) and capture which
ATS host it loads (incl. the precise workday cxs tenant/site). Output a map for targeted upgrades."""
import asyncio, json, re, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
import anyio
from ergon_tracker.http import AsyncFetcher
from harvest_tokens import _core
from harvest_tavily import load_key
from census_residual import brand_query
from census_successfactors import tavily
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
ATS={'workday':'myworkdayjobs','oracle':'oraclecloud.com','phenom':'phenompeople','icims':'icims.com','taleo':'taleo.net','successfactors':'successfactors','smartrecruiters':'smartrecruiters','greenhouse':'greenhouse.io','lever':'jobs.lever.co','ashby':'ashbyhq','avature':'avature.net','peopleadmin':'peopleadmin.com','csod':'csod.com','brassring':'brassring','eightfold':'eightfold.ai','jobvite':'jobvite','dejobs':'dejobs.org','njoyn':'njoyn.com','radancy':'talentbrew'}
async def main():
    seed=json.load(open(ROOT/'src/ergon_tracker/registry/data/seed.json'))['companies']
    giants=json.load(open(ROOT/'runs/giants.json'))['uncovered_top']
    adz=[(g['filings'],g['name']) for g in giants if seed.get(_core(g['name']),{}).get('ats')=='adzuna']
    adz.sort(reverse=True)
    key=load_key()
    urls={}
    async with AsyncFetcher() as f:
        async with anyio.create_task_group() as tg:
            async def go(n):
                try: urls[n]=await tavily(f"{brand_query(n) or n} careers jobs", key, f)
                except Exception: urls[n]=[]
            for _f,n in adz: tg.start_soon(go,n)
    from playwright.async_api import async_playwright
    sem=asyncio.Semaphore(4); out={}
    async with async_playwright() as p:
        b=await p.chromium.launch()
        async def cap(n):
            async with sem:
                hosts=set(); cxs=set()
                for u in (urls.get(n) or [])[:2]:
                    ctx=await b.new_context(user_agent=UA); pg=await ctx.new_page()
                    def on_req(r):
                        for a,h in ATS.items():
                            if h in r.url.lower(): hosts.add(a)
                        m=re.search(r'wday/cxs/([a-z0-9-]+)/([A-Za-z0-9_-]+)/',r.url)
                        if m: cxs.add(f'{m.group(1)}|wd?|{m.group(2)}')
                    pg.on('request',on_req)
                    try:
                        await pg.goto(u,wait_until='networkidle',timeout=25000); await pg.wait_for_timeout(4000)
                    except Exception: pass
                    await ctx.close()
                    if hosts: break
                if hosts: out[n]={'ats':sorted(hosts),'cxs':sorted(cxs)}; print(f'  {n[:34]:34s} {sorted(hosts)} {sorted(cxs)}',flush=True)
        await asyncio.gather(*[cap(n) for _f,n in adz])
        await b.close()
    json.dump(out, open(ROOT/'runs/adzuna_ats_map.json','w'), indent=1)
    print(f'\nmapped {len(out)}/{len(adz)} giants -> runs/adzuna_ats_map.json')
anyio.run(main)
