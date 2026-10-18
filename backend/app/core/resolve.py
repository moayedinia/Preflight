"""
Pure, dependency-free resolution/selection logic for the code knowledge graph.

Keeping these as plain functions (no Neo4j / numpy imports) makes them trivial to
unit-test in isolation and lets the I/O-heavy callers (analytics.py, graph.py) stay
thin. Each function is a small, precise policy decision that used to be buried in
Cypher or inlined, and that had a correctness bug (see the D4/D5 notes in the repo
roadmap).
"""
from typing import Iterable, List, Dict, Any, Tuple


def dedupe_rank_clone_pairs(
    rows: Iterable[Tuple[str, float, int, str]],
    threshold: float = 0.95,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    D5 fix — turn raw (name1, name2, sim, pair_key) clone candidates into the final
    ranked, de-duplicated clone-pair list returned to the API/UI.

    Input rows are (name1, name2, sim, pair_key) where `pair_key` is the
    canonical, order-insensitive identifier of the (nodeA, nodeB) pair (used to
    drop duplicates across different file paths). Output is de-duplicated by
    pair_key (keeping the highest similarity), filtered to `sim >= threshold`,
    sorted by similarity descending, and truncated to `limit`.

    Pure function: no I/O, deterministic for a given input order.
    """
    best: Dict[str, Tuple[str, str, float]] = {}
    for name1, name2, sim, key in rows:
        try:
            sim_f = float(sim)
        except (TypeError, ValueError):
            continue
        if sim_f < threshold:
            continue
        prev = best.get(key)
        if prev is None or sim_f > prev[2]:
            best[key] = (name1, name2, sim_f)

    ranked = sorted(best.values(), key=lambda x: x[2], reverse=True)[:limit]
    return [
        {"fn1": n1, "fn2": n2, "similarity": sim}
        for (n1, n2, sim) in ranked
    ]
