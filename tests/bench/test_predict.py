from scripts.bench.predict import predict


def test_predict_reads_back_extractor_values():
    row = {
        "id": "greenhouse:1",
        "source": "greenhouse",
        "company": "Acme",
        "title": "Senior Software Engineer",
        "description_text": "5+ years of experience. Bachelor's degree required. $150,000-$180,000 USD.",
        "location_raw": "New York, NY",
        "structured_salary": None,
    }
    p = predict(row)
    assert p["level"] == "senior"
    assert p["country"] == "United States" and p["city"] == "New York"
    assert p["salary"]["min"] == 150000 and p["salary"]["currency"] == "USD"
    assert p["yoe"]["min"] == 5
    assert p["degree"] in {"bachelor", "bachelors"}
