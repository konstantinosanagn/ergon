# Extraction Gold-Labeling Guide

Label each posting by reading its `title`, `description_text`, and `location_raw`. Output one
JSON object per input row, preserving `id`, `source`, `company_key`, `title`,
`description_text`, `location_raw`, `structured_salary`, and adding a `gold` object.

**Principle: label what the posting actually states. Never guess. Use the "unknown"/null
fallback when the posting doesn't say.**

## `gold` fields

### level  (string, required)
One of: `intern, entry, junior, mid, senior, staff, principal, lead, manager, director,
executive, unknown`.
- Judge the role's seniority from the title (and description if title is ambiguous).
- Map company-specific ladders to the closest rung: "Member of Technical Staff" → usually
  `mid` unless qualified ("Senior MTS" → `senior`); "MTS-3"/"E5"/"P4"/"IC4" → `senior`;
  "Engineer II"/"SDE II" → `mid`; "Engineer III" → `senior`; "Engineer I" → `entry`.
- "Senior Manager" → `manager` (management track wins over the senior modifier).
- No seniority signal at all (e.g. plain "Software Engineer") → `mid` ONLY if the description
  implies experience; otherwise `unknown`. Prefer `unknown` when genuinely unmarked.

### sector  (string or null)
The company's industry, one of the labels in `sectors.json` vocabulary (Software/SaaS, AI/ML,
Fintech, Banking/Finance, Insurance, Crypto/Web3, Healthcare, Biotech/Pharma,
Semiconductors/Hardware, Cybersecurity, Gaming, Media/Entertainment, E-commerce/Retail,
Consumer/Lifestyle, Telecom, Automotive/Mobility, Aerospace/Defense, Energy/Climate,
Logistics/SupplyChain, Education, RealEstate/PropTech, Consulting/Services,
Manufacturing/Industrial, Travel/Hospitality, Food/Beverage, Government/Public, Other).
Judge from the company, not the role. `null` only if you truly cannot tell.

### country / city  (string or null)
From `location_raw`. `country` = canonical country name ("United States", "United Kingdom",
"Germany"...). `city` = primary city if stated. Remote-only with no place → both `null`.
For multi-location ("Berlin / London"), use the FIRST. US "City, ST" → city + country
"United States".

### remote  (bool)
`true` if the posting is remote or hybrid; else `false`.

### employment_type  (string)
One of: `full_time, part_time, contract, internship, temporary, other, unknown`.
- Judge from what the posting states (title, an explicit "Employment Type" field, or phrasing in
  the description like "This is a contract position" / "6-month internship").
- `other` = the posting states a type that isn't one of the above (e.g. "seasonal", "apprenticeship").
- `unknown` = the posting doesn't say. Do NOT default to `full_time` just because that's the most
  common case — only label it when the posting actually states or clearly implies it (e.g. a
  standard salaried listing with benefits and no other type mentioned is still `unknown` unless
  the posting says "full-time").

### salary  (object or null)
`{"min": <number|null>, "max": <number|null>, "currency": "USD"|..., "interval":
"year"|"hour"|"month"|"week"|"day"}` — ONLY if the posting states pay (in `structured_salary`
OR in the description text). Numbers are absolute (150000, not "150k"). `null` if no pay stated.
Do not infer from market norms.

### yoe  (object or null)
`{"min": <int|null>, "max": <int|null>}` — the required years of experience if stated
("5+ years" → {min:5,max:null}; "3-5 years" → {min:3,max:5}). `null` if not stated. Ignore
non-experience durations (vesting, "founded N years ago", tenure of benefits).

### posted_at  (string or null)  — recency
ISO date `"YYYY-MM-DD"` for the date the POSTING ITSELF states it went live ("Posted on...",
"Date posted:", a dated header/metadata field, etc.). This is the STATED date, never the date you
are labeling on, never inferred from "how stale this reads," and never derived from unrelated
dates in the text (an application deadline, a start date, "founded in 2019"). `null` if the
posting doesn't state when it was posted.

### visa_sponsor  (bool or null)
Whether the EMPLOYER is a known H-1B sponsor. This is company-level, positive-evidence-only, and
is **matched against the DoL LCA sponsor dataset by company name/domain — it is never inferred
from the job description text.** Do not read the posting for sponsorship language to label this
field — what the POSTING itself says about sponsorship (e.g. "we are able to sponsor a visa" /
"must not require sponsorship") is a separate, posting-level signal, distinct from this
employer-level DoL match.
- `true` only when the company matches a confirmed entry in the DoL set.
- `null` (not `false`) when the company doesn't appear in the DoL set — absence of a match is NOT
  evidence the employer doesn't sponsor (the dataset is historical/incomplete); never guess `false`.
- When labeling a sample for precision-checking, verify the matched employer name/domain actually
  corresponds to the DoL record (not a same-named different company) before confirming `true`.

## Output format
Write JSONL (one object per line) to your assigned output path. Each line = the input row plus
the `gold` object. Do not reorder or drop rows.
