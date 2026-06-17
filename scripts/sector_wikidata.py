#!/usr/bin/env python3
"""Deterministic company -> sector map from Wikidata.

Strategy (free Wikidata SPARQL endpoint, batched POST queries):
  1. Domain match: companies with a website domain -> Wikidata P856 (official website).
  2. Label match: exact rdfs:label / skos:altLabel match on case-variants derived
     from the company key, requiring the entity to have P452 (industry).
For every matched entity we pull its P452 industry label(s) + sitelink count,
pick the most-notable entity (max sitelinks) per company, then map the industry
label to exactly one of our 27-label vocabulary.

Writes scripts/sector_wikidata.json. Does NOT touch seed.json / sectors.json.
"""
import json, os, sys, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEED = os.path.join(ROOT, "src/ergon_tracker/registry/data/seed.json")
SECTORS = os.path.join(ROOT, "src/ergon_tracker/registry/data/sectors.json")
OUT = os.path.join(HERE, "sector_wikidata.json")
RAW_CACHE = os.path.join(HERE, ".sector_wd_raw.jsonl")  # resumable raw rows

UA = "ergon_tracker-sector-mapper/1.0 (konstantinos.a@tavily.com; deterministic sector map)"
EP = "https://query.wikidata.org/sparql"

VOCAB = ["Software/SaaS","AI/ML","Fintech","Banking/Finance","Insurance","Crypto/Web3",
 "Healthcare","Biotech/Pharma","Semiconductors/Hardware","Cybersecurity","Gaming",
 "Media/Entertainment","E-commerce/Retail","Consumer/Lifestyle","Telecom",
 "Automotive/Mobility","Aerospace/Defense","Energy/Climate","Logistics/SupplyChain",
 "Education","RealEstate/PropTech","Consulting/Services","Manufacturing/Industrial",
 "Travel/Hospitality","Food/Beverage","Government/Public","Other"]

# Priority order for resolving a company that has multiple mapped industries.
# More specific / informative sectors win over generic ones. Software/SaaS is kept
# above AI/ML so that a company tagging both "software" and "AI" resolves to
# Software/SaaS (matches curated tendency); a pure-AI entity (only the AI tag)
# still resolves to AI/ML.
PRIORITY = ["Crypto/Web3","Biotech/Pharma","Semiconductors/Hardware","Cybersecurity",
 "Fintech","Gaming","Aerospace/Defense","Automotive/Mobility","Energy/Climate",
 "Insurance","Banking/Finance","Healthcare","Telecom","Logistics/SupplyChain",
 "RealEstate/PropTech","Education","Food/Beverage","Media/Entertainment",
 "Travel/Hospitality","E-commerce/Retail","Consumer/Lifestyle",
 "Manufacturing/Industrial","Government/Public","Consulting/Services",
 "Software/SaaS","AI/ML","Other"]

