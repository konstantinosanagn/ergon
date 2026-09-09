# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/konstantinosanagn/ergon-tracker/security/advisories/new).
Do not open a public issue.

Expect an acknowledgement within 7 days. This is a single-maintainer project, so please allow
reasonable time for a fix before public disclosure.

## Supported versions

Only `main` is supported. There are no released versions yet (the package is not on PyPI), so
fixes ship as commits rather than patch releases.

## Scope

The parts of this project most worth scrutiny:

| Area | Why it matters |
| --- | --- |
| `index/cache.py` | Downloads a SQLite index from a GitHub Release and opens it locally. Path handling and sha256 verification are the trust boundary. |
| `mcp_server.py` | Returns crawled third-party text to an LLM agent. Fields are structured and length-bounded (`serialization.py`), but treat any returned string as untrusted. |
| `http.py`, `providers/` | Fetches arbitrary third-party hosts. Redirect handling and per-host limits matter here. |
| `serve/query_app.py` | Network-exposed ASGI app if you choose to run it. |

## What is not a vulnerability

- The prebuilt index is signed only by a sha256 in its own manifest. That detects corruption, not
  a malicious publisher. Anyone who can write the GitHub Release controls both sides of that check.
  Treat the release as trusted infrastructure.
- Missing or stale data in the published index is a reliability bug, not a security issue.

## Handling secrets

`.env` is gitignored and must never be committed. Tier-2 session tokens
(`runs/tier2_tokens.json`) are likewise ignored and are written `0600`. If you believe a
credential was committed, report it privately rather than opening a PR that removes it.
