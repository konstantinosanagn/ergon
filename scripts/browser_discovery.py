"""Browser-assisted discovery — Tier-1 of the gated browser subsystem (offline-only).

    GATE: this consumes ONLY companies the ATS-Exhaustion Ladder proved exhausted
    (`browser_queue.json`). Never point it at a company without a complete exhaustion log.
    See docs/superpowers/specs/2026-06-21-browser-discovery-design.md.

The design splits cleanly into two halves so the hard logic is pure + tested and the browser is a
thin, swappable shell:

1. **Capture (non-deterministic, interactive).** A Playwright pass loads a careers SPA, records the
   job-list XHR verbatim, and writes a ``capture.json`` fixture::

       {"request": {"url","method","body","headers"}, "response": <parsed JSON>}

   (Run via the Playwright MCP; that step is I/O, not modeled here.)

2. **Propose (deterministic, this module).** ``propose_spec`` classifies the captured response shape
   — locates the job-records array, the total-count field, and maps our fields against ATS vocabulary
   — and emits an :mod:`ergon_tracker.providers.apicapture` spec. This is the careerscout
   "response-shape classification" idea, made pure and regression-tested against real specs.

Propose, don't dispose: the emitted spec is *verified live* through the apicapture provider (and the
build_registry gate) before it ever touches ``seed.json`` — discovery is fallible, verification is not.

Usage::
    # after a Playwright capture writes capture.json:
    python scripts/browser_discovery.py capture.json --company "Acme" --token acme
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]

# --- field vocabulary: our-field -> response key synonyms (matched key-normalized) ----------------
FIELD_VOCAB: dict[str, tuple[str, ...]] = {
    "title": ("title", "jobtitle", "postingtitle", "positiontitle", "name", "roletitle", "jobname"),
    "id": (
        "id",
        "jobid",
        "positionid",
        "jobnumber",
        "reqid",
        "requisitionid",
        "jobnum",
        "jobcode",
        "code",
        "slug",
        "refnumber",
        "externalid",
    ),
    "location": (
        "location",
        "joblocation",
        "normalizedlocation",
        "locations",
        "city",
        "primarylocation",
        "locationname",
        "jobcity",
        "worklocation",
    ),
    "url": (
        "url",
        "jobpath",
        "applyurl",
        "link",
        "joburl",
        "detailurl",
        "canonicalurl",
        "jobdetailurl",
        "permalink",
        "absoluteurl",
    ),
    "department": (
        "department",
        "businesscategory",
        "team",
        "category",
        "jobcategory",
        "function",
        "practice",
        "discipline",
        "jobfamily",
    ),
    "posted_at": (
        "postedat",
        "posteddate",
        "postingdate",
        "publisheddate",
        "dateposted",
        "createddate",
        "postdate",
        "datecreated",
        "firstpublished",
    ),
    "description": (
        "description",
        "jobsummary",
        "summary",
        "content",
        "jobdescription",
        "jobdescplace",
        "descriptionshort",
        "shortdescription",
        "snippet",
    ),
}
_TITLE_KEYS = set(FIELD_VOCAB["title"]) | set(FIELD_VOCAB["id"])
_TOTAL_KEYS = (
    "total",
    "totalcount",
    "totalrecords",
    "hits",
    "count",
    "numfound",
    "recordstotal",
    "totalresults",
    "totalhits",
    "resultcount",
)
_PAGE_PARAMS = ("offset", "start", "from", "page", "pagenumber", "pageindex", "p", "skip")
_OFFSET_PARAMS = ("offset", "start", "from", "skip")  # advance by page size, not by 1
_SIZE_PARAMS = ("resultlimit", "limit", "perpage", "pagesize", "size", "count", "rows")

def _norm(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _walk(obj: Any, path: list[Any]):
    """Yield (path, value) for every node in a nested dict/list."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + [k])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, path + [i])


def _looks_like_job(rec: Any) -> bool:
    return isinstance(rec, dict) and any(_norm(k) in _TITLE_KEYS for k in rec)


