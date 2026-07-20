"""Discovery helper: fetch candidate careers pages for a domain and grep for ATS markers.

Usage:  .venv/bin/python scripts/discover_shops.py <domain-or-url> [<domain-or-url> ...]

For each input it fetches a set of likely careers/jobs URLs with curl_cffi (chrome
impersonation, no browser) and reports any ATS markers found, including extracted
ceipal api_key / cp_id, oorwin slug hints, WP-REST awsm endpoints, greenhouse/lever/etc.
"""

from __future__ import annotations

import re
import sys

from curl_cffi import requests

PATHS = [
    "",
    "/careers",
    "/careers/",
    "/career",
    "/careers/jobs",
    "/jobs",
    "/jobs/",
    "/current-openings",
    "/open-positions",
    "/job-openings",
    "/careers/current-openings",
    "/wp-json/wp/v2/awsm_job_openings?per_page=100",
    "/wp-json/wp/v2/jobpost?per_page=100",
    "/wp-json/wp/v2/job-listings?per_page=100",
    "/wp-json/wp/v2/vacancy?per_page=100",
    "/feed/?post_type=jobs",
    "/api/jobs",
]

CEIPAL_API = re.compile(r'data-ceipal-api-key\s*=\s*["\']([^"\']+)["\']', re.I)
CEIPAL_CP = re.compile(r'data-ceipal-career-portal-id\s*=\s*["\']([^"\']+)["\']', re.I)
# JS-injected variants
CEIPAL_API_JS = re.compile(r'ceipal[_-]?api[_-]?key["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.I)
CEIPAL_CP_JS = re.compile(r'(?:career[_-]?portal[_-]?id|cp[_-]?id)["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.I)

MARKERS = {
    "ceipal": re.compile(r"ceipal", re.I),
    "oorwin": re.compile(r"oorwin", re.I),
    "zwayam": re.compile(r"zwayam", re.I),
    "awsm_job": re.compile(r"awsm_job_openings|awsm-job", re.I),
    "greenhouse": re.compile(r"greenhouse\.io|boards\.greenhouse", re.I),
    "lever": re.compile(r"lever\.co|jobs\.lever", re.I),
    "smartrecruiters": re.compile(r"smartrecruiters", re.I),
    "workable": re.compile(r"workable\.com", re.I),
    "recruitee": re.compile(r"recruitee\.com", re.I),
    "jazzhr": re.compile(r"applytojob\.com|jazz\.co", re.I),
    "bamboohr": re.compile(r"bamboohr\.com", re.I),
    "jobvite": re.compile(r"jobvite", re.I),
    "icims": re.compile(r"icims\.com", re.I),
    "bullhorn": re.compile(r"bullhorn", re.I),
    "jobdiva": re.compile(r"jobdiva", re.I),
}


def fetch(url: str) -> tuple[int, str]:
    try:
        r = requests.get(url, impersonate="chrome124", timeout=20, verify=False,
                         allow_redirects=True)
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        return -1, f"ERR {type(e).__name__}: {e}"


def normalize(inp: str) -> str:
    inp = inp.strip()
    if inp.startswith("http"):
        return inp.rstrip("/")
    return "https://" + inp.strip("/")


def main(argv: list[str]) -> None:
    for raw in argv:
        base = normalize(raw)
        print(f"\n===== {base} =====")
        for path in PATHS:
            url = base + path if path else base
            code, text = fetch(url)
            if code < 0:
                # only print connection errors once per host root
                if path == "":
                    print(f"  [{path or '/'}] {text}")
                continue
            hits = [name for name, rx in MARKERS.items() if rx.search(text)]
            api = CEIPAL_API.search(text) or CEIPAL_API_JS.search(text)
            cp = CEIPAL_CP.search(text) or CEIPAL_CP_JS.search(text)
            extra = ""
            if api:
                extra += f"  API_KEY={api.group(1)}"
            if cp:
                extra += f"  CP_ID={cp.group(1)}"
            # WP-REST JSON length hint
            note = ""
            if "/wp-json/" in path and text.strip().startswith("["):
                id_marker = '"id"'
                note = f"  JSON_ARRAY len~{text.count(id_marker)}"
            if hits or extra or note:
                print(f"  [{path or '/'}] {code} hits={hits}{extra}{note}")
            elif code in (200,) and path in ("", "/careers", "/jobs"):
                print(f"  [{path or '/'}] {code} (no markers)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
