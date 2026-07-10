# Structured-Field Recovery — Stage 1 (Tier 1 + 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover structured metadata (level, salary, degree, years) that ATS bulk responses already carry but our providers discard, and fix the correctness bugs where we fetch content and drop it — all at zero extra fetch cost.

**Architecture:** Each provider's `normalize()` maps a structured field it already downloads onto the `JobPosting`. The enrichment layer (`enrich_in_place`) already guards every field ("existing values are never overwritten": `if job.level is JobLevel.UNKNOWN`, `if job.salary is None`, …), so a provider-set structured value wins and the text extractors only fill gaps — **no changes to `enrich.py` are needed**. A shared `level_from_ats_vocab()` maps each ATS's seniority vocabulary to the `JobLevel` enum. Every mapping ships with a **populated-fill live gate** (asserts the field is *populated*, not merely present) and is stress-tested through the real MCP path at the end.

**Tech Stack:** Python 3.10+, pydantic models, pytest, httpx (live gates), the existing `ergon_tracker.extract` parsers (`comp.parse_salary`, `level`).

## Global Constraints

- **Fill rate means POPULATED, never PRESENT.** Recruitee's `salary` object is present 100% but `min`/`max` populated only 11%. Every live gate asserts a populated value, never key presence.
- **Zero new fetches in Stage 1.** Only map bytes already in `raw.payload`. No detail-page fetches (that is Tier 3, a separate plan).
- **Never overwrite a provider value in `enrich.py`.** Rely on the existing guards; do not remove them.
- **Never invent data.** If a structured field is absent/empty, leave the target `None`/`UNKNOWN` — the text extractor will handle it.
- **Live gates must be offline-safe:** marked `@pytest.mark.live` and skipped when `ERGON_LIVE_TESTS` is unset, so CI/offline runs stay green.
- Branch: `structured-field-recovery` (already checked out, off `main`). Commit per task. Trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- `JobLevel` values: `intern, entry, junior, mid, senior, staff, principal, lead, manager, director, executive, unknown`.
- `Salary` fields: `min_amount, max_amount, currency, interval` (`SalaryInterval` enum incl. `YEAR`, `WEEK`, `HOUR`, `MONTH`).

---

## File Structure

- `src/ergon_tracker/extract/level.py` — add `level_from_ats_vocab()` (shared vocab→JobLevel mapper).
- `src/ergon_tracker/providers/{smartrecruiters,jazzhr,workable,join,breezy,personio}.py` — map structured fields in `normalize()`.
- `src/ergon_tracker/providers/{coveo,lever,paycom,taleobe}.py` — correctness-bug fixes.
- `tests/live/conftest.py` — the `live` marker + skip logic (new).
- `tests/live/test_provider_fields_live.py` — populated-fill gates (new).
- `tests/test_provider_field_mapping.py` — synthetic-payload unit tests for each `normalize()` change (new).
- `tests/test_structured_fields_mcp.py` — end-to-end real-MCP stress test (new, final task).

---

### Task 1: Shared `level_from_ats_vocab()` mapper + live-test harness

**Files:**
- Modify: `src/ergon_tracker/extract/level.py`
- Create: `tests/live/conftest.py`
- Test: `tests/test_level_vocab.py`

**Interfaces:**
- Produces: `level_from_ats_vocab(value: str | None) -> JobLevel` — maps ATS seniority strings (SmartRecruiters `"Mid-Senior Level"`, jazzhr `"Experienced"`, workable `"Entry level"`/`"Director"`, themuse `"Senior Level"`, personio `"lead"`) to `JobLevel`; returns `JobLevel.UNKNOWN` for unknown/empty input.
- Produces (test infra): `tests/live/conftest.py` registering `@pytest.mark.live`, auto-skipped unless `ERGON_LIVE_TESTS=1`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_level_vocab.py
import pytest
from ergon_tracker.extract.level import level_from_ats_vocab
from ergon_tracker.models import JobLevel

