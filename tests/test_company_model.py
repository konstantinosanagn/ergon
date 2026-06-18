from ergon_tracker.models import Company


def test_company_defaults_and_fields():
    c = Company(company_key="stripe", display_name="Stripe")
    assert c.company_key == "stripe"
    assert c.open_roles == 0
    assert c.domain is None and c.h1b_sponsor is None


def test_company_is_exported():
    import ergon_tracker.models as m

    assert "Company" in m.__all__
