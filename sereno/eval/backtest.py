"""Weekly backtest: refit confidence weights and pick thresholds from logged human outcomes.

Label: a field is WRONG if a human corrected it to a different value, RIGHT if a human confirmed
it or it survived a full spot-check (QA sample). Unreviewed auto-accepted fields are excluded:
we don't know their truth.

Selection bias warning: flagged fields are over-represented among reviewed fields. The escape
rate in the auto-accept region is therefore estimated from QA spot-check documents only, which
are an unbiased random sample of auto-approved documents. Increase SERENO_QA_SAMPLE_RATE until
that sample is large enough (the report says when it is not).

Output: a ThresholdConfig row per doc type (inactive unless --apply) with the evidence attached.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
from sqlalchemy import select

from sereno.confidence.scorer import DEFAULT_WEIGHTS
from sereno.db import init_db, session_scope
from sereno.models import Document, ExtractedField, ThresholdConfig

TARGET_ESCAPE = 0.005
MIN_LABELLED = 300
MIN_QA_FIELDS = 200


def _labelled(s, doc_type: str):
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
        out.append((f.features, wrong, d.is_qa_sample and not f.needs_review, f.is_high_stakes))
    return out


def _same(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return a == b


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 3000, lr: float = 0.1) -> np.ndarray:
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        grad = X.T @ (p - y) / len(y) + l2 * w / len(y)
        w -= lr * grad
    return w


def backtest(doc_type: str, apply: bool = False) -> dict:
    init_db()
    with session_scope() as s:
        data = _labelled(s, doc_type)
        keys = list(DEFAULT_WEIGHTS)
        report: dict = {"doc_type": doc_type, "labelled_fields": len(data)}
        if len(data) < MIN_LABELLED:
            report["decision"] = f"not enough labelled fields ({len(data)} < {MIN_LABELLED}); keeping current thresholds"
            return report
        X = np.array([[feat.get(k, 0.0) for k in keys] for feat, *_ in data])
        right = np.array([0.0 if wrong else 1.0 for _, wrong, *_ in data])
        w = fit_logistic(X, right)
        weights = {k: round(float(v), 4) for k, v in zip(keys, w)}
        scores = 1 / (1 + np.exp(-X @ w))
        qa_mask = np.array([qa for _, _, qa, _ in data])
        hs_mask = np.array([hs for *_, hs in data])
        grid = [round(x, 3) for x in np.arange(0.50, 0.995, 0.005)]
        curve = []
        for t in grid:
            acc = scores >= t
            qa_auto = acc & qa_mask
            esc = float((right[qa_auto] == 0).mean()) if qa_auto.sum() else math.nan
            curve.append({"threshold": t, "auto_accept_rate": round(float(acc.mean()), 4),
                          "qa_fields_above": int(qa_auto.sum()), "escape_rate": None if math.isnan(esc) else round(esc, 4)})
        ok = [c for c in curve if c["escape_rate"] is not None and c["qa_fields_above"] >= MIN_QA_FIELDS
              and c["escape_rate"] <= TARGET_ESCAPE]
        report.update({"weights": weights, "curve": curve[::10]})
        if not ok:
            report["decision"] = ("no threshold meets the escape target with enough spot-check evidence; "
                                  "keep current thresholds and raise the QA sample rate")
            return report
        chosen = min(ok, key=lambda c: c["threshold"])
        hs_t = max(chosen["threshold"], min(0.99, chosen["threshold"] + 0.03))
        tc = ThresholdConfig(doc_type=doc_type, auto_accept=chosen["threshold"], high_stakes=hs_t, weights=weights,
                             evidence={"chosen": chosen, "labelled": len(data), "high_stakes_fields": int(hs_mask.sum())},
                             active=apply)
        if apply:
            for old in s.execute(select(ThresholdConfig).where(ThresholdConfig.doc_type == doc_type,
                                                               ThresholdConfig.active.is_(True))).scalars():
                old.active = False
        s.add(tc)
        report["decision"] = f"threshold {chosen['threshold']} (high-stakes {hs_t}) " + ("APPLIED" if apply else "proposed; rerun with --apply")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-type", default="invoice")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(backtest(a.doc_type, a.apply), indent=1))


if __name__ == "__main__":
    main()