# Wikidata industry label (lowercased) -> our vocab.
IND_MAP = {
 # software / saas / it
 "software industry":"Software/SaaS","software development":"Software/SaaS",
 "software":"Software/SaaS","enterprise software":"Software/SaaS",
 "software as a service":"Software/SaaS","saas":"Software/SaaS",
 "cloud computing":"Software/SaaS","information technology":"Software/SaaS",
 "information technology consulting":"Consulting/Services",
 "it performance management":"Software/SaaS","computer software":"Software/SaaS",
 "technology industry":"Software/SaaS","high tech":"Software/SaaS",
 "information and communications technology":"Software/SaaS",
 "internet":"Software/SaaS","internet industry":"Software/SaaS",
 "world wide web":"Software/SaaS","web service":"Software/SaaS",
 "web hosting service":"Software/SaaS","software publishers":"Software/SaaS",
 "computer":"Software/SaaS","computing":"Software/SaaS",
 "customer relationship management":"Software/SaaS","automation":"Software/SaaS",
 "business software":"Software/SaaS","developer tools":"Software/SaaS",
 "data management":"Software/SaaS","database":"Software/SaaS","analytics":"Software/SaaS",
 "big data":"Software/SaaS","data science":"AI/ML","infrastructure platform":"Software/SaaS",
 "computer hardware and software":"Software/SaaS",
 # ai
 "artificial intelligence":"AI/ML","machine learning":"AI/ML",
 "intelligent agent":"AI/ML","computer vision":"AI/ML","robotics":"AI/ML",
 "natural language processing":"AI/ML","generative artificial intelligence":"AI/ML",
 # fintech / finance
 "fintech":"Fintech","financial technology":"Fintech","mobile payment industry":"Fintech",
 "payment":"Fintech","payments":"Fintech","payment system":"Fintech",
 "digital banking":"Fintech","online banking":"Fintech",
 "financial services":"Banking/Finance","finance":"Banking/Finance",
 "banking":"Banking/Finance","bank":"Banking/Finance","investment banking":"Banking/Finance",
 "investment management":"Banking/Finance","asset management":"Banking/Finance",
 "wealth management":"Banking/Finance","capital market":"Banking/Finance",
 "private equity":"Banking/Finance","venture capital":"Banking/Finance",
 "stock exchange":"Banking/Finance","brokerage":"Banking/Finance",
 "accounting":"Banking/Finance","credit":"Banking/Finance",
 "financial market":"Banking/Finance","mortgage":"Banking/Finance",
 # insurance
 "insurance":"Insurance","insurance industry":"Insurance","reinsurance":"Insurance",
 "health insurance":"Insurance",
 # crypto
 "cryptocurrency":"Crypto/Web3","cryptocurrency industry":"Crypto/Web3",
 "cryptocurrency exchange":"Crypto/Web3","bitcoin":"Crypto/Web3","blockchain":"Crypto/Web3",
 "ethereum":"Crypto/Web3","web3":"Crypto/Web3","digital currency":"Crypto/Web3",
 # healthcare
 "health care":"Healthcare","healthcare":"Healthcare","health":"Healthcare",
 "health technology":"Healthcare","digital health":"Healthcare","medicine":"Healthcare",
 "medical device":"Healthcare","medical devices":"Healthcare","hospital":"Healthcare",
 "telehealth":"Healthcare","mental health":"Healthcare","dentistry":"Healthcare",
 "health services industry":"Healthcare","medical technology":"Healthcare",
 "nursing":"Healthcare","fitness":"Consumer/Lifestyle",
 # biotech / pharma
 "pharmaceutical industry":"Biotech/Pharma","pharmaceutical":"Biotech/Pharma",
 "pharmaceuticals":"Biotech/Pharma","biotechnology":"Biotech/Pharma",
 "biotech":"Biotech/Pharma","life sciences":"Biotech/Pharma","genomics":"Biotech/Pharma",
 "drug":"Biotech/Pharma","chemical industry":"Manufacturing/Industrial",
 # semiconductors / hardware
 "semiconductor industry":"Semiconductors/Hardware","semiconductor":"Semiconductors/Hardware",
 "semiconductors":"Semiconductors/Hardware","consumer electronics industry":"Semiconductors/Hardware",
 "electronics industry":"Semiconductors/Hardware","electronics":"Semiconductors/Hardware",
 "consumer electronics":"Semiconductors/Hardware","electrical industry":"Semiconductors/Hardware",
 "computer hardware":"Semiconductors/Hardware","networking hardware":"Semiconductors/Hardware",
 "hardware":"Semiconductors/Hardware","integrated circuit":"Semiconductors/Hardware",
 "electrical equipment":"Semiconductors/Hardware","electronic engineering":"Semiconductors/Hardware",
 # cybersecurity
 "computer security":"Cybersecurity","cybersecurity":"Cybersecurity",
 "information security":"Cybersecurity","internet security":"Cybersecurity",
 "network security":"Cybersecurity","security":"Cybersecurity",
 # gaming
 "video game industry":"Gaming","video game":"Gaming","video games":"Gaming",
 "gaming":"Gaming","game":"Gaming","esports":"Gaming","gambling":"Gaming",
 "interactive entertainment":"Gaming",
 # media / entertainment
 "media industry":"Media/Entertainment","mass media":"Media/Entertainment",
 "media":"Media/Entertainment","streaming media":"Media/Entertainment",
 "entertainment":"Media/Entertainment","film industry":"Media/Entertainment",
 "music industry":"Media/Entertainment","music":"Media/Entertainment",
 "broadcasting":"Media/Entertainment","television":"Media/Entertainment",
 "publishing":"Media/Entertainment","news media":"Media/Entertainment",
 "advertising":"Media/Entertainment","online advertising":"Media/Entertainment",
 "internet marketing":"Media/Entertainment","marketing":"Media/Entertainment",
 "social media":"Media/Entertainment","social network":"Media/Entertainment",
 "digital media":"Media/Entertainment","newspaper":"Media/Entertainment",
 "animation":"Media/Entertainment","film":"Media/Entertainment",
 "public relations":"Media/Entertainment","content":"Media/Entertainment",
 # e-commerce / retail
 "e-commerce":"E-commerce/Retail","retail":"E-commerce/Retail",
 "online shopping":"E-commerce/Retail","retail industry":"E-commerce/Retail",
 "electronic commerce":"E-commerce/Retail","online marketplace":"E-commerce/Retail",
 "marketplace":"E-commerce/Retail","wholesale":"E-commerce/Retail",
 "grocery store":"E-commerce/Retail","supermarket":"E-commerce/Retail",
 "department store":"E-commerce/Retail","fashion":"Consumer/Lifestyle",
 # consumer / lifestyle
 "consumer goods":"Consumer/Lifestyle","cosmetics":"Consumer/Lifestyle",
 "consumer products":"Consumer/Lifestyle","apparel":"Consumer/Lifestyle",
 "clothing industry":"Consumer/Lifestyle","footwear":"Consumer/Lifestyle",
 "luxury goods":"Consumer/Lifestyle","beauty":"Consumer/Lifestyle",
 "personal care":"Consumer/Lifestyle","furniture":"Consumer/Lifestyle",
 "toys":"Consumer/Lifestyle","sporting goods":"Consumer/Lifestyle",
 "household goods":"Consumer/Lifestyle",
 # telecom
 "telecommunications":"Telecom","telecommunications industry":"Telecom",
 "telecommunication":"Telecom","mobile network operator":"Telecom",
 "voice over ip":"Telecom","wireless":"Telecom","internet service provider":"Telecom",
 "mobile telephony":"Telecom","broadband":"Telecom",
 # automotive / mobility
 "automotive industry":"Automotive/Mobility","automotive":"Automotive/Mobility",
 "car":"Automotive/Mobility","automobile":"Automotive/Mobility",
 "electric vehicle":"Automotive/Mobility","ride sharing":"Automotive/Mobility",
 "transportation":"Logistics/SupplyChain","mobility":"Automotive/Mobility",
 "automobile manufacturing":"Automotive/Mobility","motorcycle":"Automotive/Mobility",
 # aerospace / defense
 "aerospace":"Aerospace/Defense","aerospace industry":"Aerospace/Defense",
 "aerospace manufacturer":"Aerospace/Defense","defense industry":"Aerospace/Defense",
 "arms industry":"Aerospace/Defense","aviation":"Aerospace/Defense",
 "space industry":"Aerospace/Defense","spaceflight":"Aerospace/Defense",
 "military":"Aerospace/Defense","aeronautics":"Aerospace/Defense",
 # energy / climate
 "energy industry":"Energy/Climate","energy":"Energy/Climate",
 "renewable energy":"Energy/Climate","solar energy":"Energy/Climate",
 "oil and gas industry":"Energy/Climate","petroleum industry":"Energy/Climate",
 "electric power industry":"Energy/Climate","electricity":"Energy/Climate",
 "utility":"Energy/Climate","cleantech":"Energy/Climate","solar power":"Energy/Climate",
 "wind power":"Energy/Climate","nuclear power":"Energy/Climate",
 "oil":"Energy/Climate","natural gas":"Energy/Climate","mining":"Energy/Climate",
 # logistics / supply chain
 "logistics":"Logistics/SupplyChain","supply chain":"Logistics/SupplyChain",
 "supply chain management":"Logistics/SupplyChain","shipping":"Logistics/SupplyChain",
 "freight":"Logistics/SupplyChain","courier":"Logistics/SupplyChain",
 "warehousing":"Logistics/SupplyChain","delivery":"Logistics/SupplyChain",
 "postal service":"Logistics/SupplyChain","transport":"Logistics/SupplyChain",
 # education
 "education":"Education","educational technology":"Education","e-learning":"Education",
 "higher education":"Education","education industry":"Education","training":"Education",
 "school":"Education","university":"Education",
 # real estate / proptech
 "real estate":"RealEstate/PropTech","real property":"RealEstate/PropTech",
 "property management":"RealEstate/PropTech","construction":"Manufacturing/Industrial",
 "real estate industry":"RealEstate/PropTech","real estate development":"RealEstate/PropTech",
 # consulting / services
 "consulting":"Consulting/Services","management consulting":"Consulting/Services",
 "professional services":"Consulting/Services","business services":"Consulting/Services",
 "human resources":"Consulting/Services","staffing":"Consulting/Services",
 "recruitment":"Consulting/Services","outsourcing":"Consulting/Services",
 "legal services":"Consulting/Services","law":"Consulting/Services",
 "law firm":"Consulting/Services","customer service":"Consulting/Services",
 "facility management":"Consulting/Services","engineering":"Consulting/Services",
 "design":"Consulting/Services","architecture":"Consulting/Services",
 # manufacturing / industrial
 "manufacturing":"Manufacturing/Industrial","industrial":"Manufacturing/Industrial",
 "machinery":"Manufacturing/Industrial","heavy industry":"Manufacturing/Industrial",
 "steel industry":"Manufacturing/Industrial","industry":"Manufacturing/Industrial",
 "engineering industry":"Manufacturing/Industrial","metalworking":"Manufacturing/Industrial",
 "industrial manufacturing":"Manufacturing/Industrial","factory":"Manufacturing/Industrial",
 "agriculture":"Manufacturing/Industrial","textile industry":"Manufacturing/Industrial",
 "plastics":"Manufacturing/Industrial","packaging":"Manufacturing/Industrial",
 "paper industry":"Manufacturing/Industrial","industrial design":"Manufacturing/Industrial",
 # travel / hospitality
 "tourism industry":"Travel/Hospitality","tourism":"Travel/Hospitality",
 "hospitality industry":"Travel/Hospitality","hospitality":"Travel/Hospitality",
 "travel":"Travel/Hospitality","travel agency":"Travel/Hospitality",
 "hotel":"Travel/Hospitality","airline":"Travel/Hospitality",
 "restaurant":"Food/Beverage","leisure":"Travel/Hospitality",
 # food / beverage
 "food industry":"Food/Beverage","food":"Food/Beverage","beverage":"Food/Beverage",
 "food and drink":"Food/Beverage","food processing":"Food/Beverage",
 "brewing":"Food/Beverage","beverage industry":"Food/Beverage",
 "food and beverage":"Food/Beverage","drink industry":"Food/Beverage",
 "agribusiness":"Food/Beverage","dairy":"Food/Beverage","food delivery":"Food/Beverage",
 # government / public
 "government":"Government/Public","public sector":"Government/Public",
 "public administration":"Government/Public","nonprofit":"Government/Public",
 "non-profit organization":"Government/Public","ngo":"Government/Public",
 # extras (seen as unmapped on sample / common Wikidata industry labels)
 "biotechnology industry":"Biotech/Pharma","biopharmaceutical":"Biotech/Pharma",
 "cosmetics industry":"Consumer/Lifestyle","consumer goods industry":"Consumer/Lifestyle",
 "bedding":"Consumer/Lifestyle","sporting activities":"Consumer/Lifestyle",
 "consulting company":"Consulting/Services","management consulting industry":"Consulting/Services",
 "consultation":"Consulting/Services","auditing":"Consulting/Services",
 "accounting services":"Consulting/Services","it service management":"Software/SaaS",
 "it service":"Consulting/Services","it services":"Consulting/Services",
 "data analytics software industry":"Software/SaaS","cloud storage":"Software/SaaS",
 "data analytics":"Software/SaaS","computer programming":"Software/SaaS",
 "medical technology industry":"Healthcare","human health activities":"Healthcare",
 "central banking":"Banking/Finance","financial service":"Banking/Finance",
 "financial service activities, except insurance and pension funding":"Banking/Finance",
 "advertising agency":"Media/Entertainment","digital marketing":"Media/Entertainment",
 "mobile advertising":"Media/Entertainment","app store optimization":"Media/Entertainment",
 "journalism":"Media/Entertainment","pornography industry":"Media/Entertainment",
 "video on demand":"Media/Entertainment","podcast":"Media/Entertainment",
 "postal sector":"Logistics/SupplyChain","mass surveillance":"Cybersecurity",
 "open-source intelligence":"Cybersecurity","computer and network surveillance":"Cybersecurity",
 "data security":"Cybersecurity","identity management":"Cybersecurity",
 "environment":"Energy/Climate","environmental":"Energy/Climate",
 "waste management":"Energy/Climate","water industry":"Energy/Climate",
 "aquaculture":"Food/Beverage","fishing":"Food/Beverage","winemaking":"Food/Beverage",
 "real estate investment trust":"RealEstate/PropTech","coworking":"RealEstate/PropTech",
 "transportation industry":"Logistics/SupplyChain","railroad":"Logistics/SupplyChain",
 "maritime transport":"Logistics/SupplyChain","trucking industry":"Logistics/SupplyChain",
 # second round of unmapped labels seen on the full run
 "rail transport":"Logistics/SupplyChain","public transport":"Logistics/SupplyChain",
 "passenger transport":"Logistics/SupplyChain","transport industry":"Logistics/SupplyChain",
 "air transport":"Travel/Hospitality","air transportation":"Travel/Hospitality",
 "shipbuilding":"Manufacturing/Industrial","metal industry":"Manufacturing/Industrial",
 "creative industries":"Media/Entertainment","anime industry":"Media/Entertainment",
 "market research":"Consulting/Services","financial sector":"Banking/Finance",
 "computer hardware industry":"Semiconductors/Hardware",
 "health care industry":"Healthcare","health technology industry":"Healthcare",
 "bicycle industry":"Consumer/Lifestyle","game industry":"Gaming",
 "it systems and software consulting":"Consulting/Services","professional service":"Consulting/Services",
 "business software industry":"Software/SaaS","cannabis industry":"Consumer/Lifestyle",
 "technology":"Software/SaaS","information technology industry":"Software/SaaS",
 "business and other management consultancy activities":"Consulting/Services",
 "economics of banking":"Banking/Finance","quantum computing industry":"Semiconductors/Hardware",
 "aerospace engineering":"Aerospace/Defense","aircraft":"Aerospace/Defense",
 "business and professional associations, unions":"Government/Public",
 "research and development":"Consulting/Services",
 "construction industry":"Manufacturing/Industrial","building materials":"Manufacturing/Industrial",
 "fashion industry":"Consumer/Lifestyle","jewellery":"Consumer/Lifestyle",
 "tobacco industry":"Consumer/Lifestyle","retail banking":"Banking/Finance",
 "social media industry":"Media/Entertainment","search engine":"Software/SaaS",
 # third round of unmapped labels seen on the 42k full run
 "electric power generation, transmission and distribution":"Energy/Climate",
 "electricity generation":"Energy/Climate","public utility":"Energy/Climate",
 "fast food":"Food/Beverage","sports industry":"Consumer/Lifestyle",
 "internet portal":"Software/SaaS","computer network":"Software/SaaS",
 "film production":"Media/Entertainment","show business":"Media/Entertainment",
 "publishing house":"Media/Entertainment","publishing industry":"Media/Entertainment",
 "weapons industry":"Aerospace/Defense","aircraft industry":"Aerospace/Defense",
 "commercial aviation":"Travel/Hospitality","taxi service":"Automotive/Mobility",
 "hospitals and rehabilitation":"Healthcare","hospital industry":"Healthcare",
 "hedge fund":"Banking/Finance","crowdfunding":"Fintech",
 "iron and steel industry":"Manufacturing/Industrial",
 "manufacture of machinery and equipment":"Manufacturing/Industrial",
 "product packaging industry":"Manufacturing/Industrial",
 "educational system":"Education","e-commerce industry":"E-commerce/Retail",
 "food manufacturing":"Food/Beverage","confectionery":"Food/Beverage",
}

