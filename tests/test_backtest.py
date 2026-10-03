"""The threshold certificate must hold: exact binomial tails, cluster-aware sample size, and a
violation rate no higher than delta on simulated data with known risk."""
import math
import random

import numpy as np

from sereno.eval.backtest import binom_cdf, certify, design_effect, upper_bound


def test_binomial_tail_matches_closed_forms():
    assert math.isclose(binom_cdf(0, 10, 0.1), 0.9 ** 10, rel_tol=1e-9)
    exact = sum(math.comb(20, i) * 0.05 ** i * 0.95 ** (20 - i) for i in range(3))
    assert math.isclose(binom_cdf(2, 20, 0.05), exact, rel_tol=1e-9)
    assert math.isclose(upper_bound(0, 300, 0.05), 1 - 0.05 ** (1 / 300), rel_tol=1e-4)


def test_design_effect_grows_when_errors_cluster_by_document():
    docs = [f"d{i // 10}" for i in range(400)]
    clustered = [i // 10 % 8 == 0 for i in range(400)]        # whole documents wrong together
    scattered = [i % 80 == 0 for i in range(400)]             # same rate, spread across documents
    assert design_effect(docs, clustered)[0] > 5
    assert design_effect(docs, scattered)[0] < 1.5


def _sim(rng, n_docs=400, per_doc=12):
    scores, wrong, docs = [], [], []
    for d in range(n_docs):
        bad_doc = rng.random() < 0.05
        for _ in range(per_doc):
            s = round(rng.random(), 3)
            p = (0.3 if bad_doc else 0.02) * (1 - s) ** 2
            scores.append(s)
            wrong.append(rng.random() < p)
            docs.append(f"d{d}")
    return np.array(scores), np.array(wrong), docs


def _true_risk(t):
    """Exact risk of the simulation: scores are round(U, 3), errors 0.034 * (1 - s)^2 on average."""
    grid = np.arange(1001) / 1000
    w = np.where((grid == 0) | (grid == 1), 0.5, 1.0)
    acc = grid >= t
    return float(0.034 * (w[acc] * (1 - grid[acc]) ** 2).sum() / w[acc].sum())


def test_certified_threshold_holds_with_probability_one_minus_delta():
    rng = random.Random(7)
    alpha, delta, trials, violations, certified = 0.005, 0.1, 40, 0, 0
    for _ in range(trials):
        sc, w, docs = _sim(rng)
        c = certify(sc, w, docs, alpha, delta, guide=_sim(rng))
        if c["threshold"] is None:
            continue
        certified += 1
        violations += _true_risk(c["threshold"]) > alpha
    assert certified >= trials // 2
    assert violations / trials <= delta


def test_no_threshold_without_enough_evidence():
    c = certify(np.array([0.99] * 50), np.array([False] * 50), ["d"] * 50, 0.003, 0.025)
    assert c["threshold"] is None and "not enough" in c["note"]