@pytest.mark.parametrize("raw,expected", [
    ("Entry Level", JobLevel.ENTRY),
    ("entry-level", JobLevel.ENTRY),
    ("Internship", JobLevel.INTERN),
    ("Associate", JobLevel.JUNIOR),
    ("Mid Level", JobLevel.MID),
    ("Mid-Senior Level", JobLevel.SENIOR),
    ("Experienced", JobLevel.MID),
    ("Senior Level", JobLevel.SENIOR),
    ("Staff", JobLevel.STAFF),
    ("Principal", JobLevel.PRINCIPAL),
    ("Lead", JobLevel.LEAD),
    ("Manager/Supervisor", JobLevel.MANAGER),
    ("Director", JobLevel.DIRECTOR),
    ("Executive", JobLevel.EXECUTIVE),
    ("", JobLevel.UNKNOWN),
    (None, JobLevel.UNKNOWN),
    ("Purple Monkey", JobLevel.UNKNOWN),
])
def test_level_from_ats_vocab(raw, expected):
    assert level_from_ats_vocab(raw) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_level_vocab.py -q`
Expected: FAIL with `ImportError: cannot import name 'level_from_ats_vocab'`

- [ ] **Step 3: Implement the mapper**

Add to `src/ergon_tracker/extract/level.py` (near `level_from_years`):

```python
# ATS "seniority/experience-level" vocabularies -> JobLevel. Ordered longest-key-first at match
# time so "mid-senior" wins over "senior"/"mid". Keys are matched against a lowercased, space-and-
# hyphen-normalized form of the input. Unknown/empty -> UNKNOWN (the text extractor then fills in).
_ATS_VOCAB: dict[str, JobLevel] = {
    "internship": JobLevel.INTERN, "intern": JobLevel.INTERN, "trainee": JobLevel.INTERN,
    "entry level": JobLevel.ENTRY, "entry": JobLevel.ENTRY, "graduate": JobLevel.ENTRY,
    "junior": JobLevel.JUNIOR, "associate": JobLevel.JUNIOR,
    "mid senior level": JobLevel.SENIOR, "mid senior": JobLevel.SENIOR,
    "mid level": JobLevel.MID, "mid": JobLevel.MID, "intermediate": JobLevel.MID,
    "experienced": JobLevel.MID, "professional": JobLevel.MID,
    "senior level": JobLevel.SENIOR, "senior": JobLevel.SENIOR,
    "staff": JobLevel.STAFF, "principal": JobLevel.PRINCIPAL,
    "lead": JobLevel.LEAD, "team lead": JobLevel.LEAD,
    "manager supervisor": JobLevel.MANAGER, "manager": JobLevel.MANAGER, "management": JobLevel.MANAGER,
    "director": JobLevel.DIRECTOR, "vp": JobLevel.EXECUTIVE, "executive": JobLevel.EXECUTIVE,
}
_ATS_KEYS = sorted(_ATS_VOCAB, key=len, reverse=True)  # longest-first: "mid senior" before "senior"


def level_from_ats_vocab(value: str | None) -> JobLevel:
    """Map an ATS seniority/experience-level string to JobLevel. Unknown/empty -> UNKNOWN."""
    if not value:
        return JobLevel.UNKNOWN
    norm = " ".join(value.replace("-", " ").replace("/", " ").lower().split())
    for key in _ATS_KEYS:
        if key in norm:
            return _ATS_VOCAB[key]
    return JobLevel.UNKNOWN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_level_vocab.py -q`
Expected: PASS (17 cases)

- [ ] **Step 5: Create the live-test marker harness**

```python
# tests/live/conftest.py
import os
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits real ATS APIs; skipped unless ERGON_LIVE_TESTS=1")

