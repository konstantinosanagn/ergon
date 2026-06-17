#!/usr/bin/env python3
"""Deterministic company->sector map from SEC EDGAR SIC codes.

Matches ergon_tracker registry companies (seed.json) to SEC EDGAR public companies
by normalized name / domain stem, fetches each match's SIC via the submissions
API, and maps SIC -> ergon_tracker's 27-label sector vocab.

Free, no API key. Sends a descriptive User-Agent per SEC fair-access policy and
throttles to <=10 req/s. Output: scripts/sector_edgar.json (matched public
companies only). Does NOT modify seed.json / sectors.json.
"""
import json, re, time, os, sys, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "src/ergon_tracker/registry/data/seed.json")
SECTORS = os.path.join(ROOT, "src/ergon_tracker/registry/data/sectors.json")
OUT = os.path.join(ROOT, "scripts/sector_edgar.json")
UA = "ergon_tracker-research konstantinos.a@tavily.com"

SUFFIX = {
    'inc','incorporated','corp','corporation','co','company','companies','llc','lp','llp',
    'ltd','limited','plc','sa','ag','nv','se','oyj','asa','ab','holdings','holding','group',
    'groupe','grp','technologies','technology','systems','solutions','international','intl',
    'global','worldwide','enterprises','industries','industrial','partners','ventures',
    'capital','trust','fund','the','class','common','stock','ord','adr','ads','reit','spa',
    'bv','gmbh','kgaa',
}


def norm(name):
    s = name.lower()
    s = re.sub(r'&', ' and ', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t]
    while toks and toks[-1] in SUFFIX:
        toks.pop()
    return ''.join(toks)


def norm_key(k):
    return re.sub(r'[^a-z0-9]', '', k.lower())