def variants(slug):
    out = set()
    s = slug.strip()
    if not s: return out
    for v in (s, s.upper(), s.capitalize(), s.title()):
        out.add(v)
    if "-" in s or "_" in s:
        h = s.replace("-", " ").replace("_", " ")
        for v in (h, h.title(), h.capitalize(), h.upper()):
            out.add(v)
    # drop pure-numeric / single char noise
    return {v for v in out if len(v) >= 2 and not v.isdigit()}

def sparql(q, tries=4):
    data = urllib.parse.urlencode({"query": q, "format": "json"}).encode()
    last = None
    for i in range(tries):  # noqa
        try:
            req = urllib.request.Request(EP, data=data, headers={
                "User-Agent": UA, "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded"})
            return json.loads(urllib.request.urlopen(req, timeout=35).read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(12, 2 * (i + 1))); continue
            raise
        except Exception as e:
            last = e; time.sleep(min(12, 2 * (i + 1)))
    raise RuntimeError("sparql failed: %s" % last)

def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')

def label_query(cands):
    vals = " ".join('"%s"@en' % esc(c) for c in cands)
    return ('SELECT ?cand ?company ?sl ?indLabel WHERE { VALUES ?cand { %s } '
            '?company rdfs:label|skos:altLabel ?cand . '
            '?company wdt:P452 ?ind . ?company wikibase:sitelinks ?sl . '
            '?ind rdfs:label ?indLabel . FILTER(LANG(?indLabel)="en") }' % vals)

