"""One-to-one matching of predicted points to reference points.

Objective (lexicographic):
  1. Maximise the number of matched pairs (maximum-cardinality matching)
     where a pair is eligible only if its Euclidean distance <= tolerance.
  2. Among all maximum-cardinality matchings, minimise total distance.

Each prediction and each reference point is used at most once, so a
duplicate prediction can never earn credit for a receptacle that is
already matched.

Implementation: the eligibility graph is split into connected components
(most components on a drawing are tiny), and each component is solved
exactly with the Hungarian algorithm. Ineligible pairs (and padding) cost
BIG, where BIG exceeds any possible sum of eligible distances in the
component, so minimising total cost maximises cardinality first and total
distance second.

Determinism: inputs are processed in sorted-ID order. When two matchings
have exactly equal cardinality and total distance, the result is the one
the Hungarian algorithm finds on that ordering; it is stable run to run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class Point:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class Pair:
    prediction_id: str
    reference_id: str
    distance: float


def distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _hungarian(cost: List[List[float]]) -> List[int]:
    """Min-cost perfect assignment on a square matrix.

    Returns assignment[row] = column. O(n^3) potentials-based version.
    """
    n = len(cost)
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[col] = row assigned to col (1-based), 0 = none
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            assignment[p[j] - 1] = j - 1
    return assignment


def _components(
    preds: Sequence[Point], refs: Sequence[Point], edges: Dict[Tuple[int, int], float]
) -> List[Tuple[List[int], List[int]]]:
    """Connected components of the bipartite eligibility graph (edges only)."""
    parent = list(range(len(preds) + len(refs)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for (pi, ri) in edges:
        a, b = find(pi), find(len(preds) + ri)
        if a != b:
            parent[max(a, b)] = min(a, b)

    groups: Dict[int, Tuple[List[int], List[int]]] = {}
    for (pi, ri) in edges:
        root = find(pi)
        groups.setdefault(root, ([], []))
    for pi in range(len(preds)):
        root = find(pi)
        if root in groups:
            groups[root][0].append(pi)
    for ri in range(len(refs)):
        root = find(len(preds) + ri)
        if root in groups:
            groups[root][1].append(ri)
    return [groups[k] for k in sorted(groups)]


def match(
    predictions: Sequence[Point], references: Sequence[Point], tolerance: float
) -> List[Pair]:
    """Return the matched pairs, sorted by reference ID then prediction ID."""
    if not (isinstance(tolerance, (int, float)) and math.isfinite(tolerance) and tolerance >= 0):
        raise ValueError("tolerance must be a finite number >= 0")

    preds = sorted(predictions, key=lambda p: p.id)
    refs = sorted(references, key=lambda r: r.id)

    edges: Dict[Tuple[int, int], float] = {}
    for pi, p in enumerate(preds):
        for ri, r in enumerate(refs):
            d = distance(p, r)
            if d <= tolerance:
                edges[(pi, ri)] = d

    pairs: List[Pair] = []
    for comp_preds, comp_refs in _components(preds, refs, edges):
        n = max(len(comp_preds), len(comp_refs))
        big = (n + 1) * (tolerance + 1.0) + 1.0
        cost = [[big] * n for _ in range(n)]
        for a, pi in enumerate(comp_preds):
            for b, ri in enumerate(comp_refs):
                d = edges.get((pi, ri))
                if d is not None:
                    cost[a][b] = d
        assignment = _hungarian(cost)
        for a, b in enumerate(assignment):
            if a < len(comp_preds) and b < len(comp_refs):
                pi, ri = comp_preds[a], comp_refs[b]
                d = edges.get((pi, ri))
                if d is not None:
                    pairs.append(Pair(preds[pi].id, refs[ri].id, d))

    pairs.sort(key=lambda pr: (pr.reference_id, pr.prediction_id))
    return pairs
