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
distance second. Eligible pairs are found with a uniform grid, so building
the graph is close to linear in the number of points.

Tolerance may be one radius for all predictions, or a per-prediction radius
(used by the relative tolerance, see tolerance.py). A pair is eligible when
its distance is <= the *prediction's* radius.

Determinism: inputs are processed in sorted-ID order. When two matchings
have exactly equal cardinality and total distance, the result is the one
the Hungarian algorithm finds on that ordering; it is stable run to run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union


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


def _cell(v: float, size: float) -> int:
    return int(math.floor(v / size))


def eligible_pairs(
    predictions: Sequence[Point],
    references: Sequence[Point],
    tolerance: Union[float, Mapping[str, float]],
) -> Dict[Tuple[str, str], float]:
    """All (prediction_id, reference_id) pairs with distance <= tolerance.

    ``tolerance`` is either one radius for every prediction or a mapping from
    prediction ID to that prediction's own radius. A uniform grid (cell size
    = the largest radius) keeps this close to linear on a real page instead of
    comparing every prediction with every reference.
    """
    if isinstance(tolerance, Mapping):
        tol_of = dict(tolerance)
        for p in predictions:
            if p.id not in tol_of:
                raise ValueError(f"no tolerance for prediction '{p.id}'")
    else:
        tol_of = {p.id: tolerance for p in predictions}
    for t in tol_of.values():
        if not (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t >= 0):
            raise ValueError("tolerance must be a finite number >= 0")
    edges: Dict[Tuple[str, str], float] = {}
    if not predictions or not references:
        return edges
    size = max(tol_of.values()) or 1.0
    grid: Dict[Tuple[int, int], List[Point]] = {}
    for r in references:
        grid.setdefault((_cell(r.x, size), _cell(r.y, size)), []).append(r)
    for p in predictions:
        tol = tol_of[p.id]
        reach = int(math.ceil(tol / size))
        cx, cy = _cell(p.x, size), _cell(p.y, size)
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                for r in grid.get((gx, gy), ()):
                    d = distance(p, r)
                    if d <= tol:
                        edges[(p.id, r.id)] = d
    return edges


def match(
    predictions: Sequence[Point],
    references: Sequence[Point],
    tolerance: Union[float, Mapping[str, float]],
    edges_by_id: Optional[Mapping[Tuple[str, str], float]] = None,
) -> List[Pair]:
    """Return the matched pairs, sorted by reference ID then prediction ID.

    ``tolerance`` is one radius, or a mapping prediction ID -> radius (used for
    relative tolerances). ``edges_by_id`` may pass in a precomputed
    :func:`eligible_pairs` result for the same inputs.
    """
    if edges_by_id is None:
        edges_by_id = eligible_pairs(predictions, references, tolerance)

    preds = sorted(predictions, key=lambda p: p.id)
    refs = sorted(references, key=lambda r: r.id)
    pidx = {p.id: i for i, p in enumerate(preds)}
    ridx = {r.id: i for i, r in enumerate(refs)}
    edges: Dict[Tuple[int, int], float] = {
        (pidx[pid], ridx[rid]): d for (pid, rid), d in edges_by_id.items()
    }

    pairs: List[Pair] = []
    for comp_preds, comp_refs in _components(preds, refs, edges):
        n = max(len(comp_preds), len(comp_refs))
        comp_edges = [
            (a, b, edges[(pi, ri)])
            for a, pi in enumerate(comp_preds)
            for b, ri in enumerate(comp_refs)
            if (pi, ri) in edges
        ]
        max_d = max(d for _, _, d in comp_edges)
        big = (n + 1) * (max_d + 1.0) + 1.0
        cost = [[big] * n for _ in range(n)]
        for a, b, d in comp_edges:
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