def pytest_collection_modifyitems(config, items):
    if os.environ.get("ERGON_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live test — set ERGON_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
```

- [ ] **Step 6: Run full suite to confirm nothing regressed and live tests skip**

Run: `uv run pytest tests/test_level_vocab.py tests/live -q`
Expected: PASS; live dir collects 0 or shows skips.

- [ ] **Step 7: Commit**

```bash
git add src/ergon_tracker/extract/level.py tests/test_level_vocab.py tests/live/conftest.py
git commit -m "feat(extract): level_from_ats_vocab mapper + live-test harness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: SmartRecruiters `experienceLevel` → level (largest single win, ~150k)

**Files:**
- Modify: `src/ergon_tracker/providers/smartrecruiters.py` (`normalize`, ~line 143)
- Test: `tests/test_provider_field_mapping.py` (new), `tests/live/test_provider_fields_live.py` (new)

**Interfaces:**
- Consumes: `level_from_ats_vocab` (Task 1).

- [ ] **Step 1: Write the populated-fill live gate FIRST (proves the field is real before mapping)**

```python
# tests/live/test_provider_fields_live.py
import json, httpx, pytest
from pathlib import Path

_SEED = json.load(open(Path(__file__).resolve().parents[2] /
    "src/ergon_tracker/registry/data/seed.json"))["companies"]
_H = {"User-Agent": "Mozilla/5.0 (populated-fill gate)"}

def _tokens(ats, n):
    return [e["token"] for e in _SEED.values()
            if isinstance(e, dict) and e.get("ats") == ats and e.get("token")][:n]

@pytest.mark.live
def test_smartrecruiters_experiencelevel_populated():
    tot = filled = 0
    for t in _tokens("smartrecruiters", 12):
        try:
            d = httpx.get(f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=50",
                          headers=_H, timeout=15).json()
        except Exception:
            continue
        for p in d.get("content", []):
            tot += 1
            lvl = (p.get("experienceLevel") or {}).get("label")
            if lvl:
                filled += 1
    assert tot >= 50, f"too few sampled ({tot})"
    assert filled / tot >= 0.80, f"experienceLevel populated only {filled}/{tot}"
```

- [ ] **Step 2: Run the live gate**

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_smartrecruiters_experiencelevel_populated -q`
Expected: PASS (populated ≥ 80%). If it FAILS, stop — the field is not the win claimed; do not map it.

- [ ] **Step 3: Write the synthetic unit test**

```python
# tests/test_provider_field_mapping.py
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider
from ergon_tracker.providers.base import RawJob
from ergon_tracker.models import JobLevel

def _raw(payload):
    return RawJob(source_job_id="1", company="Co", url="http://x", token="co", payload=payload)

def test_smartrecruiters_maps_experience_level():
    p = SmartRecruitersProvider()
    job = p.normalize(_raw({"name": "Engineer", "experienceLevel": {"id": "mid_senior_level",
                            "label": "Mid-Senior Level"}}))
    assert job.level is JobLevel.SENIOR

def test_smartrecruiters_unknown_level_stays_unknown():
    p = SmartRecruitersProvider()
    job = p.normalize(_raw({"name": "Engineer"}))
    assert job.level is JobLevel.UNKNOWN
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py -q`
Expected: FAIL — `job.level` is UNKNOWN because normalize doesn't set it yet.

- [ ] **Step 5: Map the field in `normalize()`**

In `src/ergon_tracker/providers/smartrecruiters.py`, add the import near the top:

```python
from ..extract.level import level_from_ats_vocab
```

In `normalize()`, add a `level=` kwarg to the `JobPosting.create(...)` call:

```python
            department=department,
            level=level_from_ats_vocab((p.get("experienceLevel") or {}).get("label")),
            salary=None,  # not exposed by the listing endpoint (structured comp is detail-only)
```

- [ ] **Step 6: Run both tests to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py -q`
Expected: PASS (both cases)

- [ ] **Step 7: Commit**

```bash
git add src/ergon_tracker/providers/smartrecruiters.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(smartrecruiters): map experienceLevel -> JobLevel (~150k postings)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: jazzhr `<experience>` → level (~59k)

**Files:**
- Modify: `src/ergon_tracker/providers/jazzhr.py` (`normalize` + confirm the XML parse keeps `experience`)
- Test: append to `tests/test_provider_field_mapping.py`, `tests/live/test_provider_fields_live.py`

- [ ] **Step 1: Confirm `experience` reaches `raw.payload`**

Run: `grep -nE "experience|_row_to_dict|findtext|\.tag" src/ergon_tracker/providers/jazzhr.py`
If the XML→dict parse does not already capture the `<experience>` element into the payload dict, add it there (mirror how `<type>`/`<department>` are captured) as the first change. The live gate in Step 2 tells you which case you're in.

- [ ] **Step 2: Write + run the populated-fill live gate**

```python
# append to tests/live/test_provider_fields_live.py
@pytest.mark.live
def test_jazzhr_experience_populated():
    tot = filled = 0
    for t in _tokens("jazzhr", 10):
        try:
            r = httpx.get(f"https://app.jazz.co/feeds/export/jobs/{t}", headers=_H, timeout=15)
        except Exception:
            continue
        import re
        for m in re.findall(r"<experience>(.*?)</experience>", r.text, re.S):
            tot += 1
            if m.replace("<![CDATA[", "").replace("]]>", "").strip():
                filled += 1
    assert tot >= 30 and filled / tot >= 0.80, f"jazzhr experience {filled}/{tot}"
```

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_jazzhr_experience_populated -q`
Expected: PASS (≥80%). Stop if it fails.

- [ ] **Step 3: Write the synthetic unit test**

```python
# append to tests/test_provider_field_mapping.py
def test_jazzhr_maps_experience():
    from ergon_tracker.providers.jazzhr import JazzHRProvider
    p = JazzHRProvider()
    job = p.normalize(_raw({"title": "Engineer", "experience": "Experienced"}))
    assert job.level is JobLevel.MID
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_jazzhr_maps_experience -q`
Expected: FAIL (level UNKNOWN).

- [ ] **Step 5: Map the field**

Add `from ..extract.level import level_from_ats_vocab` near the top of `jazzhr.py`, and add to the `JobPosting.create(...)` call in `normalize()`:

```python
            department=department,
            level=level_from_ats_vocab(p.get("experience")),
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/ergon_tracker/providers/jazzhr.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(jazzhr): map <experience> -> JobLevel (~59k postings)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: workable `experience` → level (~45k)

**Files:**
- Modify: `src/ergon_tracker/providers/workable.py` (`normalize`, ~line 111)
- Test: append to both test files

Note: workable `education` (→ degree) is deferred to Task 10 — its vocabulary and fill need their own verification and a separate `degree_from_ats_vocab` mapper. This task does the verified level win only.

- [ ] **Step 1: Write + run the populated-fill live gate**

```python
# append to tests/live/test_provider_fields_live.py
@pytest.mark.live
def test_workable_experience_populated():
    tot = filled = 0
    for t in _tokens("workable", 12):
        try:
            d = httpx.get(f"https://apply.workable.com/api/v1/widget/accounts/{t}",
                          headers=_H, timeout=15).json()
        except Exception:
            continue
        for p in d.get("jobs", []):
            tot += 1
            if p.get("experience"):
                filled += 1
    assert tot >= 40 and filled / tot >= 0.55, f"workable experience {filled}/{tot}"
```

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_workable_experience_populated -q`
Expected: PASS (≥55%; measured ~62%). Stop if it fails.

- [ ] **Step 2: Write the synthetic unit test**

```python
# append to tests/test_provider_field_mapping.py
def test_workable_maps_experience():
    from ergon_tracker.providers.workable import WorkableProvider
    p = WorkableProvider()
    job = p.normalize(_raw({"title": "Engineer", "experience": "Entry level"}))
    assert job.level is JobLevel.ENTRY
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_workable_maps_experience -q`
Expected: FAIL (level UNKNOWN).

- [ ] **Step 4: Map the field**

Add `from ..extract.level import level_from_ats_vocab` near the top of `workable.py`, and add to the `JobPosting.create(...)` call:

```python
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("experience")),
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ergon_tracker/providers/workable.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(workable): map experience bucket -> JobLevel (~45k postings)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: join structured salary (`salaryAmountFrom/To`, ~15–27k)

**Files:**
- Modify: `src/ergon_tracker/providers/join.py` (`normalize`, replace `salary=None`)
- Test: append to both test files

Context (verified): amounts are in the `__NEXT_DATA__` blob in minor units (÷100), present even when `settings.showSalary` is `false`. `salaryFrequency` is `PER_YEAR`/`PER_MONTH`/etc.

- [ ] **Step 1: Write + run the populated-fill live gate**

```python
# append to tests/live/test_provider_fields_live.py
@pytest.mark.live
def test_join_salary_amount_populated():
    import re
    tot = filled = 0
    for t in _tokens("join", 8):
        try:
            r = httpx.get(f"https://join.com/companies/{t}", headers=_H, timeout=15)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            d = json.loads(m.group(1))
        except Exception:
            continue
        stack = [d]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if o.get("title") and "salaryAmountFrom" in o:
                    tot += 1
                    if o.get("salaryAmountFrom"):
                        filled += 1
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    assert tot >= 30 and filled / tot >= 0.30, f"join salaryAmountFrom {filled}/{tot}"
```

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_join_salary_amount_populated -q`
Expected: PASS (≥30%; measured 35–62%). Stop if it fails.

- [ ] **Step 2: Write the synthetic unit test**

```python
# append to tests/test_provider_field_mapping.py
def test_join_maps_structured_salary():
    from ergon_tracker.providers.join import JoinProvider
    from ergon_tracker.models import SalaryInterval
    p = JoinProvider()
    job = p.normalize(_raw({"title": "Eng", "salaryAmountFrom": 18000000,
                            "salaryAmountTo": 32000000, "salaryCurrency": "USD",
                            "salaryFrequency": "PER_YEAR", "settings": {"showSalary": False}}))
    assert job.salary is not None
    assert job.salary.min_amount == 180000.0 and job.salary.max_amount == 320000.0
    assert job.salary.currency == "USD" and job.salary.interval is SalaryInterval.YEAR

def test_join_no_amount_stays_none():
    from ergon_tracker.providers.join import JoinProvider
    p = JoinProvider()
    assert p.normalize(_raw({"title": "Eng"})).salary is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py -k join -q`
Expected: FAIL (salary None).

- [ ] **Step 4: Add a salary builder + map it**

In `src/ergon_tracker/providers/join.py`, add near the top:

```python
from ..models import Salary, SalaryInterval

_FREQ = {"PER_YEAR": SalaryInterval.YEAR, "PER_MONTH": SalaryInterval.MONTH,
         "PER_WEEK": SalaryInterval.WEEK, "PER_DAY": SalaryInterval.DAY,
         "PER_HOUR": SalaryInterval.HOUR}


def _salary(p: dict) -> Salary | None:
    """Join carries amounts in MINOR units (cents); present even when showSalary is false."""
    lo, hi = p.get("salaryAmountFrom"), p.get("salaryAmountTo")
    if not lo and not hi:
        return None
    return Salary(
        min_amount=(lo / 100) if lo else None,
        max_amount=(hi / 100) if hi else None,
        currency=p.get("salaryCurrency") or None,
        interval=_FREQ.get(str(p.get("salaryFrequency") or "").upper()),
    )
```

(If `SalaryInterval` has no `DAY` member, drop the `PER_DAY` entry — check `from ergon_tracker.models import SalaryInterval; list(SalaryInterval)`.)

Replace `salary=None,  # amounts not exposed in the list blob` in `normalize()` with:

```python
            salary=_salary(p),
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py -k join -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ergon_tracker/providers/join.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(join): map structured salary amounts (~20k boards; incl showSalary=false)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: breezy free-text salary (~11k)

**Files:**
- Modify: `src/ergon_tracker/providers/breezy.py` (`normalize`, replace `salary=None`)
- Test: append to both test files

Context: breezy's feed carries `salary` as a free-text string (`"$78,000 / year"`, `"$1,400 – $1,800 / week"`); `comp.parse_salary` already parses these (verified). Empty string on many boards → `None`.

- [ ] **Step 1: Write + run the populated-fill live gate**

```python
# append to tests/live/test_provider_fields_live.py
@pytest.mark.live
def test_breezy_salary_populated():
    tot = filled = 0
    for t in _tokens("breezy", 12):
        try:
            arr = httpx.get(f"https://{t}.breezy.hr/json", headers=_H, timeout=15).json()
        except Exception:
            continue
        for p in arr:
            tot += 1
            s = p.get("salary")
            if isinstance(s, str) and s.strip():
                filled += 1
    assert tot >= 100 and filled / tot >= 0.30, f"breezy salary {filled}/{tot}"
```

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_breezy_salary_populated -q`
Expected: PASS (≥30%; measured ~38%). Stop if it fails.

- [ ] **Step 2: Write the synthetic unit test**

```python
# append to tests/test_provider_field_mapping.py
def test_breezy_parses_freetext_salary():
    from ergon_tracker.providers.breezy import BreezyProvider
    p = BreezyProvider()
    job = p.normalize(_raw({"name": "Eng", "salary": "$78,000 / year"}))
    assert job.salary is not None and job.salary.min_amount == 78000.0

def test_breezy_empty_salary_stays_none():
    from ergon_tracker.providers.breezy import BreezyProvider
    p = BreezyProvider()
    assert p.normalize(_raw({"name": "Eng", "salary": ""})).salary is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py -k breezy -q`
Expected: FAIL (salary None).

- [ ] **Step 4: Map the field**

Add near the top of `breezy.py`:

```python
from ..extract.comp import parse_salary
```

Replace `salary=None,  # present in feed as a free-text string, not normalized here` with:

```python
            salary=parse_salary(p.get("salary") if isinstance(p.get("salary"), str) else None),
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py -k breezy -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ergon_tracker/providers/breezy.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(breezy): parse the free-text salary the feed already carries (~11k)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: personio `seniority` → level + `yearsOfExperience` → years (~8k; already in `raw`, unpromoted)

**Files:**
- Modify: `src/ergon_tracker/providers/personio.py` (`normalize`)
- Test: append to both test files

Context: the provider docstring already documents `<seniority>` and `<yearsOfExperience>` and `_position_to_dict` captures them into `raw`; `normalize` never promotes them. `yearsOfExperience` is a string range like `"1-2"` / `"lt-1"` / `"5-10"`.

- [ ] **Step 1: Confirm the payload keys**

Run: `ERGON_LIVE_TESTS=1 uv run python -c "import json,httpx; s=json.load(open('src/ergon_tracker/registry/data/seed.json'))['companies']; t=[e['token'] for e in s.values() if isinstance(e,dict) and e.get('ats')=='personio' and e.get('token')][0]; import ergon_tracker.providers.personio as m; print(t)"`
Then inspect one live payload's keys for `seniority` / `yearsOfExperience` (use the provider's own fetch or the documented XML endpoint). Confirm the exact key spelling before writing the map.

- [ ] **Step 2: Write + run the populated-fill live gate**

```python
# append to tests/live/test_provider_fields_live.py
@pytest.mark.live
def test_personio_seniority_populated():
    from ergon_tracker.providers.personio import PersonioProvider
    import anyio
    prov = PersonioProvider()
    tot = filled = 0
    for t in _tokens("personio", 8):
        try:
            raws = anyio.run(prov.fetch, t, None)  # adjust to the provider's fetch signature
        except Exception:
            continue
        for r in raws:
            tot += 1
            if r.payload.get("seniority"):
                filled += 1
    assert tot >= 30 and filled / tot >= 0.70, f"personio seniority {filled}/{tot}"
```

(If the provider fetch signature differs, adapt the call — the point is to sample real payloads and assert `seniority` is populated ≥70%.)

Run: `ERGON_LIVE_TESTS=1 uv run pytest tests/live/test_provider_fields_live.py::test_personio_seniority_populated -q`
Expected: PASS. Stop if it fails.

- [ ] **Step 3: Write the synthetic unit test**

```python
# append to tests/test_provider_field_mapping.py
def test_personio_promotes_seniority_and_years():
    from ergon_tracker.providers.personio import PersonioProvider
    p = PersonioProvider()
    job = p.normalize(_raw({"name": "Eng", "seniority": "senior", "yearsOfExperience": "1-2"}))
    assert job.level is JobLevel.SENIOR
    assert job.years_experience_min == 1 and job.years_experience_max == 2
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_personio_promotes_seniority_and_years -q`
Expected: FAIL.

- [ ] **Step 5: Add a years-range parser + map both fields**

Add near the top of `personio.py`:

```python
from ..extract.level import level_from_ats_vocab

def _years_range(v: str | None) -> tuple[int | None, int | None]:
    """Personio yearsOfExperience: 'lt-1'->(0,1), '1-2'->(1,2), '5-10'->(5,10), 'gt-10'->(10,None)."""
    if not v:
        return (None, None)
    s = v.strip().lower()
    if s.startswith("lt"):
        return (0, 1)
    if s.startswith("gt"):
        import re
        m = re.search(r"\d+", s)
        return ((int(m.group()) if m else None), None)
    import re
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if len(nums) >= 2:
        return (nums[0], nums[1])
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (None, None)
```

In `normalize()`, compute and add the kwargs:

```python
        ymin, ymax = _years_range(p.get("yearsOfExperience"))
```

and in the `JobPosting.create(...)` call:

```python
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("seniority")),
            years_experience_min=ymin,
            years_experience_max=ymax,
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_personio_promotes_seniority_and_years -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/ergon_tracker/providers/personio.py tests/test_provider_field_mapping.py tests/live/test_provider_fields_live.py
git commit -m "feat(personio): promote seniority + yearsOfExperience already in raw (~8k)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Correctness bug — coveo direct-mode drops description + department

