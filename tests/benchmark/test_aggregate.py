"""Scores are summed over pages as TP/FP/FN, then divided (P9), not averaged."""

from pinny.benchmark.aggregate import aggregate_counts, metrics


def test_summed_not_averaged():
    pages = [
        {"true_positives": 1, "false_positives": 0, "false_negatives": 0},   # P=1.0, R=1.0
        {"true_positives": 9, "false_positives": 9, "false_negatives": 91},  # P=0.5, R=0.09
    ]
    counts = aggregate_counts(pages)
    assert counts == {"pages": 2, "true_positives": 10, "false_positives": 9,
                      "false_negatives": 91, "predictions": 19, "references": 101}
    m = metrics(counts)
    assert m["precision"]["value"] == 10 / 19
    assert m["recall"]["value"] == 10 / 101
    # The per-page averages would be 0.75 and 0.545; they must not appear.
    assert m["precision"]["value"] != (1.0 + 0.5) / 2
    assert m["recall"]["value"] != (1.0 + 0.09) / 2


def test_zero_denominators_are_undefined_not_zero():
    m = metrics(aggregate_counts([{"true_positives": 0, "false_positives": 0, "false_negatives": 0}]))
    assert m["precision"]["value"] is None and m["precision"]["undefined_reason"]
    assert m["recall"]["value"] is None and m["recall"]["undefined_reason"]
