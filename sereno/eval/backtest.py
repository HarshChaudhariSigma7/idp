"""Weekly backtest: refit confidence weights, then certify auto-accept thresholds with a guarantee.

Label: a field is WRONG if a human corrected it to a different value, RIGHT if a human confirmed it
or it sat on a spot-check document (where the reviewer confirms every field). Unreviewed
auto-accepted fields are excluded: their truth is unknown.

Guarantee: with probability >= 1 - delta, the share of auto-accepted fields that are wrong is at
most alpha (separately for high-stakes and other fields). How:

* Split by document (hash of its id) into a fit half and a calibration half. Weights are fitted on
  the fit half only; scoring the same fields they were fitted on would understate the risk.
* Calibrate on spot-check documents only. Spot checks are a uniform random sample of all
  documents, so their fields estimate the risk at any threshold without selection bias (fields a
  reviewer saw only because they were flagged are fine for fitting, never for calibration).
* Learn then Test (Angelopoulos et al., 2021): walk thresholds from strict to lenient, test
  "risk > alpha" with an exact binomial tail at each, stop at the first one that can't be
  rejected. Fixed-sequence testing needs no multiplicity correction. The walk starts at the first
  threshold that accepts enough fields to reject at all; that is decided from scores alone, never
  from labels, so the sequence stays fixed with respect to the outcomes being tested.
* Fields on one document fail together (one bad scan), so they aren't independent: the sample size
  is deflated by the design effect 1 + (m - 1) * ICC (Kish, 1965) before the binomial test.
* Mondrian groups: high-stakes fields get their own, stricter alpha and their own threshold;
  delta is split between the two groups.
* Ties: only thresholds at distinct observed scores are tested, so tied fields enter together.

These address the failure modes in per-field selective risk control (arXiv 2608.14639): document
clustering, refit leakage and tie mass. Output: a ThresholdConfig row per doc type (inactive
unless --apply) with the certificate attached.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math

import numpy as np
from sqlalchemy import select

from sereno.confidence.scorer import DEFAULT_WEIGHTS
from sereno.db import init_db, session_scope
from sereno.models import Document, ExtractedField, ThresholdConfig

ALPHA = {"normal": 0.01, "high_stakes": 0.003}
DELTA = 0.05
MIN_FIT = 300
MIN_UNIQUE_SCORES = 10


# --- exact binomial tail for non-integer (effective) counts ------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def binom_cdf(k: float, n: float, p: float) -> float:
    """P(Binomial(n, p) <= k), continuous in n and k: I_{1-p}(n - k, k + 1)."""
    if k >= n:
        return 1.0
    return betainc(n - k, k + 1, 1 - p)


def upper_bound(k: float, n: float, delta: float) -> float:
    """One-sided Clopper-Pearson upper bound on the error rate at confidence 1 - delta."""
    if n <= 0:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if binom_cdf(k, n, mid) > delta else (lo, mid)
    return hi


# --- data ---------------------------------------------------------------------------------------

def _half(doc_id: str) -> str:
    return "fit" if hashlib.sha256(doc_id.encode()).digest()[0] < 128 else "cal"


def _same(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return a == b


def _labelled(s, doc_type: str) -> list[dict]:
    rows = s.execute(select(ExtractedField, Document).join(Document, Document.id == ExtractedField.document_id)
                     .where(Document.doc_type == doc_type, Document.reviewed_at.is_not(None))).all()
    out = []
    for f, d in rows:
        if not f.features:
            continue
        if f.status == "corrected":
            wrong = not _same(f.final_value, f.value)
        elif f.status == "confirmed" or (d.is_qa_sample and f.status == "auto"):
            wrong = False
        else:
            continue
        out.append({"doc": d.id, "x": f.features, "wrong": wrong, "qa": bool(d.is_qa_sample),
                    "high": bool(f.is_high_stakes), "forced": "forced" in (f.reason_codes or [])})
    return out


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 3000, lr: float = 0.1) -> np.ndarray:
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        w -= lr * (X.T @ (p - y) / len(y) + l2 * w / len(y))
    return w


# --- calibration --------------------------------------------------------------------------------

def design_effect(docs: list[str], wrong: list[bool]) -> tuple[float, float, float]:
    """(deff, icc, mean cluster size) from the one-way ANOVA estimator of the intra-document
    correlation of errors. ICC < 0 is clipped to 0 (deff 1)."""
    groups: dict[str, list[float]] = {}
    for d, w in zip(docs, wrong):
        groups.setdefault(d, []).append(float(w))
    k, n = len(groups), len(docs)
    if k < 2 or n <= k:
        return 1.0, 0.0, n / max(k, 1)
    mean = sum(wrong) / n
    ssb = sum(len(g) * (sum(g) / len(g) - mean) ** 2 for g in groups.values())
    ssw = sum(sum((v - sum(g) / len(g)) ** 2 for v in g) for g in groups.values())
    msb, msw = ssb / (k - 1), ssw / (n - k)
    m0 = (n - sum(len(g) ** 2 for g in groups.values()) / n) / (k - 1)
    icc = 0.0 if msb + (m0 - 1) * msw <= 0 else max(0.0, (msb - msw) / (msb + (m0 - 1) * msw))
    mbar = n / k
    return 1 + (mbar - 1) * icc, icc, mbar


def _p_value(scores: np.ndarray, wrong: np.ndarray, t: float, alpha: float, deff: float) -> tuple[float, float, float]:
    acc = scores >= t
    n, k = acc.sum() / deff, wrong[acc].sum() / deff
    return (binom_cdf(k, n, alpha) if n >= 1 else 1.0), n, k


def certify(scores: np.ndarray, wrong: np.ndarray, docs: list[str], alpha: float, delta: float,
            guide: tuple | None = None) -> dict:
    """Learn then Test over the distinct calibration scores. The walk starts where independent
    `guide` data (scores, wrong, docs) gives the smallest p-value, else at the first threshold
    accepting enough fields to reject at all, and moves to more lenient thresholds until one can't
    be rejected. Returns the most lenient certified threshold with its certificate (None if none)."""
    out: dict = {"alpha": alpha, "delta": round(delta, 4), "fields": int(len(scores)),
                 "documents": len(set(docs)), "threshold": None}
    if len(scores) == 0:
        out["note"] = "not enough spot-check evidence: no spot-check fields yet"
        return out
    deff, icc, mbar = design_effect(docs, list(wrong))
    out.update({"design_effect": round(deff, 3), "icc": round(icc, 4), "fields_per_document": round(mbar, 1)})
    uniq = np.unique(scores)[::-1]
    if len(uniq) < MIN_UNIQUE_SCORES:
        out["warning"] = f"only {len(uniq)} distinct scores: thresholds are coarse"
    if guide is not None and len(guide[0]):
        g_s, g_w, g_d = guide
        g_deff = design_effect(g_d, list(g_w))[0]
        start = min(range(len(uniq)), key=lambda i: _p_value(g_s, g_w, uniq[i], alpha, g_deff)[0])
    else:
        n_min = math.log(delta) / math.log1p(-alpha)  # fewest fields that could reject with no errors
        start = next((i for i, t in enumerate(uniq) if (scores >= t).sum() >= n_min), len(uniq))
    for t in uniq[start:]:
        p, n, k = _p_value(scores, wrong, t, alpha, deff)
        if p > delta:
            break
        acc = scores >= t
        out.update({"threshold": round(float(t), 4), "accepted": int(acc.sum()), "errors": int(wrong[acc].sum()),
                    "accept_rate": round(float(acc.mean()), 4), "p_value": float(f"{p:.3g}"),
                    "risk_upper_bound": round(upper_bound(k, n, delta), 5)})
    if out["threshold"] is None:
        out["note"] = "not enough spot-check evidence to certify any threshold at this alpha"
    return out


def backtest(doc_type: str, apply: bool = False) -> dict:
    init_db()
    with session_scope() as s:
        data = _labelled(s, doc_type)
        keys = list(DEFAULT_WEIGHTS)
        fit = [r for r in data if _half(r["doc"]) == "fit"]
        cal = [r for r in data if _half(r["doc"]) == "cal" and r["qa"] and not r["forced"]]
        report: dict = {"doc_type": doc_type, "labelled_fields": len(data), "fit_fields": len(fit),
                        "calibration_fields": len(cal)}
        if len(fit) < MIN_FIT:
            report["decision"] = f"not enough labelled fields to refit ({len(fit)} < {MIN_FIT}); keeping current thresholds"
            return report
        X = np.array([[r["x"].get(k, 0.0) for k in keys] for r in fit])
        w = fit_logistic(X, np.array([0.0 if r["wrong"] else 1.0 for r in fit]))
        weights = {k: round(float(v), 4) for k, v in zip(keys, w)}
        def arrays(rows):
            Xr = np.array([[r["x"].get(k, 0.0) for k in keys] for r in rows]).reshape(len(rows), len(keys))
            return 1 / (1 + np.exp(-Xr @ w)), np.array([r["wrong"] for r in rows], dtype=bool), [r["doc"] for r in rows]

        certs = {}
        for group, high in (("normal", False), ("high_stakes", True)):
            guide = [r for r in fit if r["qa"] and not r["forced"] and r["high"] == high]
            certs[group] = certify(*arrays([r for r in cal if r["high"] == high]), ALPHA[group], DELTA / 2,
                                   guide=arrays(guide) if guide else None)
        report.update({"weights": weights, "certificates": certs})
        t_n, t_h = certs["normal"]["threshold"], certs["high_stakes"]["threshold"]
        if t_n is None or t_h is None:
            report["decision"] = ("no certified threshold for " + " and ".join(g for g, c in certs.items() if c["threshold"] is None)
                                  + "; keep current thresholds and raise SERENO_QA_SAMPLE_RATE")
            return report
        t_h = max(t_h, t_n)
        tc = ThresholdConfig(doc_type=doc_type, auto_accept=t_n, high_stakes=t_h, weights=weights,
                             evidence={"certificates": certs, "labelled": len(data)}, active=apply)
        if apply:
            for old in s.execute(select(ThresholdConfig).where(ThresholdConfig.doc_type == doc_type,
                                                               ThresholdConfig.active.is_(True))).scalars():
                old.active = False
        s.add(tc)
        report["decision"] = f"threshold {t_n} (high-stakes {t_h}) " + ("APPLIED" if apply else "proposed; rerun with --apply")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-type", default="invoice")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(backtest(a.doc_type, a.apply), indent=1))


if __name__ == "__main__":
    main()
