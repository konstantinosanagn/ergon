from ergon_tracker.canonicalize import aggregate_companies
from ergon_tracker.models import JobPosting


def _job(company, **kw):
    t = kw.pop("t", "")
    return JobPosting.create(
        source="greenhouse", source_job_id=company + t, company=company, title="Engineer", **kw
    )


def test_aggregate_keys_by_normalized_name_and_counts_open_roles():
    jobs = [_job("Stripe, Inc.", t="1"), _job("STRIPE INC", t="2"), _job("Acme GmbH", t="3")]
    by_key = {c.company_key: c for c in aggregate_companies(jobs)}
    assert by_key["stripe"].open_roles == 2
    assert by_key["acme"].open_roles == 1
    assert by_key["stripe"].display_name in ("Stripe, Inc.", "STRIPE INC")


def test_aggregate_fills_domain_and_sector_when_present():
    jobs = [
        _job("Stripe", company_domain=None, sector=None, t="1"),
        _job("Stripe", company_domain="stripe.com", sector="Fintech", t="2"),
    ]
    c = aggregate_companies(jobs)[0]
    assert c.domain == "stripe.com" and c.sector == "Fintech"
