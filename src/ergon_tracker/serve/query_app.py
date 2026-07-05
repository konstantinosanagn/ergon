"""HTTP QUERY search surface (RFC 10008) over the job index — raw ASGI 3.0, no web framework.

Why QUERY: our search takes ~30 structured filters (keywords, salary, degree, geo, visa…). GET blows
past URL limits and can't carry arrays; POST works but *lies* — it reads yet signals mutation, so
nothing caches it. QUERY is the honest fit: **safe + idempotent like GET, body-carrying like POST,
and cacheable** — and the RFC makes the request body part of the cache key, so identical complex
queries can be served from cache and answered ``304 Not Modified`` on repeat (exactly the shape of
agent traffic that hits the same query many times a minute).

Design (production-grade, dependency-light):
- **No web-framework dependency** — a raw ASGI callable. Runtime deps stay httpx+anyio (already core);
  ``uvicorn`` is only needed to actually serve (the ``serve`` extra), never to test.
- **Read-only, index-backed.** Wraps :class:`SqliteIndexBackend.search` — the cacheable/idempotent
  path. Live-provider crawling (non-idempotent, third-party) stays on the MCP where it belongs.
- **Concurrency.** SQLite search is blocking; it's offloaded to a **bounded** worker-thread pool
  (:class:`anyio.CapacityLimiter`) so a burst of N requests uses a fixed number of DB connections,
  not N. **Single-flight** coalescing means an agent hammering one uncached query computes it *once*
  while the rest await the same result — thundering-herd protection for free.
- **Body-aware caching + ETag/304.** The cache key is ``canonical(query) + index build_id``: the
  query is normalized through pydantic then dumped with sorted keys (so reordered/whitespace-variant
  bodies collide to one entry — and can't be used for cache poisoning), and a new index build rotates
  every ETag automatically. ``If-None-Match`` on a still-current ETag returns a bodyless ``304``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from ..index.backend import SqliteIndexBackend
from ..models import SearchQuery
from ..serialization import job_to_dict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
    Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

_MAX_BODY = int(os.environ.get("ERGON_QUERY_MAX_BODY", str(64 * 1024)))  # a search body is tiny
_LIMIT_CAP = int(os.environ.get("ERGON_QUERY_LIMIT_CAP", "200"))  # bound response size
_DEFAULT_LIMIT = 20


# --- cache-key + serialization helpers (pure) -----------------------------------------------------
def _canonical(q: SearchQuery) -> str:
    """Stable string for a query: pydantic-normalized, None-stripped, key-sorted. Equivalent bodies
    (reordered keys, whitespace, omitted-vs-null) collapse to one key — the poison-resistant basis."""
    data = q.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _etag(canonical: str, build_id: str) -> str:
    return '"' + hashlib.sha256(f"{build_id}\x00{canonical}".encode()).hexdigest()[:24] + '"'


def _encode(jobs: list[Any], meta: dict[str, Any]) -> bytes:
    return json.dumps(
        {"count": len(jobs), "results": [job_to_dict(j) for j in jobs], "index": meta},
        separators=(",", ":"),
    ).encode()


# --- bounded TTL+LRU cache (event-loop-thread only; no lock needed — all access is await-free) -----
class _Cache:
    def __init__(self, maxsize: int, ttl: float) -> None:
        self._d: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._max = maxsize
        self._ttl = ttl
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> bytes | None:
        entry = self._d.get(key)
        if entry is None:
            self.misses += 1
            return None
        ts, body = entry
        if time.monotonic() - ts > self._ttl:
            del self._d[key]
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return body

    def put(self, key: str, body: bytes) -> None:
        self._d[key] = (time.monotonic(), body)
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = anyio.Event()
        self.result: bytes | None = None
        self.error: BaseException | None = None


class QueryError(Exception):
    """A client/server error mapped to an HTTP status by the ASGI layer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SearchService:
    """Index-backed search with bounded DB concurrency, single-flight, and a body-aware ETag cache."""

    def __init__(
        self, index_path: Path | str, *, max_db_threads: int = 8,
        cache_size: int = 2048, cache_ttl: float = 300.0,
    ) -> None:
        self.backend = SqliteIndexBackend(index_path)
        self._path = Path(index_path)
        self._limiter = anyio.CapacityLimiter(max_db_threads)
        self._cache = _Cache(cache_size, cache_ttl)
        self._inflight: dict[str, _Pending] = {}
        self._build_id = ""
        self._row_count = 0
        self._mtime = -1.0

    def _refresh_meta(self) -> None:
        """Re-read index metadata if the file changed; a new build rotates ETags AND drops the cache."""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        meta = self.backend.metadata()
        self._build_id = str(meta.get("build_id") or "")
        self._row_count = int(meta.get("row_count") or 0)
        if self._mtime >= 0:  # not the first load -> the index was swapped; stale entries out
            self._cache.clear()
        self._mtime = mtime

    def index_meta(self) -> dict[str, Any]:
        self._refresh_meta()
        return {"build_id": self._build_id, "row_count": self._row_count}

    def _to_query(self, body: dict[str, Any]) -> SearchQuery:
        try:
            q = SearchQuery.model_validate(body)
        except Exception as exc:  # pydantic ValidationError -> 400 with the reason
            raise QueryError(400, f"invalid query: {exc}") from exc
        if q.semantic:
            # Honesty guard: this surface serves the lexical index (the cacheable/idempotent path).
            # Silently returning BM25 results under a semantic=true cache key would mislead — say so.
            raise QueryError(
                501, "semantic ranking is not available on the QUERY surface; "
                "use the MCP search_jobs(semantic=true) for embedding-based ranking",
            )
        limit = min(q.limit or _DEFAULT_LIMIT, _LIMIT_CAP)
        return q.model_copy(update={"limit": limit}) if limit != q.limit else q

    async def query(self, body: dict[str, Any]) -> tuple[str, bytes, bool]:
        """Return (etag, json_bytes, cache_hit). Coalesces concurrent identical misses (single-flight)."""
        q = self._to_query(body)
        self._refresh_meta()
        etag = _etag(_canonical(q), self._build_id)

        hit = self._cache.get(etag)
        if hit is not None:
            return etag, hit, True

        pending = self._inflight.get(etag)  # no await since the cache miss -> race-free registration
        if pending is not None:
            await pending.event.wait()
            if pending.error is not None:
                raise pending.error
            assert pending.result is not None
            return etag, pending.result, True  # coalesced onto the leader's computation

        pending = _Pending()
        self._inflight[etag] = pending
        try:
            meta = {"build_id": self._build_id, "row_count": self._row_count}
            jobs = await anyio.to_thread.run_sync(self.backend.search, q, limiter=self._limiter)
            body_bytes = _encode(jobs, meta)
            self._cache.put(etag, body_bytes)
            pending.result = body_bytes
            return etag, body_bytes, False
        except BaseException as exc:  # propagate to any coalesced waiters too
            pending.error = exc if isinstance(exc, Exception) else QueryError(500, "search failed")
            raise
        finally:
            self._inflight.pop(etag, None)
            pending.event.set()

    def stats(self) -> dict[str, Any]:
        return {"cache_hits": self._cache.hits, "cache_misses": self._cache.misses}


