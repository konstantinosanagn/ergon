from __future__ import annotations

from ergon_tracker.extract.sector_features import (
    TLD_VOCAB,
    build_input_text,
    tld_features,
)


def test_input_text_combines_name_domain_title() -> None:
    # domain contributes its registrable label WITHOUT the TLD (the TLD is a separate feature).
    txt = build_input_text("Acme Corp", "careers.acme-bank.com", "Senior Risk Analyst")
    assert "Acme Corp" in txt
    assert "acme-bank" in txt
    assert ".com" not in txt
    assert "Senior Risk Analyst" in txt


def test_input_text_tolerates_missing_fields() -> None:
    assert build_input_text(None, None, None) == ""
    assert build_input_text("Acme", None, None) == "Acme"


def test_tld_features_are_fixed_width_and_grouped() -> None:
    vec = tld_features("foo.ai")
    assert len(vec) == len(TLD_VOCAB)
    assert sum(vec) == 1.0  # .ai lights exactly its group
    assert tld_features(None) == [0.0] * len(TLD_VOCAB)  # no domain → all-zero
    assert tld_features("foo.unknown-tld") == [0.0] * len(TLD_VOCAB)