# ---- SIC -> 27-label vocab ----------------------------------------------------
def sic_to_vocab(code):
    """Map a 4-digit SIC code to one of ergon_tracker's 27 sector labels, or None."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return None

    # specific 4-digit codes take precedence
    specific = {
        # software / computer services
        7370: 'Software/SaaS', 7371: 'Software/SaaS', 7372: 'Software/SaaS',
        7373: 'Software/SaaS', 7374: 'Software/SaaS', 7375: 'Software/SaaS',
        7376: 'Software/SaaS', 7377: 'Software/SaaS', 7378: 'Software/SaaS',
        7379: 'Software/SaaS',
        # semiconductors / hardware / computers
        3571: 'Semiconductors/Hardware', 3572: 'Semiconductors/Hardware',
        3575: 'Semiconductors/Hardware', 3576: 'Semiconductors/Hardware',
        3577: 'Semiconductors/Hardware', 3578: 'Semiconductors/Hardware',
        3674: 'Semiconductors/Hardware', 3559: 'Semiconductors/Hardware',
        3672: 'Semiconductors/Hardware', 3827: 'Semiconductors/Hardware',
        # communications equipment / telecom services
        3661: 'Telecom', 3663: 'Telecom', 3669: 'Telecom',
        4810: 'Telecom', 4812: 'Telecom', 4813: 'Telecom', 4822: 'Telecom',
        4899: 'Telecom',
        # media / broadcasting / publishing / motion pictures
        2711: 'Media/Entertainment', 2721: 'Media/Entertainment',
        2731: 'Media/Entertainment', 2741: 'Media/Entertainment',
        4832: 'Media/Entertainment', 4833: 'Media/Entertainment',
        4841: 'Media/Entertainment', 7310: 'Media/Entertainment',
        7311: 'Media/Entertainment', 7812: 'Media/Entertainment',
        7819: 'Media/Entertainment', 7822: 'Media/Entertainment',
        7841: 'Media/Entertainment', 7900: 'Media/Entertainment',
        7990: 'Media/Entertainment', 7997: 'Media/Entertainment',
        # biotech / pharma
        2833: 'Biotech/Pharma', 2834: 'Biotech/Pharma', 2835: 'Biotech/Pharma',
        2836: 'Biotech/Pharma', 8731: 'Biotech/Pharma', 8734: 'Biotech/Pharma',
        3826: 'Biotech/Pharma',  # lab analytical/life-science instruments
        # healthcare (providers / medical devices)
        3841: 'Healthcare', 3842: 'Healthcare',
        3843: 'Healthcare', 3845: 'Healthcare',
        8000: 'Healthcare', 8011: 'Healthcare', 8050: 'Healthcare',
        8060: 'Healthcare', 8062: 'Healthcare', 8071: 'Healthcare',
        8090: 'Healthcare', 8093: 'Healthcare',
        # automotive / mobility
        3711: 'Automotive/Mobility', 3713: 'Automotive/Mobility',
        3714: 'Automotive/Mobility', 3715: 'Automotive/Mobility',
        3716: 'Automotive/Mobility', 3751: 'Automotive/Mobility',
        5500: 'Automotive/Mobility', 5511: 'Automotive/Mobility',
        # aerospace / defense
        3721: 'Aerospace/Defense', 3724: 'Aerospace/Defense',
        3728: 'Aerospace/Defense', 3760: 'Aerospace/Defense',
        3761: 'Aerospace/Defense', 3764: 'Aerospace/Defense',
        3795: 'Aerospace/Defense', 3812: 'Aerospace/Defense',
        # energy / climate (oil, gas, coal, utilities)
        1221: 'Energy/Climate', 1311: 'Energy/Climate', 1381: 'Energy/Climate',
        1382: 'Energy/Climate', 1389: 'Energy/Climate', 2911: 'Energy/Climate',
        2990: 'Energy/Climate', 4911: 'Energy/Climate', 4922: 'Energy/Climate',
        4923: 'Energy/Climate', 4924: 'Energy/Climate', 4931: 'Energy/Climate',
        4941: 'Energy/Climate', 5172: 'Energy/Climate',
        # food / beverage
        5812: 'Food/Beverage', 5813: 'Food/Beverage', 2080: 'Food/Beverage',
        2082: 'Food/Beverage', 2086: 'Food/Beverage',
        # travel / hospitality
        4512: 'Travel/Hospitality', 4513: 'Logistics/SupplyChain',  # air courier
        4522: 'Travel/Hospitality', 4724: 'Travel/Hospitality',
        7011: 'Travel/Hospitality', 7510: 'Travel/Hospitality',
        7011: 'Travel/Hospitality',
        # banking / finance
        6021: 'Banking/Finance', 6022: 'Banking/Finance', 6029: 'Banking/Finance',
        6035: 'Banking/Finance', 6036: 'Banking/Finance', 6199: 'Fintech',
        6141: 'Fintech', 6324: 'Healthcare',  # consumer credit; health-service plans
        6200: 'Banking/Finance', 6211: 'Banking/Finance', 6221: 'Banking/Finance',
        6282: 'Banking/Finance', 6311: 'Insurance', 6321: 'Insurance',
        6311: 'Insurance', 6331: 'Insurance', 6351: 'Insurance',
        6411: 'Insurance',
        # consulting / professional services
        8711: 'Consulting/Services', 8712: 'Consulting/Services',
        8742: 'Consulting/Services', 8748: 'Consulting/Services',
        7363: 'Consulting/Services', 7389: 'Consulting/Services',
        # education
        8200: 'Education', 8211: 'Education', 8221: 'Education',
        8231: 'Education', 8200: 'Education',
        # real estate
        6500: 'RealEstate/PropTech', 6510: 'RealEstate/PropTech',
        6512: 'RealEstate/PropTech', 6531: 'RealEstate/PropTech',
        6552: 'RealEstate/PropTech', 6798: 'RealEstate/PropTech',
        # consumer / lifestyle
        2100: 'Consumer/Lifestyle', 2300: 'Consumer/Lifestyle',
        2844: 'Consumer/Lifestyle', 3911: 'Consumer/Lifestyle',
        3944: 'Consumer/Lifestyle',
    }
    if c in specific:
        return specific[c]

    # range fallbacks (SIC divisions / major groups)
    if 100 <= c <= 999:
        return 'Food/Beverage'           # agriculture
    if 1200 <= c <= 1399:
        return 'Energy/Climate'          # coal, oil & gas
    if 1000 <= c <= 1499:
        return 'Manufacturing/Industrial'  # metal/other mining
    if 1500 <= c <= 1799:
        return 'Manufacturing/Industrial'  # construction
    if 2000 <= c <= 2099:
        return 'Food/Beverage'
    if 2200 <= c <= 2399:
        return 'Consumer/Lifestyle'      # textiles / apparel
    if 2700 <= c <= 2799:
        return 'Media/Entertainment'     # printing & publishing
    if 2800 <= c <= 2899:
        return 'Manufacturing/Industrial'  # chemicals (non-pharma)
    if 2900 <= c <= 2999:
        return 'Energy/Climate'          # petroleum refining
    if 3570 <= c <= 3579:
        return 'Semiconductors/Hardware'
    if 3600 <= c <= 3699:
        return 'Semiconductors/Hardware'  # electronic components
    if 3700 <= c <= 3799:
        return 'Manufacturing/Industrial'  # other transport equip
    if 3800 <= c <= 3829:
        return 'Semiconductors/Hardware'  # instruments
    if 3840 <= c <= 3851:
        return 'Healthcare'              # medical instruments
    if 2000 <= c <= 3999:
        return 'Manufacturing/Industrial'  # catch-all manufacturing
    if 4000 <= c <= 4499:
        return 'Logistics/SupplyChain'   # rail / trucking / water transport
    if 4500 <= c <= 4599:
        return 'Travel/Hospitality'      # air transport
    if 4700 <= c <= 4799:
        return 'Logistics/SupplyChain'   # transportation services
    if 4800 <= c <= 4899:
        return 'Telecom'
    if 4900 <= c <= 4999:
        return 'Energy/Climate'          # utilities
    if 5000 <= c <= 5199:
        return 'Logistics/SupplyChain'   # wholesale
    if 5200 <= c <= 5999:
        return 'E-commerce/Retail'       # retail
    if 6000 <= c <= 6199:
        return 'Banking/Finance'
    if 6200 <= c <= 6299:
        return 'Banking/Finance'
    if 6300 <= c <= 6411:
        return 'Insurance'
    if 6500 <= c <= 6599:
        return 'RealEstate/PropTech'
    if 6700 <= c <= 6799:
        return 'Banking/Finance'         # investment offices / holding
    if 7000 <= c <= 7099:
        return 'Travel/Hospitality'      # hotels
    if 7300 <= c <= 7399:
        return 'Consulting/Services'     # business services (non-software)
    if 7800 <= c <= 7999:
        return 'Media/Entertainment'
    if 8000 <= c <= 8099:
        return 'Healthcare'
    if 8200 <= c <= 8299:
        return 'Education'
    if 8700 <= c <= 8799:
        return 'Consulting/Services'
    if 9000 <= c <= 9899:
        return 'Government/Public'
    return 'Other'


def fetch_url(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA,
                                                       'Accept-Encoding': 'gzip, deflate'})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(1.5 * (i + 1))
                continue
            if e.code == 404:
                return None
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None


def main():
    seed = json.load(open(SEED))['companies']

    # lookup: normalized name/domain-stem -> registry key
    lookup = {}
    for k, v in seed.items():
        nk = norm_key(k)
        if nk:
            lookup.setdefault(nk, k)
        dom = v.get('domain')
        if dom:
            ns = re.sub(r'[^a-z0-9]', '', dom.split('.')[0].lower())
            if ns:
                lookup.setdefault(ns, k)

    tk = fetch_url('https://www.sec.gov/files/company_tickers.json')
    if not tk:
        print("BLOCKED: could not fetch company_tickers.json; writing {}")
        json.dump({}, open(OUT, 'w'))
        return

    # registry key -> cik (first ticker wins)
    matches = {}
    for _, row in tk.items():
        nt = norm(row['title'])
        if len(nt) < 3:
            continue
        rk = lookup.get(nt)
        if rk and rk not in matches:
            matches[rk] = row['cik_str']

    print(f"candidate name/domain matches: {len(matches)}")

    out = {}
    fails = 0
    keys = list(matches.items())
    for i, (rk, cik) in enumerate(keys):
        url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
        data = fetch_url(url)
        time.sleep(0.12)  # ~8 req/s, under SEC's 10/s cap
        if not data:
            fails += 1
            continue
        sic = data.get('sic')
        vocab = sic_to_vocab(sic)
        if vocab and sic:
            out[rk] = {'sector': vocab, 'source': 'edgar', 'sic': str(sic)}
        if (i + 1) % 100 == 0:
            print(f"  fetched {i+1}/{len(keys)} (fails={fails})")

    json.dump(out, open(OUT, 'w'), indent=2, sort_keys=True)
    print(f"WROTE {OUT}: {len(out)} companies (fetch fails={fails})")

    # ---- validate vs curated sectors.json ----
    curated = json.load(open(SECTORS))['companies']
    both = [k for k in out if k in curated]
    agree = sum(1 for k in both if out[k]['sector'] == curated[k]['sector'])
    print(f"\nmatched public companies: {len(out)}")
    print(f"coverage of registry (22115): {100*len(out)/len(seed):.2f}%")
    print(f"overlap with curated (1453): {len(both)}")
    if both:
        print(f"accuracy vs curated: {agree}/{len(both)} = {100*agree/len(both):.1f}%")
    from collections import Counter
    cnt = Counter(v['sector'] for v in out.values())
    print("\nSIC->vocab distribution:")
    for s, n in cnt.most_common():
        print(f"  {n:4d}  {s}")

    # disagreements sample for inspection
    diff = [(k, out[k]['sic'], out[k]['sector'], curated[k]['sector'])
            for k in both if out[k]['sector'] != curated[k]['sector']]
    print(f"\ndisagreements ({len(diff)}), sample:")
    for k, sic, e, c in diff[:30]:
        print(f"  {k:24s} sic={sic} edgar={e:24s} curated={c}")


if __name__ == '__main__':
    main()