**Files:**
- Modify: `src/ergon_tracker/providers/coveo.py` (`normalize` / direct-mode branch)
- Test: append to `tests/test_provider_field_mapping.py`

Context: direct-mode (UST-style) raw items key the description under `data` and the department under `obu`, but `normalize()` reads `description`/`category`, so both silently normalize to `None` despite being 99.5–100% present.

- [ ] **Step 1: Confirm the direct-mode key names**

Run: `grep -nE "obu|\bdata\b|category|description|def normalize|direct" src/ergon_tracker/providers/coveo.py | head -25`
Confirm the direct-mode branch and the actual raw keys (`obu`, `data`) vs what `normalize` reads. Read `scratchpad/inventory-E.md` (coveo section) for the exact field names the agent captured.

- [ ] **Step 2: Write the failing synthetic test**

```python
# append to tests/test_provider_field_mapping.py
def test_coveo_direct_mode_reads_correct_keys():
    from ergon_tracker.providers.coveo import CoveoProvider  # confirm class name
    p = CoveoProvider()
    # direct-mode raw shape: description under 'data', department under 'obu'
    job = p.normalize(_raw({"title": "Eng", "data": "<p>Build things.</p>", "obu": "Engineering"}))
    assert job.description_html and "Build things" in job.description_html
    assert job.department == "Engineering"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_coveo_direct_mode_reads_correct_keys -q`
