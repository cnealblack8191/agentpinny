"""Judge a match with a symbol type's learned package, and check a package
on reviews it was not trained on.

The whole-set scanner finds symbols by matching the legend drawing
(docs/set-scanning.md). A learned package (``train_model``) adds what the
reviews taught about one symbol type: rejected look-alikes (the negative
veto) and a verifier trained on approved and rejected crops.
:func:`judge_crop` applies both to one match, given its contracts §6 crop
(box plus ``CROP_MARGIN_PX``).

:func:`cross_check` estimates what that would have done on reviews the
package never saw: each group (a drawing, or a sheet when there is only
one drawing) is judged by a package trained on the other groups. A package
is only switched on when that check shows it throws out rejected matches
without losing approved ones (:func:`gate`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from pinny.detection.template_bank import NegativeBank, ncc, rotate_quarter, to_gray, trim_margin
from pinny.detection.types import DetectionError
from pinny.detection.verifier import CROP_MARGIN_PX

from .package import ModelPackage

#: A package may lose at most this share of held-out approved matches ...
MAX_LOST_SHARE = 0.05
#: ... and must throw out at least this many held-out rejected ones.
MIN_CAUGHT = 1


@dataclass(frozen=True)
class Judgement:
    accept: bool
    #: "ok", "looks_like_rejected" (negative veto) or "verifier".
    reason: str
    verifier_p: Optional[float] = None
    positive_similarity: Optional[float] = None
    negative_similarity: Optional[float] = None


def _positive_similarity(tight: np.ndarray, package: ModelPackage, rotation: int, mirrored: bool) -> float:
    gray = to_gray(tight)
    best = -1.0
    for t in package.templates:
        g = to_gray(t.image)
        if mirrored:
            g = np.ascontiguousarray(g[:, ::-1])
        best = max(best, ncc(gray, rotate_quarter(g, rotation)))
    return best


def judge_crop(package: ModelPackage, crop: np.ndarray, *, rotation: int = 0, mirrored: bool = False,
               tight: Optional[np.ndarray] = None) -> Judgement:
    """Keep or throw out one match. ``crop`` is its §6 crop (box plus
    ``CROP_MARGIN_PX``); ``tight`` is the box alone, if known (else the
    margin is trimmed off ``crop``; a crop clipped at the page edge then
    skips the look-alike check). The verifier decides only when the package
    chose it as its decision rule (it has enough approved and rejected
    examples)."""
    neg = pos = None
    if package.negatives:
        if tight is None:
            try:
                tight = trim_margin(crop, CROP_MARGIN_PX)
            except DetectionError:
                tight = None
        if tight is not None:
            bank = NegativeBank(crops=[to_gray(n) for n in package.negatives])
            neg = float(bank.max_similarity(tight))
            pos = float(_positive_similarity(tight, package, rotation % 360, mirrored))
            if neg > pos + package.veto_margin:
                return Judgement(False, "looks_like_rejected", None, pos, neg)
    vp = None
    if package.verifier is not None:
        vp = float(package.verifier.score([crop])[0])
        if package.decision.method == "verifier" and vp < package.decision.threshold:
            return Judgement(False, "verifier", vp, pos, neg)
    return Judgement(True, "ok", vp, pos, neg)


@dataclass(frozen=True)
class Example:
    """One reviewed match: its §6 crop, whether it was approved, and the
    group (drawing or sheet) it belongs to."""

    crop: np.ndarray
    approved: bool
    group: str
    rotation: int = 0


@dataclass
class CheckResult:
    groups: int
    approved: int
    rejected: int
    approved_kept: int = 0
    approved_lost: int = 0
    rejected_caught: int = 0
    rejected_missed: int = 0
    #: Why the check could not run, if it could not.
    skipped: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def cross_check(examples: Sequence[Example], train: Callable[[List[Example]], ModelPackage]) -> CheckResult:
    """Leave-one-group-out: judge each group with a package trained on the
    others. ``train`` builds a package from a list of examples."""
    groups = sorted({e.group for e in examples})
    res = CheckResult(groups=len(groups), approved=sum(e.approved for e in examples),
                      rejected=sum(not e.approved for e in examples))
    if len(groups) < 2:
        res.skipped = "needs reviews on at least two sheets"
        return res
    if not res.rejected:
        res.skipped = "needs at least one rejected match"
        return res
    for g in groups:
        rest = [e for e in examples if e.group != g]
        if not any(e.approved for e in rest):
            continue  # nothing to learn from without this group
        package = train(rest)
        for e in (e for e in examples if e.group == g):
            ok = judge_crop(package, e.crop, rotation=e.rotation).accept
            if e.approved:
                res.approved_kept += ok
                res.approved_lost += not ok
            else:
                res.rejected_missed += ok
                res.rejected_caught += not ok
    return res


def gate(check: CheckResult) -> Dict[str, object]:
    """Whether a package may be switched on, and why (plain language)."""
    if check.skipped:
        return {"ok": False, "reason": f"Not checked yet: {check.skipped}."}
    judged = check.approved_kept + check.approved_lost
    if not judged:
        return {"ok": False, "reason": "Not checked yet: needs approved matches on at least two sheets."}
    lost_share = check.approved_lost / judged
    if lost_share > MAX_LOST_SHARE:
        return {"ok": False, "reason": f"On reviews it had not seen it would have thrown out "
                f"{check.approved_lost} of {judged} correct matches; keep reviewing."}
    if check.rejected_caught < MIN_CAUGHT or check.rejected_caught < check.approved_lost:
        return {"ok": False, "reason": "On reviews it had not seen it would not have caught any wrong "
                "matches yet; keep reviewing."}
    return {"ok": True, "reason": f"On reviews it had not seen it caught {check.rejected_caught} of "
            f"{check.rejected_caught + check.rejected_missed} wrong matches and kept "
            f"{check.approved_kept} of {judged} correct ones."}