def domain_url_variants(d):
    d = d.lower().strip().rstrip("/")
    hosts = [d] + ([d[4:]] if d.startswith("www.") else ["www." + d])
    out = []
    for h in hosts:
        for scheme in ("https://", "http://"):
            out.append(scheme + h + "/")
            out.append(scheme + h)
    return out

def domain_query(url_to_dom):
    # exact-IRI match against the official-website index (fast, no scan)
    vals = " ".join("<%s>" % esc(u) for u in url_to_dom)
    return ('SELECT ?url ?company ?sl ?indLabel WHERE { VALUES ?url { %s } '
            '?company wdt:P856 ?url . '
            '?company wdt:P452 ?ind . ?company wikibase:sitelinks ?sl . '
            '?ind rdfs:label ?indLabel . FILTER(LANG(?indLabel)="en") }' % vals)

def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def map_industry(label):
    return IND_MAP.get(label.strip().lower())

def resolve_sector(industries):
    """industries: set of raw labels -> one vocab via priority, else None."""
    mapped = set()
    for raw in industries:
        v = map_industry(raw)
        if v: mapped.add(v)
    if not mapped:
        return None
    for p in PRIORITY:
        if p in mapped:
            return p
    return sorted(mapped)[0]

RAW_JSON = os.path.join(HERE, ".sector_wd_raw.json")
DONE_JSON = os.path.join(HERE, ".sector_wd_done.json")