Expected: FAIL (both None).

- [ ] **Step 4: Fix the key access in the direct-mode branch**

In `coveo.py`, in the direct-mode normalize path, read `data`→description and `obu`→department (falling back to the existing keys for proxy-mode). Show the exact edit against the real code found in Step 1 — read `description` from `p.get("data") or p.get("description")` and `department` from `p.get("obu") or (p.get("category") or {}).get(...)`, matching the proxy-mode fallback. Preserve proxy-mode behavior.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_provider_field_mapping.py::test_coveo_direct_mode_reads_correct_keys -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ergon_tracker/providers/coveo.py tests/test_provider_field_mapping.py
git commit -m "fix(coveo): direct-mode read description from 'data', department from 'obu'

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Correctness bugs — lever `country`, paycom teaser flag, taleobe location

**Files:**
- Modify: `src/ergon_tracker/providers/{lever,paycom,taleobe}.py`
- Test: append to `tests/test_provider_field_mapping.py`

Read `scratchpad/inventory-{A,C,D}.md` for the exact fields before editing each.

- [ ] **Step 1: lever — map top-level `country` ISO into `Location.country`**

Write a failing test asserting a lever payload with top-level `country: "US"` yields a location whose `country` is set; then in `lever.py` `normalize()`, when the location has no country, fall back to the top-level `country` field. Run the test to red→green.

