# jobspine

**Unified, reliable, typed job-fetching SDK.** `jobspine` combines and canonicalizes job
postings from many *free* sources — public ATS feeds (Greenhouse, Lever, Ashby, Workday) and
aggregators — behind one search interface, with job-aware deduplication and per-source health
reporting.

> Reliability-first: public ATS JSON feeds are the default path. Scraping adapters are opt-in
> and isolated. No paid APIs required.

## Quickstart

```python
import jobspine

# Simple, synchronous
result = jobspine.search("backend engineer", location="Berlin", remote=True)
for job in result:
    print(job.company, "—", job.title, "—", job.apply_url)

# Inspect per-source health (no silent failures)
for h in result.health:
    print(h.source, "ok" if h.ok else f"ERROR: {h.error}", h.count)
```

```python
# Async, full control
import asyncio
from jobspine import AsyncJobSpine, SearchQuery

async def main():
    async with AsyncJobSpine() as js:
        result = await js.search(SearchQuery(keywords="ml engineer", remote=True))
        print(len(result), "jobs")

asyncio.run(main())
```

## CLI

```bash
jobspine search "ml engineer" --remote --json
jobspine resolve acme.com          # detect the ATS + token a company uses
jobspine sources                   # provider health check
```

## MCP server

Expose jobspine to Claude and other MCP clients:

```bash
pip install 'jobspine[mcp]'
jobspine-mcp          # stdio transport
```

Tools: `search_jobs`, `resolve_company`, `list_sources`. Example Claude Desktop config:

```json
{
  "mcpServers": {
    "jobspine": { "command": "jobspine-mcp" }
  }
}
```

## Why jobspine

The OSS landscape splits into unreliable aggregator scrapers and fragmented ATS readers with
no dedup. `jobspine` is the bridge: reliable public feeds + a real job-aware dedup engine +
a maintained company→ATS registry with auto-discovery + structured observability.

See `docs/superpowers/specs/2026-06-15-jobspine-sdk-design.md` for the full design.

## License

MIT
