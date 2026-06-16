# Curated company registry CSVs

This directory is the **human contribution path** for the jobspine seed registry: hand-curated
companies that we want auto-discovery to know about, one ATS per file.

## Contribution model

- **One ATS per CSV.** The file name is the ATS: `greenhouse.csv`, `lever.csv`, `ashby.csv`,
  `workday.csv`, `smartrecruiters.csv`, `workable.csv`, `recruitee.csv`, `personio.csv`. A row's
  `ats` cell may be left blank — it then defaults to the file stem.
- **One company per row**, sorted by name.
- **Header row required:** `name,ats,token,domain`.
  - `name` — human company name. The registry *key* is slugified from it (lowercase, hyphenated).
  - `ats` — optional; defaults to the file stem.
  - `token` — the board token / slug / tenant. For **workday** rows this is the pipe-composite
    `tenant|wd|site` (e.g. `nvidia|wd5|NVIDIAExternalCareerSite`).
  - `domain` — optional; leave empty for none.
- Lines that are blank or start with `#` are ignored.

## How a PR is verified

Open a PR that touches `data/companies/**`. CI (`.github/workflows/registry-verify.yml`) runs:

```
python scripts/ingest_companies_csv.py --out candidates_csv.json
python scripts/build_registry.py candidates_csv.json --dry-run
```

The ingest step flattens the CSVs into candidates; the `--dry-run` build step **fetches each new
company live through jobspine's own providers** and prints verified/dead counts. It never writes
`seed.json`. A company that doesn't verify live shows up as DEAD in the CI log — fix the token.

Duplicates are harmless: `build_registry.py` skips any company key already in `seed.json`, so
re-listing a known company changes nothing.

## Finding tokens

- **Greenhouse:** the board slug in `boards.greenhouse.io/{token}` (usually the company name).
- **Lever:** the slug in `jobs.lever.co/{token}`.
- **Ashby:** the org slug in `jobs.ashbyhq.com/{token}` (case-sensitive).
- **Workday:** open the careers site; the URL is
  `https://{tenant}.wd{N}.myworkdayjobs.com/{site}` — combine as `tenant|wdN|site`.
- **SmartRecruiters / Workable / Recruitee / Personio:** the tenant slug in the respective
  careers URL.