```python
def test_lever_maps_country_iso():
    from ergon_tracker.providers.lever import LeverProvider
    p = LeverProvider()
    job = p.normalize(_raw({"text": "Eng", "categories": {"location": "Remote"}, "country": "US"}))
    assert any(l.country for l in job.locations)
```

- [ ] **Step 2: paycom — flag the truncated description as a teaser, not full JD**

paycom's `description_html` is a hard 153-char preview. Write a test asserting the mapped description is not treated as complete: set a marker so downstream/extraction does not assume full text. Minimal correct fix: stop populating `description_text`/`description_html` from the truncated field (leave them `None`) so paycom is honestly classified as JD-less (Tier-3), OR store it in `raw` only. Choose the option that matches how other snippet-only sources are handled (check `grep -n "snippet\|teaser\|truncat" src/ergon_tracker/providers/*.py`). Add a test pinning the chosen behavior.

- [ ] **Step 3: taleobe — fix the location mis-tag**

Read the taleobe `normalize()` and the inventory-D taleobe section; the 2nd/3rd `<div>` are employment_type/department, currently mis-read as location on 2/3 tenants. Write a synthetic test with the real div ordering asserting location vs employment_type/department land in the right fields; fix the div indexing; red→green.

- [ ] **Step 4: Run all three provider tests**