# --- ASGI app -------------------------------------------------------------------------------------
_JSON = [(b"content-type", b"application/json")]


async def _read_body(receive: Receive, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            raise QueryError(499, "client disconnected")
        chunk = msg.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            raise QueryError(413, f"request body exceeds {limit} bytes")
        chunks.append(chunk)
        if not msg.get("more_body"):
            return b"".join(chunks)


class QueryApp:
    """ASGI 3.0 callable. Routes: ``QUERY|POST /jobs`` (same JSON body), ``GET /health``."""

    def __init__(self, service: SearchService, *, cache_ttl: float = 300.0) -> None:
        self.service = service
        self._cache_control = f"public, max-age={int(cache_ttl)}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":  # websockets etc. not served here
            return
        try:
            await self._handle(scope, receive, send)
        except QueryError as e:
            if e.status == 499:
                return  # client vanished; nothing to send
            await self._json(send, e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001 - last-resort 500, never leak a traceback to the wire
            await self._json(send, 500, {"error": f"internal error: {type(e).__name__}"})

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope["method"]
        path = scope["path"].rstrip("/") or "/"

        if path == "/health":
            if method != "GET":
                raise QueryError(405, "health is GET-only")
            await self._json(send, 200, {"status": "ok", **self.service.index_meta(),
                                         **self.service.stats()})
            return

        if path != "/jobs":
            raise QueryError(404, f"no such resource: {scope['path']}")

        if method not in ("QUERY", "POST"):
            await self._json(send, 405, {"error": "use QUERY (preferred) or POST"},
                             extra=[(b"allow", b"QUERY, POST")])
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        ctype = headers.get(b"content-type", b"").split(b";")[0].strip()
        if ctype and ctype != b"application/json":
            raise QueryError(415, "content-type must be application/json")

        raw = await _read_body(receive, _MAX_BODY)
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise QueryError(400, f"malformed JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise QueryError(400, "body must be a JSON object")

        etag, payload, cache_hit = await self.service.query(body)

        # RFC 10008 conditional: a repeated QUERY whose result is unchanged answers 304.
        inm = headers.get(b"if-none-match", b"")
        if inm and etag.encode() in {t.strip() for t in inm.split(b",")}:
            await self._send(send, 304, b"", extra=[(b"etag", etag.encode()),
                                                    (b"cache-control", self._cache_control)])
            return

        await self._send(send, 200, payload, extra=[
            *_JSON,
            (b"etag", etag.encode()),
            (b"cache-control", self._cache_control),
            (b"x-cache", b"HIT" if cache_hit else b"MISS"),
        ])

    async def _json(self, send: Send, status: int, obj: dict[str, Any],
                    *, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        await self._send(send, status, json.dumps(obj).encode(), extra=[*_JSON, *(extra or [])])

    async def _send(self, send: Send, status: int, body: bytes,
                    *, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        await send({"type": "http.response.start", "status": status, "headers": extra or []})
        await send({"type": "http.response.body", "body": body})


def create_app(index_path: Path | str | None = None, **service_kwargs: Any) -> QueryApp:
    """Build the ASGI app. ``index_path`` defaults to ``$ERGON_INDEX_PATH`` or ``dist/index.sqlite``."""
    path = Path(index_path or os.environ.get("ERGON_INDEX_PATH", "dist/index.sqlite"))
    ttl = float(service_kwargs.pop("cache_ttl", 300.0))
    return QueryApp(SearchService(path, cache_ttl=ttl, **service_kwargs), cache_ttl=ttl)


def serve(index_path: Path | str | None = None, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the app with uvicorn (needs the ``serve`` extra: pip install 'ergon-tracker[serve]')."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ImportError("serving needs the extra: pip install 'ergon-tracker[serve]'") from exc
    uvicorn.run(create_app(index_path), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    serve(os.environ.get("ERGON_INDEX_PATH"))