def main():
    sample = None
    if "--sample" in sys.argv:
        sample = int(sys.argv[sys.argv.index("--sample") + 1])
    from_cache = "--from-cache" in sys.argv
    companies = json.load(open(SEED))["companies"]
    curated = json.load(open(SECTORS))["companies"]

    if from_cache:
        rawc = json.load(open(RAW_JSON))
        raw = {k: {q: {"sl": v["sl"], "ind": set(v["ind"])} for q, v in qm.items()}
               for k, qm in rawc.items()}
        return finish(raw, companies, curated, sample=None)

    keys = list(companies.keys())
    if sample:
        # sample from keys that are in curated (to measure recall+accuracy)
        import random
        random.seed(42)
        pool = [k for k in keys if k in curated]
        keys = random.sample(pool, min(sample, len(pool)))

    # ---- domain pass ----
    dom_to_keys = {}
    for k in keys:
        d = companies[k].get("domain")
        if d:
            dom_to_keys.setdefault(d.lower().strip(), []).append(k)
    # raw[key] = {qid: {"sl":int, "ind":set()}}
    raw = {}
    # resume: reload previous checkpoint if present
    if not sample and os.path.exists(RAW_JSON):
        try:
            prev = json.load(open(RAW_JSON))
            raw = {k: {q: {"sl": v["sl"], "ind": set(v["ind"])} for q, v in qm.items()}
                   for k, qm in prev.items()}
            print("[resume] loaded %d cached keys" % len(raw), file=sys.stderr)
        except Exception:
            raw = {}
    done = set()
    if not sample and os.path.exists(DONE_JSON):
        try:
            done = set(json.load(open(DONE_JSON)))
            print("[resume] %d label batches already done" % len(done), file=sys.stderr)
        except Exception:
            done = set()
    def add_row(key, qid, sl, ind):
        e = raw.setdefault(key, {}).setdefault(qid, {"sl": 0, "ind": set()})
        e["sl"] = max(e["sl"], int(sl)); e["ind"].add(ind)

    # build url -> domain map for exact-IRI matching
    url_to_dom = {}
    for dom in dom_to_keys:
        for u in domain_url_variants(dom):
            url_to_dom[u] = dom
    urls = sorted(url_to_dom.keys())  # deterministic order
    failed = 0
    print("[domain] %d domains, %d url variants, %d batches" % (len(dom_to_keys), len(urls), (len(urls)+99)//100), file=sys.stderr)
    for bi, batch in enumerate(chunk(urls, 100)):
        try:
            d = sparql(domain_query(batch))
        except Exception as e:
            failed += 1; print("  [skip domain batch %d] %s" % (bi, e), file=sys.stderr); continue
        for b in d["results"]["bindings"]:
            dom = url_to_dom.get(b["url"]["value"])
            for key in dom_to_keys.get(dom, []):
                add_row(key, b["company"]["value"].split("/")[-1], b["sl"]["value"], b["indLabel"]["value"])
        if bi % 5 == 0: print("  domain batch %d" % bi, file=sys.stderr)
        time.sleep(0.2)

    # ---- label pass ----
    cand_to_keys = {}
    for k in keys:
        for v in variants(k):
            cand_to_keys.setdefault(v, []).append(k)
    cands = sorted(cand_to_keys.keys())  # deterministic order (stable batches)
    nb = (len(cands)+149)//150
    print("[label] %d candidate strings in %d batches" % (len(cands), nb), file=sys.stderr)

    def save_cache():
        if sample: return
        rawc = {k: {q: {"sl": v["sl"], "ind": sorted(v["ind"])} for q, v in qm.items()}
                for k, qm in raw.items()}
        json.dump(rawc, open(RAW_JSON, "w"))
        json.dump(sorted(done), open(DONE_JSON, "w"))

    for bi, batch in enumerate(chunk(cands, 150)):
        if bi in done:
            continue
        try:
            d = sparql(label_query(batch))
        except Exception as e:
            failed += 1; print("  [skip label batch %d/%d] %s" % (bi, nb, e), file=sys.stderr); continue
        for b in d["results"]["bindings"]:
            cand = b["cand"]["value"]
            for key in cand_to_keys.get(cand, []):
                add_row(key, b["company"]["value"].split("/")[-1], b["sl"]["value"], b["indLabel"]["value"])
        done.add(bi)
        if bi % 10 == 0:
            print("  label batch %d/%d (matched keys so far=%d, failed=%d)" % (bi, nb, len(raw), failed), file=sys.stderr)
        if bi % 100 == 0:
            save_cache()  # periodic checkpoint
        time.sleep(0.15)

    # ---- persist raw rows for offline re-resolution ----
    save_cache()
    print("cached raw -> %s (%d keys, %d failed batches)" % (RAW_JSON, len(raw), failed), file=sys.stderr)

    return finish(raw, companies, curated, sample)

def finish(raw, companies, curated, sample):
    # ---- resolve ----
    result = {}
    unmapped = {}
    for key, qmap in raw.items():
        # pick most-notable entity (max sitelinks)
        best_qid = max(qmap, key=lambda q: qmap[q]["sl"])
        inds = qmap[best_qid]["ind"]
        sec = resolve_sector(inds)
        if sec is None:
            for raw_l in inds:
                unmapped[raw_l.lower()] = unmapped.get(raw_l.lower(), 0) + 1
            continue
        # pick a representative raw industry label that produced the sector
        wd_ind = next((r for r in sorted(inds) if map_industry(r) == sec), sorted(inds)[0])
        result[key] = {"sector": sec, "source": "wikidata", "wd_industry": wd_ind, "wd_qid": best_qid}

    # ---- write ----
    if not sample:
        json.dump(result, open(OUT, "w"), indent=0, sort_keys=True)
        print("wrote %s (%d companies)" % (OUT, len(result)), file=sys.stderr)

    # ---- validate ----
    both = [k for k in result if k in curated]
    correct = sum(1 for k in both if result[k]["sector"] == curated[k]["sector"])
    print("\n=== RESULTS ===")
    print("companies matched : %d" % len(result))
    print("seed total        : %d" % len(companies))
    print("coverage          : %.1f%%" % (100.0 * len(result) / len(companies)))
    print("overlap w/ curated: %d" % len(both))
    if both:
        print("accuracy vs curated: %.1f%% (%d/%d)" % (100.0*correct/len(both), correct, len(both)))
    if unmapped:
        top = sorted(unmapped.items(), key=lambda x: -x[1])[:30]
        print("\nTop UNMAPPED industry labels (label,count):")
        for l, c in top: print("  %4d  %s" % (c, l))
    # confusion sample
    if both:
        miss = [(k, result[k]["sector"], curated[k]["sector"], result[k]["wd_industry"]) for k in both if result[k]["sector"] != curated[k]["sector"]]
        print("\nSample mismatches (key, ours, curated, wd_industry):")
        for row in miss[:25]: print("  ", row)

if __name__ == "__main__":
    main()
