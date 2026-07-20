"""Verify a provider token live. Usage: _verify_b2.py PROVIDER TOKEN"""
import sys

import anyio

sys.path.insert(0, 'src')
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery
from ergon_tracker.providers.base import get_provider, load_builtins

load_builtins()

async def main(provider, token):
    async with AsyncFetcher(timeout=45) as f:
        p = get_provider(provider)
        raws = await p.fetch(token, SearchQuery(limit=12), f)
        print("COUNT:", len(raws))
        print("COMPANIES:", [r.company for r in raws[:3]])
        for r in raws[:6]:
            n = p.normalize(r)
            print(" -", (n.title or "")[:45], "|", (str(n.location) if getattr(n,'location',None) else "")[:30])

if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2])