Run: `uv run pytest tests/test_provider_field_mapping.py -k "lever or paycom or taleobe" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/providers/lever.py src/ergon_tracker/providers/paycom.py src/ergon_tracker/providers/taleobe.py tests/test_provider_field_mapping.py
git commit -m "fix(providers): lever country ISO, paycom teaser honesty, taleobe location

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Aggregator + tail structured fields (themuse, jobicy, himalayas, usajobs, workable education)

**Files:**
- Modify: `src/ergon_tracker/providers/{themuse,jobicy,himalayas,usajobs,workable}.py`
- Create: `degree_from_ats_vocab` in `src/ergon_tracker/extract/degree.py`
- Test: append to both test files

Each sub-step follows the identical pattern: **live populated-fill gate → synthetic unit test → map in `normalize()` → green → (batched commit)**. Read `scratchpad/inventory-E.md` for exact field names/values per source.

- [ ] **Step 1: themuse `levels[]` → level** (100% fill). Gate: `levels` populated ≥90% across sampled boards. Map: `level=level_from_ats_vocab(first level name)`.
- [ ] **Step 2: jobicy `jobLevel` → level, `jobIndustry` → sector** (100%). Gate + map both.
- [ ] **Step 3: himalayas `seniority` → level, `categories` → department** (100%). Gate + map.
- [ ] **Step 4: usajobs `JobGrade`/`LowGrade` → level** (federal GS scale; map GS-05/07/09→entry, 11/12→mid, 13→senior, 14/15→staff via a small `_gs_to_level` dict). Gate + map.
- [ ] **Step 5: workable `education` → degree_min.** Add `degree_from_ats_vocab(value)->str|None` to `degree.py` mapping workable's education strings ("Bachelor's Degree"→"bachelor", "Master's Degree"→"master", "Doctorate"→"phd", "High School"→"high_school") to the `degree_min` enum values used by `degree.py`. Live-gate education populated ≥30% first; then map.
- [ ] **Step 6: Run the batch**: `uv run pytest tests/test_provider_field_mapping.py -q` → PASS.
- [ ] **Step 7: Commit** the batch with message `feat(providers): map tail structured level/sector/degree (themuse, jobicy, himalayas, usajobs, workable education)`.

---

### Task 11: Tail salary + geo (phenom, teamtailor, applicantpro, remotive, recruitee, ceipal, eightfold)

**Files:**
- Modify: `src/ergon_tracker/providers/{phenom,teamtailor,applicantpro,remotive,recruitee,ceipal,eightfold}.py`
- Test: append to both test files

Same pattern per source (live gate → unit test → map → green). Note the **verified** fill rates — do not over-claim:

- [ ] **Step 1: recruitee** — `salary.{min,max}` structured, but **populated only ~11%** (object present ≠ populated). Gate asserts populated ≥8%; map `Salary(min_amount=min, max_amount=max, currency=..., interval=from period)` only when min or max is truthy.
- [ ] **Step 2: teamtailor** — `_jobposting.baseSalary` (schema.org MonetaryAmount), ~12% fill. Gate ≥8%; map.
- [ ] **Step 3: phenom** — `compensationRange`→salary, `experienceLevel`→level, `industry`→sector; bimodal ~12–48%. Gate ≥10%; map all three.
- [ ] **Step 4: applicantpro** — `workplaceType`→remote (71%), `minSalary`/`maxSalary`→salary (~40%). Gate remote ≥60%, salary ≥30%; map.
- [ ] **Step 5: remotive** — free-text salary via `parse_salary` (~67%). Gate ≥50%; map like breezy.
- [ ] **Step 6: ceipal** — structured `pay_rates`→salary + full JD in `requistion_description`/`public_job_desc` (currently unmapped). Gate ≥80%; map salary AND description_html/text (this also moves ceipal toward Tier-2).
- [ ] **Step 7: eightfold** — `standardizedLocations`→structured geo (72%). Gate ≥60%; map into `Location` objects (city/region/country) instead of the raw string.
- [ ] **Step 8: Run the batch**: `uv run pytest tests/test_provider_field_mapping.py -q` → PASS.
- [ ] **Step 9: Commit** with message `feat(providers): map tail salary + structured geo (phenom, teamtailor, applicantpro, remotive, recruitee, ceipal, eightfold)`.

---

### Task 12: End-to-end real-MCP stress test + coverage measurement

**Files:**
- Create: `tests/test_structured_fields_mcp.py`
- Create: `scripts/measure_field_coverage.py`

**Interfaces:**
- Consumes: all provider mappings from Tasks 2–11.

This is the required real-MCP validation: build a small index from synthetic-but-realistic raw payloads for the mapped providers, run it through the actual serving path (`enrich_in_place` + `try_index_ranked` / the MCP `search_jobs` handler), and assert the recovered fields actually change filter results — the same method that surfaced the original gaps.

- [ ] **Step 1: Write the end-to-end test**

```python
# tests/test_structured_fields_mcp.py
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider
from ergon_tracker.providers.base import RawJob
from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.models import JobLevel

