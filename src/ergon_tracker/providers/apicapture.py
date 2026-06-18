"""Captured-API replay provider — for "proxied" giants that have no reachable ATS host but DO
expose a public, no-auth JSON/GraphQL job API on their own domain (Goldman -> api-higher.gs.com).

A Playwright capture pass records the SPA's job-data request verbatim — URL, method, body — plus
dot-paths into the response (records, total) and a per-field map. We replay that request exactly,
mutating only the page field, and extract jobs generically. Specs live in
``registry/data/apicapture.json`` keyed by token; the provider is OPT-IN (``matches()`` only
resolves an explicit ``apicapture:`` scheme) so it never auto-claims a host.

Token: a spec key (e.g. ``"goldmansachs"``). Fields absent from the capture normalize to ``None``.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["ApiCaptureProvider"]

_SCHEME_RE = re.compile(r"^api(?:capture)?:", re.IGNORECASE)
_EMPLOYMENT = {
    "full_time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _load_specs() -> dict[str, dict[str, Any]]:
    try:
        text = (files("ergon_tracker.registry.data") / "apicapture.json").read_text("utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _dig(obj: Any, path: list[Any]) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _set(obj: Any, path: list[Any], value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


@register("apicapture")
class ApiCaptureProvider(BaseProvider):
    name = "apicapture"

    MAX_PAGES = 200

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        m = _SCHEME_RE.match(url_or_host.strip())
        if not m:
            return None
        tok = url_or_host.strip()[m.end() :].strip()
        return tok or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        token = _SCHEME_RE.sub("", token.strip()).strip()
        spec = _load_specs().get(token)
        if not spec:
            return []
        url, method = spec["url"], spec.get("method", "POST").upper()
        page_path = spec.get("page_path") or []
        page_start = int(spec.get("page_start", 0))
        size = int(spec.get("size", 50))
        rec_path, tot_path = spec.get("records_path") or [], spec.get("total_path") or []
        company = spec.get("company") or token

        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None
        for page in range(self.MAX_PAGES):
            body = copy.deepcopy(spec.get("body"))
            if page_path:
                _set(body, page_path, page_start + page)
            if spec.get("size_path"):
                _set(body, spec["size_path"], size)
            try:
                if method == "POST":
                    data = await fetcher.post_json(url, json=body)
                else:
                    data = await fetcher.get_json(url)
            except Exception:
                break
            if total is None:
                t = _dig(data, tot_path)
                total = t if isinstance(t, int) else None
            records = _dig(data, rec_path)
            if not isinstance(records, list) or not records:
                break
            new = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                jid = str(_dig(rec, [spec["fields"].get("id", "id")]) or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=company,
                        token=token,
                        url=self._field(rec, spec, "url"),
                        payload={**rec, "_spec": spec["fields"]},
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws[:limit]
            if new == 0 or (total is not None and (page - page_start + 1) * len(records) >= total):
                break
        return raws

    @staticmethod
    def _field(rec: dict[str, Any], spec: dict[str, Any], name: str) -> str | None:
        key = spec["fields"].get(name)
        if not key:
            return None
        val = rec.get(key)
        return val if isinstance(val, str) and val.strip() else None

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        fmap = p.get("_spec", {})

        title = str(p.get(fmap.get("title", "title")) or "")
        department = self._clean(p.get(fmap.get("department", "")))
        loc = self._location(p.get(fmap.get("location", "")))
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        emp_raw = (
            str(p.get(fmap.get("employment_type", "")) or "").strip().lower().replace(" ", "_")
        )
        employment = _EMPLOYMENT.get(emp_raw, EmploymentType.UNKNOWN)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            department=department,
            salary=None,
            posted_at=self._date(p.get(fmap.get("posted_at", ""))),
            updated_at=None,
            description_html=self._clean(p.get(fmap.get("description", ""))),
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _date(v: Any) -> datetime | None:
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            return datetime.fromisoformat(v.strip()[:10])
        except ValueError:
            return None

    @staticmethod
    def _location(v: Any) -> Location | None:
        """Build a Location from a string, or a list/dict of {city,state,country,name}."""
        item = v[0] if isinstance(v, list) and v else v
        if isinstance(item, str) and item.strip():
            label = item.strip()
        elif isinstance(item, dict):
            parts = [
                str(item[k]).strip()
                for k in ("city", "name", "state", "country")
                if item.get(k) and str(item[k]).strip()
            ]
            label = ", ".join(dict.fromkeys(parts))
        else:
            return None
        if not label:
            return None
        return Location(raw=label, is_remote="remote" in label.lower())
