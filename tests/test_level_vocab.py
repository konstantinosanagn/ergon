import pytest

from ergon.extract.level import level_from_ats_vocab
from ergon.models import JobLevel


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Entry Level", JobLevel.ENTRY),
        ("entry-level", JobLevel.ENTRY),
        ("Internship", JobLevel.INTERN),
        ("Associate", JobLevel.JUNIOR),
        ("Mid Level", JobLevel.MID),
        ("Mid-Senior Level", JobLevel.SENIOR),
        ("Experienced", JobLevel.MID),
        ("Senior Level", JobLevel.SENIOR),
        ("Staff", JobLevel.STAFF),
        ("Principal", JobLevel.PRINCIPAL),
        ("Lead", JobLevel.LEAD),
        ("Manager/Supervisor", JobLevel.MANAGER),
        ("Director", JobLevel.DIRECTOR),
        ("Executive", JobLevel.EXECUTIVE),
        ("", JobLevel.UNKNOWN),
        (None, JobLevel.UNKNOWN),
        ("Purple Monkey", JobLevel.UNKNOWN),
    ],
)
def test_level_from_ats_vocab(raw, expected):
    assert level_from_ats_vocab(raw) is expected
