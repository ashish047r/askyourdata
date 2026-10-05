"""Scoring helpers for the eval runner. Pure Python."""

from collections import Counter
from itertools import permutations


def _norm(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v).strip().lower()


def compare_results(gold_rows: list, pred_rows: list, ordered: bool = False) -> bool:
    """Execution accuracy: same rows (as a multiset unless ordered) after rounding numbers to 2 dp.
    The prediction may contain extra columns and any column order, as long as some choice of its
    columns reproduces every gold column."""
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    gold = [tuple(_norm(v) for v in r) for r in gold_rows]
    pred = [[_norm(v) for v in r] for r in pred_rows]
    n_gold, n_pred = len(gold[0]), len(pred[0])
    if n_pred < n_gold:
        return False
    target = gold if ordered else Counter(gold)
    # ponytail: brute-force column matching; fine for <= ~8 columns, cap it if results get wider.
    for cols in permutations(range(n_pred), n_gold):
        proj = [tuple(r[i] for i in cols) for r in pred]
        if (proj if ordered else Counter(proj)) == target:
            return True
    return False


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]
