"""ADP Workforce Now Recruitment careers provider.

ADP Workforce Now is one of the most common US enterprise ATSes. Each client's public career
center is a SPA at ``https://{host}/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}``
where ``host`` is ``workforcenow.adp.com`` or ``workforcenow.cloud.adp.com`` (separate
namespaces — a cid is valid on exactly one) and ``cid`` is the client's GUID. The board fetches
jobs from a public, no-auth JSON endpoint with NO browser::

    GET https://{host}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
        ?cid={cid}&$top={N}&$skip={M}

Response: ``{"jobRequisitions": [ {record}, ... ]}``. Each record carries ``itemID`` (unique),
``requisitionTitle``, ``postDate``, ``requisitionLocations`` (``[{address:{cityName,
countrySubdivisionLevel1:{codeValue}}}]``), ``workLevelCode.shortName`` (e.g. "Full-Time"), and a
``customFieldGroup.stringFields`` list that includes ``ExternalJobID``.

Pagination gotcha: ``$skip=N`` is inclusive of index ``N-1`` (one-row overlap per page). So we
NEVER stride by a fixed page size — we advance ``skip`` by the ACTUAL row count returned and
dedup by ``itemID``, which is correct regardless of any server-side ``$top`` cap.

Token: ``"{cid}"`` (host defaults to ``workforcenow.adp.com``), or ``"{cid}|{host}"`` for the
cloud namespace, or ``"{cid}|{host}|{company}"`` to carry a display name. Example:
``"3993975e-194c-4504-9c5e-9e6017ca5023||ACNB Corp"``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["ADPProvider"]

_DEFAULT_HOST = "workforcenow.adp.com"
_HOSTS = frozenset({"workforcenow.adp.com", "workforcenow.cloud.adp.com"})
_API = "https://{host}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
_VIEW = "https://{host}/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={jid}&lang=en_US"
_CID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_PAGE = 100


@register("adp")
class ADPProvider(BaseProvider):
    name = "adp"

    MAX_PAGES = 200  # bound full pulls

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise an ADP WFN career-center URL -> token (``cid`` or ``cid|host``), else None."""
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if host not in _HOSTS:
            return None
        cid = (parse_qs(parts.query).get("cid") or [""])[0].strip()
        if not _CID_RE.fullmatch(cid):
            m = _CID_RE.search(url_or_host)  # fall back to a GUID anywhere in the URL
            cid = m.group(0) if m else ""
        if not _CID_RE.fullmatch(cid):
            return None
        cid = cid.lower()
        return cid if host == _DEFAULT_HOST else f"{cid}|{host}"

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        cid = parts[0].lower()
        host = parts[1] if len(parts) > 1 and parts[1] else _DEFAULT_HOST
        host = host.replace("https://", "").replace("http://", "").strip("/").lower()
        company = parts[2] if len(parts) > 2 and parts[2] else None
        return cid, host, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        cid, host, company = self._parse(token)
        if not _CID_RE.fullmatch(cid) or host not in _HOSTS:
            return []
        base = _API.format(host=host)
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        skip = 0  # advance by the ACTUAL returned count (never a fixed stride): $skip overlaps by
        # one row and a server Top cap would otherwise create silent gaps.
        for _ in range(self.MAX_PAGES):
            url = f"{base}?cid={cid}&$top={_PAGE}&$skip={skip}"
            try:
                data = await fetcher.get_json(url, headers={"Accept": "application/json"})
            except Exception:  # noqa: BLE001 - a dead/blocked board just yields nothing
                break
            reqs = data.get("jobRequisitions") if isinstance(data, dict) else None
            if not isinstance(reqs, list) or not reqs:
                break
            new = 0
            for rec in reqs:
                if not isinstance(rec, dict):
                    continue
                jid = str(rec.get("itemID") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raws.append(self._to_raw(rec, cid, host, company, jid))
                if limit is not None and len(raws) >= limit:
                    return raws
            skip += len(reqs)
            if new == 0:
                break
        return raws

    @staticmethod
    def _string_field(rec: dict[str, Any], code: str) -> str | None:
        group = rec.get("customFieldGroup")
        fields = group.get("stringFields") if isinstance(group, dict) else None
        if not isinstance(fields, list):
            return None
        for f in fields:
            if isinstance(f, dict) and isinstance(f.get("nameCode"), dict):
                if f["nameCode"].get("codeValue") == code:
                    val = f.get("stringValue")
                    return str(val).strip() if val else None
        return None

    def _to_raw(
        self, rec: dict[str, Any], cid: str, host: str, company: str | None, jid: str
    ) -> RawJob:
        ext = self._string_field(rec, "ExternalJobID") or jid
        token = cid if host == _DEFAULT_HOST else f"{cid}|{host}"
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company or cid,
            token=token,
            url=_VIEW.format(host=host, cid=cid, jid=ext),
            payload=rec,
        )

    @staticmethod
    def _location(rec: dict[str, Any]) -> Location | None:
        locs = rec.get("requisitionLocations")
        item = locs[0] if isinstance(locs, list) and locs else None
        if not isinstance(item, dict):
            return None
        addr = item.get("address")
        addr = addr if isinstance(addr, dict) else {}
        city = str(addr.get("cityName") or "").strip()
        state = ""
        sub = addr.get("countrySubdivisionLevel1")
        if isinstance(sub, dict):
            state = str(sub.get("codeValue") or "").strip()
        label = ", ".join(x for x in (city, state) if x)
        if not label and isinstance(item.get("nameCode"), dict):
            label = str(item["nameCode"].get("shortName") or "").strip()
        if not label:
            return None
        return Location(
            city=city or None,
            region=state or None,
            raw=label,
            is_remote="remote" in label.lower(),
        )

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(value.strip()[:10], "%Y-%m-%d")
            except ValueError:
                return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = self._location(p)
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        level = p.get("workLevelCode")
        short = level.get("shortName", "") if isinstance(level, dict) else ""
        employment = (
            EmploymentType.FULL_TIME
            if "full" in str(short).lower()
            else EmploymentType.PART_TIME
            if "part" in str(short).lower()
            else EmploymentType.UNKNOWN
        )
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("requisitionTitle") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            department=self._string_field(p, "HomeDepartment"),
            posted_at=self._date(p.get("postDate")),
        )
