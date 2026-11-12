import pytest

from ekyc.config import Settings
from ekyc.pipeline.doc_rules import ID12_RE, build_class_pattern, build_rules
from ekyc.pipeline.normalize import fold


def test_rules(settings: Settings) -> None:
    rules = build_rules(settings)
    assert tuple(rule.key for rule in rules) == (
        "cccd_front",
        "cccd_back",
        "gplx_front",
        "cmnd_front",
    )
    for rule in rules:
        for feature in rule.features:
            assert all(phrase == fold(phrase) for phrase in feature.phrases)
            assert "cong hoa xa hoi chu nghia viet nam" not in feature.phrases


@pytest.mark.parametrize(
    "text,matched",
    [
        ("079 123 456 789", True),
        ("079123456789", True),
        ("0791234567890", False),
        ("0 7 9 1 2 3 4 5 6 7 8 9 0", False),
        ("079  123 456 789", False),
        ("07912345678", False),
    ],
)
def test_id12(text: str, matched: bool) -> None:
    assert bool(ID12_RE.search(text)) is matched


@pytest.mark.parametrize(
    "text,matched",
    [
        ("hang/class: a1", True),
        ("hang: b", True),
        ("class b2", True),
        ("hang xe", False),
        ("can cuoc", False),
        ("hang: a10", False),
        ("xhang: b", False),
        ("class/hang - be", True),
    ],
)
def test_class_pattern(settings: Settings, text: str, matched: bool) -> None:
    assert bool(build_class_pattern(settings.license_class_codes).search(text)) is matched


def test_long_code_first() -> None:
    assert build_class_pattern(["A", "A1"]).search("hang: a1").group() == "hang: a1"
    assert build_class_pattern([]).search("hang: a1") is None
