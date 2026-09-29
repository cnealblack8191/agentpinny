"""Per-symbol-type packages: judging one match, the check on reviews the
package was not trained on, the gate, and the index of packages."""

from __future__ import annotations

import json

import numpy as np
import pytest

from pinny.detection.verifier import crop_with_margin
from pinny.model import ModelPackageError
from pinny.model.judge import CheckResult, Example, cross_check, gate, judge_crop
from pinny.model.symbols import SymbolIndex, train_symbol

from tests.model.test_package import make_page


def examples(seeds):
    out = []
    for seed in seeds:
        page, real, fake = make_page(seed)
        out += [Example(crop_with_margin(page, b), True, f"sheet{seed}") for b in real]
        out += [Example(crop_with_margin(page, b), False, f"sheet{seed}") for b in fake]
    return out


@pytest.fixture(scope="module")
def original():
    page, real, _ = make_page(1)
    b = next(b for b in real if b.height == 36)
    return page[b.y:b.y2, b.x:b.x2].copy()


@pytest.fixture(scope="module")
def trained(original):
    return train_symbol("D", original, examples((1, 2, 3)))


def test_a_package_throws_out_look_alikes_and_keeps_the_symbol(trained):
    assert trained.package is not None and trained.package.verifier is not None and trained.package.negatives
    page, real, fake = make_page(7)  # a sheet it never saw
    kept = [judge_crop(trained.package, crop_with_margin(page, b)).accept for b in real]
    dropped = [not judge_crop(trained.package, crop_with_margin(page, b)).accept for b in fake]
    assert all(kept) and all(dropped)


def test_the_check_uses_only_reviews_the_package_did_not_see(trained):
    c = trained.check
    assert c.groups == 3 and c.approved == 15 and c.rejected == 15 and c.skipped is None
    assert c.approved_kept + c.approved_lost == 15 and c.rejected_caught + c.rejected_missed == 15
    assert c.approved_lost == 0 and c.rejected_caught == 15
    assert trained.active and "caught 15 of 15" in trained.reason


def test_the_gate_needs_two_sheets_and_a_rejection(original):
    one = train_symbol("D", original, examples((1,)))
    assert not one.active and "two sheets" in one.reason and one.package is not None
    no_neg = [e for e in examples((1, 2)) if e.approved]
    t = train_symbol("D", original, no_neg)
    assert not t.active and "rejected" in t.reason
    none = train_symbol("D", original, [e for e in examples((1,)) if not e.approved])
    assert none.package is None and not none.active and "Approve" in none.reason


def test_the_gate_refuses_a_package_that_loses_correct_matches():
    assert not gate(CheckResult(groups=3, approved=20, rejected=5, approved_kept=18, approved_lost=2,
                                rejected_caught=5))["ok"]
    assert not gate(CheckResult(groups=3, approved=20, rejected=5, approved_kept=20, rejected_missed=5))["ok"]
    assert gate(CheckResult(groups=3, approved=40, rejected=5, approved_kept=39, approved_lost=1,
                            rejected_caught=3, rejected_missed=2))["ok"]


def test_cross_check_skips_a_group_with_nothing_left_to_learn_from(original):
    ex = examples((1,)) + [Example(e.crop, False, "sheet9") for e in examples((2,)) if not e.approved]
    calls = []

    def train(rest):
        calls.append({e.group for e in rest})
        from pinny.model.symbols import _package
        return _package("D", original, rest)
    res = cross_check(ex, train)
    # Judging sheet1 needs approvals elsewhere, and sheet9 has none: skipped.
    assert calls == [{"sheet1"}] and res.rejected_caught + res.rejected_missed == 5


def test_index_saves_packages_and_lists_only_intact_active_ones(tmp_path, trained, original):
    idx = SymbolIndex(tmp_path)
    assert idx.read() == {} and idx.active() == {}
    e = idx.save("D", trained, trained_by="a@b.c")
    assert e["active"] and e["file"] and e["templates"] >= 1 and e["negatives"] >= 1 and e["verifier"]
    assert set(idx.active()) == {"D"}
    one = train_symbol("G", original, examples((1,)))
    idx.save("G", one)
    assert set(idx.active()) == {"D"}  # G did not pass its check
    with pytest.raises(ModelPackageError) as err:
        idx.set_active("G", True)
    assert err.value.code == "symbol_model_not_checked"
    idx.set_active("D", False)
    assert idx.active() == {}
    idx.set_active("D", True)
    (idx.root / e["file"]).write_bytes(b"changed")  # a tampered package is never used
    assert idx.active() == {}
    body = json.loads(idx.index_path.read_text())
    assert body["format"] == "pinny.symbol_models" and set(body["symbols"]) == {"D", "G"}
