"""The P9 promotion gate: every condition must hold, and each one alone blocks."""

from __future__ import annotations

import pytest

from pinny.benchmark.errors import BenchmarkError
from pinny.benchmark.gate import build_promotion_report

CONDITIONS = ["recall_not_below_baseline", "precision_within_margin", "meaningful_improvement",
              "min_test_documents", "min_reference_points", "not_synthetic_only"]


def _mode(tp, fp, fn, model_ids=None, pages=("p1", "p2", "p3")):
    return {
        "model_ids": model_ids or {},
        "counts": {"true_positives": tp, "false_positives": fp, "false_negatives": fn},
        "pages": [{"page_key": k} for k in pages],
    }


def summary(base=(80, 20, 20), cand=(85, 18, 15), *, docs=3, refs=100, synthetic=False,
            candidate_mode="template+verifier", tolerance=12):
    kind = {"template+verifier": "verifier", "model": "detector"}[candidate_mode]
    return {
        "format": "pinny.benchmark", "format_version": 1, "created_at": "2026-09-24T00:00:00Z",
        "dataset": {"dataset_id": "ds-1", "synthetic_only": synthetic},
        "split": "test", "label": "real drawings, verified reference", "tolerance_px": tolerance,
        "page_count": 3, "document_count": docs, "reference_points": refs,
        "modes": {
            "template": _mode(*base),
            candidate_mode: _mode(*cand, model_ids={kind: f"{kind}-20260924T000000Z-abcdef12"}),
        },
    }


def test_all_conditions_pass_promotes():
    r = build_promotion_report(summary(), "template+verifier")
    assert r["format"] == "pinny.promotion" and r["format_version"] == 1
    assert r["promote"] is True
    assert r["failed_conditions"] == []
    assert [c["name"] for c in r["conditions"]] == CONDITIONS
    assert all("value" in c and c["passed"] for c in r["conditions"])
    assert r["model_id"] == "verifier-20260924T000000Z-abcdef12"
    assert r["kind"] == "verifier"
    assert (r["dataset_id"], r["split"], r["tolerance_px"]) == ("ds-1", "test", 12)


def test_model_mode_candidate_names_detector():
    r = build_promotion_report(summary(candidate_mode="model"), "model")
    assert r["promote"] is True and r["kind"] == "detector"
    assert r["model_id"].startswith("detector-")


@pytest.mark.parametrize("kwargs, failing", [
    # baseline recall 0.8; candidate recall 79/100 = 0.79 < 0.8 (precision up a lot)
    ({"cand": (79, 1, 21)}, "recall_not_below_baseline"),
    # baseline precision 0.8; candidate 90/(90+30)=0.75 < 0.78 (recall up to 0.9)
    ({"cand": (90, 30, 10)}, "precision_within_margin"),
    # tiny improvement only: recall 81/100, precision 81/101
    ({"cand": (81, 20, 19)}, "meaningful_improvement"),
    ({"docs": 2}, "min_test_documents"),
    ({"refs": 99}, "min_reference_points"),
    ({"synthetic": True}, "not_synthetic_only"),
])
def test_each_failing_condition_blocks_promotion(kwargs, failing):
    r = build_promotion_report(summary(**kwargs), "template+verifier")
    assert r["promote"] is False
    assert r["failed_conditions"] == [failing]
    cond = {c["name"]: c for c in r["conditions"]}[failing]
    assert cond["passed"] is False and "value" in cond


def test_boundaries_are_inclusive_and_exact():
    base = (400, 100, 100)  # precision 0.8, recall 0.8
    # Precision exactly 0.02 lower (390/500 = 0.78) passes the margin; recall 0.78 does not hold.
    r = build_promotion_report(summary(base=base, cand=(390, 110, 110)), "template+verifier")
    by = {c["name"]: c for c in r["conditions"]}
    assert by["precision_within_margin"]["passed"] is True
    assert by["recall_not_below_baseline"]["passed"] is False
    # Recall exactly 0.02 higher (410/500 = 0.82) is a meaningful improvement.
    r = build_promotion_report(summary(base=base, cand=(410, 102, 90)), "template+verifier")
    by = {c["name"]: c for c in r["conditions"]}
    assert by["meaningful_improvement"]["passed"] is True
    assert r["promote"] is True


def test_just_below_improvement_margin_fails():
    base = (400, 100, 100)
    cand = (409, 102, 91)            # recall 0.818 (+0.018), precision 0.8004
    r = build_promotion_report(summary(base=base, cand=cand), "template+verifier")
    assert r["failed_conditions"] == ["meaningful_improvement"]


def test_synthetic_never_promotes_even_with_perfect_scores():
    r = build_promotion_report(
        summary(base=(50, 50, 50), cand=(1000, 0, 0), docs=50, refs=1000, synthetic=True),
        "template+verifier")
    assert r["promote"] is False
    assert r["failed_conditions"] == ["not_synthetic_only"]
    assert r["label"] == "synthetic — not real-drawing accuracy"


def test_undefined_metrics_block_promotion():
    r = build_promotion_report(summary(cand=(0, 0, 100)), "template+verifier")
    assert r["promote"] is False
    assert "recall_not_below_baseline" in r["failed_conditions"]
    assert "precision_within_margin" in r["failed_conditions"]


def test_rejects_wrong_tolerance_and_mismatched_pages():
    with pytest.raises(BenchmarkError) as e:
        build_promotion_report(summary(tolerance=10), "template+verifier")
    assert e.value.code == "invalid_tolerance"
    s = summary()
    s["modes"]["template+verifier"]["pages"] = [{"page_key": "other"}]
    with pytest.raises(BenchmarkError) as e:
        build_promotion_report(s, "template+verifier")
    assert e.value.code == "page_mismatch"


def test_rejects_baseline_as_candidate_and_missing_mode():
    with pytest.raises(BenchmarkError):
        build_promotion_report(summary(), "template")
    with pytest.raises(BenchmarkError) as e:
        build_promotion_report(summary(), "model")
    assert e.value.code == "mode_not_benchmarked"