def _raw(payload, src="smartrecruiters"):
    return RawJob(source_job_id="1", company="Co", url="http://x", token="co", payload=payload)

def test_smartrecruiters_level_survives_enrichment():
    # The whole point: a provider-set level from experienceLevel must NOT be overwritten by the
    # text extractor (which sees no description for these sources and would return UNKNOWN).
    prov = SmartRecruitersProvider()
    job = prov.normalize(_raw({"name": "Coordinator", "experienceLevel": {"label": "Entry Level"}}))
    enrich_in_place(job)  # runs level.extract etc.
    assert job.level is JobLevel.ENTRY  # provider value preserved end-to-end

def test_breezy_salary_survives_enrichment():
    from ergon_tracker.providers.breezy import BreezyProvider
    job = BreezyProvider().normalize(_raw({"name": "Eng", "salary": "$78,000 / year"}, "breezy"))
    enrich_in_place(job)
    assert job.salary is not None and job.salary.min_amount == 78000.0
```

- [ ] **Step 2: Run it (red before the mappings, green after)**

Run: `uv run pytest tests/test_structured_fields_mcp.py -q`
Expected: PASS (mappings from Tasks 2–11 are in place; enrichment guards preserve them).

- [ ] **Step 3: Write the live coverage-measurement script**

`scripts/measure_field_coverage.py` — reads the local cached index (`~/.cache/ergon-tracker/index.sqlite`), computes level/salary/degree populated-coverage per source, and prints a before/after table. This is the real-index check to run after a rebuild ships the mappings (documents the actual coverage lift, e.g. smartrecruiters level 0%→~100%, index-wide level 43%→~55%+). Not a unit test — an operator tool.

```python
import sqlite3, os
c = sqlite3.connect(os.path.expanduser("~/.cache/ergon-tracker/index.sqlite"))
def cov(where):
    n = c.execute(f"SELECT COUNT(*) FROM jobs WHERE {where} AND expired_at IS NULL").fetchone()[0]
    tot = c.execute("SELECT COUNT(*) FROM jobs WHERE expired_at IS NULL").fetchone()[0]
    return n, tot, n / tot
for label, w in [("level", "level != 'unknown' AND level IS NOT NULL"),
                 ("salary", "salary_min IS NOT NULL OR salary_max IS NOT NULL")]:
    n, tot, r = cov(w)
    print(f"{label:8} {n:>9,}/{tot:,} = {r:.1%}")
for src in ("smartrecruiters", "jazzhr", "workable", "join", "breezy", "personio"):
    n = c.execute(f"SELECT COUNT(*) FROM jobs WHERE source='{src}' AND level!='unknown'").fetchone()[0]
    tot = c.execute(f"SELECT COUNT(*) FROM jobs WHERE source='{src}'").fetchone()[0]
    print(f"  {src:16} level {n:>7,}/{tot:,} = {n/max(tot,1):.0%}")
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_structured_fields_mcp.py scripts/measure_field_coverage.py
git commit -m "test(fields): end-to-end enrichment-preservation test + coverage measurement tool

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 5: Full suite + lint gate**

Run: `uv run pytest -q && uv run ruff check && uv run mypy`
Expected: all green (live tests skip without `ERGON_LIVE_TESTS=1`).

---

## Self-Review

**Spec coverage:** Tier-1 volume wins → Tasks 2–7 (smartrecruiters, jazzhr, workable, join, breezy, personio); Tier-1b bugs → Tasks 8–9 (coveo, lever, paycom, taleobe) — peopleclick/paylocity deferred (peopleclick=40 postings, paylocity=token-space bug flagged as separate ticket in the spec's out-of-scope); tail → Tasks 10–11; populated-fill gate → every task's Step 1–2; real-MCP stress test → Task 12. Tier 2 (audit) and Tier 3 (detail-fetcher) are explicitly separate plans per the spec's staging.

**Placeholder scan:** Tasks 8–11 intentionally reference the inventory files + a Step-1 code-confirmation rather than reproducing enterprise/tail `normalize()` bodies not yet read in full; each still has a concrete test, exact fields, and the established map-in-normalize pattern. The high-volume/high-value tasks (1–7, 12) are fully code-complete.

**Type consistency:** `level_from_ats_vocab(str|None)->JobLevel`, `parse_salary(str|None)->Salary|None`, `Salary(min_amount,max_amount,currency,interval)`, `JobPosting.create(..., level=, salary=, years_experience_min/max=, degree_min=)` — consistent across all tasks. `enrich_in_place` guards (`level is UNKNOWN`, `salary is None`, `years is None`) confirmed against `enrich.py`.

## Execution Handoff

Two execution options — see below.
