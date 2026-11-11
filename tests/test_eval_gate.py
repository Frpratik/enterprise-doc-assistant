"""
Stage 8: the EVAL GATE. If a code change makes extraction worse, this test fails.

These are minimum numbers on the golden set. Later (Phase 9) this same test runs in
GitHub Actions on every change, so quality can't silently drop.
"""
import pytest

from ingestion.evaluate import run_eval

MINIMUM = {
    "status_ok": 1.00,        # every file must get exactly the expected status
    "word_recall": 0.95,      # we get at least 95% of the correct text
    "word_precision": 0.95,   # at least 95% of our text is correct (no junk)
    "heading_recall": 0.95,
    "table_accuracy": 0.95,
    "junk_removed": 1.00,     # headers/footers/scripts never left behind
}


@pytest.fixture(scope="module")
def result(samples, tmp_path_factory, monkeypatch_module):
    return run_eval()


@pytest.fixture(scope="module")
def monkeypatch_module():
    """run_eval points settings at a temp folder; put everything back afterwards."""
    from ingestion.config import settings
    saved = settings.model_copy()
    yield
    for field in type(settings).model_fields:
        setattr(settings, field, getattr(saved, field))


@pytest.mark.parametrize("metric,minimum", MINIMUM.items())
def test_quality_does_not_drop(result, metric, minimum):
    assert result["overall"][metric] >= minimum, (
        f"{metric} = {result['overall'][metric]} is below the minimum {minimum}")


def test_no_wrong_status(result):
    wrong = [f"{r['file']}: expected {r['expected']}, got {r['got']}"
             for r in result["documents"] if r["expected"] != r["got"]]
    assert not wrong, wrong