def find_records_path(response: Any) -> list[Any]:
    """Path to the job-records array = the LONGEST list whose elements look like job dicts."""
    best: tuple[int, list[Any]] = (0, [])
    for path, val in _walk(response, []):
        if (
            isinstance(val, list)
            and val
            and sum(_looks_like_job(x) for x in val) >= max(1, len(val) // 2)
            and len(val) > best[0]
        ):
            best = (len(val), path)
    # strip trailing list index if the walk landed on an element; keep the list's own path
    return [p for p in best[1] if not isinstance(p, int)] if best[1] else []


def find_total_path(response: Any, records_path: list[Any]) -> list[Any]:
    """First int-valued key (outside the records array) whose name reads like a total count."""
    rp = tuple(records_path)
    for path, val in _walk(response, []):
        if (
            isinstance(val, int)
            and path
            and _norm(path[-1]) in _TOTAL_KEYS
            and tuple(path[: len(rp)]) != rp  # not inside a record
        ):
            return path
    return []


def map_fields(records: list[Any]) -> dict[str, str]:
    """Map our-field -> dotted response path, searched within sample records.

    Handles nested scalars under a vocab-matching parent (WordPress ``title.rendered``,
    Apple ``team.teamName``): a leaf-key match wins, else a parent-key match with a scalar leaf.
    """
    sample = [r for r in records[:8] if isinstance(r, dict)]
    out: dict[str, str] = {}
    for field, synonyms in FIELD_VOCAB.items():
        rank = {s: i for i, s in enumerate(synonyms)}
        best: tuple[tuple[int, int], str] | None = None  # ((kind, synonym-rank), dotted-path)
        for rec in sample:
            for path, val in _walk(rec, []):
                if not path or len(path) > 3 or not isinstance(path[-1], str):
                    continue
                if not isinstance(val, (str, int, float, list)):
                    continue
                leaf, parent = (
                    _norm(path[-1]),
                    (_norm(path[-2]) if len(path) >= 2 and isinstance(path[-2], str) else None),
                )
                if leaf in rank:  # leaf match = most specific (kind 0)
                    score = (0, rank[leaf])
                elif (
                    parent in rank
                ):  # parent fallback (kind 1), e.g. team.teamName / title.rendered
                    score = (1, rank[parent])
                else:
                    continue
                if best is None or score < best[0]:
                    best = (score, ".".join(str(p) for p in path))
        if best is not None:
            out[field] = best[1]
    return out


def infer_pagination(request: dict[str, Any]) -> dict[str, Any]:
    """Detect offset/page pagination from the captured request (GET query or POST body)."""
    method = (request.get("method") or "GET").upper()
    out: dict[str, Any] = {}
    if method == "GET":
        q = parse_qs(urlsplit(request.get("url", "")).query)
        flat = {_norm(k): (v[0] if isinstance(v, list) else v) for k, v in q.items()}
        for p in _PAGE_PARAMS:
            if p in flat:
                # recover the original (non-normalized) param name from the URL
                orig = next(
                    (k for k in parse_qs(urlsplit(request["url"]).query) if _norm(k) == p), p
                )
                out["page_param"] = orig
                out["page_start"] = int(flat[p]) if str(flat[p]).lstrip("-").isdigit() else 0
                size = next(
                    (int(flat[s]) for s in _SIZE_PARAMS if s in flat and str(flat[s]).isdigit()), 1
                )
                out["page_step"] = size if p in _OFFSET_PARAMS else 1
                break
    else:
        body = request.get("body")
        if isinstance(body, dict):
            for path, val in _walk(body, []):
                if (
                    path
                    and isinstance(path[-1], str)
                    and _norm(path[-1]) in _PAGE_PARAMS
                    and isinstance(val, int)
                ):
                    out["page_path"] = [str(p) for p in path]
                    out["page_start"] = val
                    out["page_step"] = 1
                    break
    return out


def propose_spec(
    request: dict[str, Any], response: Any, *, company: str, token: str
) -> dict[str, Any]:
    """Turn a captured (request, response) into an apicapture spec. Pure + deterministic."""
    records_path = find_records_path(response)
    records = response
    for p in records_path:
        records = records[p] if isinstance(records, dict) else records
    if not isinstance(records, list):
        records = []
    fields = map_fields(records)
    if "title" not in fields:
        raise ValueError(
            f"no title-like field found in records for {token!r}; capture may be wrong"
        )

    method = (request.get("method") or "GET").upper()
    spec: dict[str, Any] = {
        "company": company,
        "url": request["url"],
        "method": method,
        "records_path": records_path,
        "total_path": find_total_path(response, records_path),
        "fields": fields,
    }
    if method == "POST" and request.get("body") is not None:
        spec["body"] = request["body"]
    if request.get("headers"):
        # keep only stable, semantic headers; drop volatile/auth-ish ones a replay shouldn't pin
        drop = {
            "cookie",
            "authorization",
            "content-length",
            "host",
            "user-agent",
            "referer",
            "origin",
        }
        hdrs = {k: v for k, v in request["headers"].items() if k.lower() not in drop}
        if hdrs:
            spec["headers"] = hdrs
    spec.update(infer_pagination(request))
    return spec


async def verify_spec_async(spec: dict[str, Any], token: str) -> tuple[int, str]:
    """Async core of the verify-gate: replay the proposed spec through the real apicapture provider.

    Callable from inside a running event loop (e.g. the Tier-1 capture batch). NOTE: monkeypatches the
    module-global ``_load_specs``, so callers must NOT run this concurrently for different tokens —
    verify sequentially (the batch capturer does)."""
    sys.path.insert(0, str(ROOT / "src"))
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers import apicapture as ap
    from ergon_tracker.providers.apicapture import ApiCaptureProvider

    # Inject the proposed spec into the provider's in-memory map so we can verify it live BEFORE it is
    # ever written to apicapture.json (propose -> verify -> only then merge).
    orig = ap._load_specs
    ap._load_specs = lambda: {token: spec}  # type: ignore[assignment]
    try:
        async with AsyncFetcher(timeout=30) as f:
            raws = await ApiCaptureProvider().fetch(token, SearchQuery(limit=10), f)
        if not raws:
            return 0, "spec replayed but returned 0 jobs"
        j = ApiCaptureProvider().normalize(raws[0])
        return len(raws), f"OK — {len(raws)} jobs, e.g. {j.title!r}"
    finally:
        ap._load_specs = orig  # type: ignore[assignment]


def verify_spec(spec: dict[str, Any], token: str) -> tuple[int, str]:
    """Sync wrapper around :func:`verify_spec_async` (for sync callers / the CLI)."""
    import anyio

    return anyio.run(verify_spec_async, spec, token)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Propose an apicapture spec from a browser capture fixture"
    )
    ap.add_argument("capture", help="capture.json with {request, response}")
    ap.add_argument("--company", required=True)
    ap.add_argument("--token", required=True, help="spec key (company slug)")
    ap.add_argument("--verify", action="store_true", help="replay the spec live before printing")
    args = ap.parse_args()

    cap = json.loads(Path(args.capture).read_text())
    spec = propose_spec(cap["request"], cap["response"], company=args.company, token=args.token)
    print(json.dumps({args.token: spec}, indent=2))
    if args.verify:
        n, msg = verify_spec(spec, args.token)
        print(f"\n[verify] {msg}", file=sys.stderr)
        sys.exit(0 if n else 2)


if __name__ == "__main__":
    main()
