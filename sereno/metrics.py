"""Customer dashboard and internal metrics. Definitions are explicit because these numbers become
sales collateral: nothing here is allowed to flatter the product.

- first_pass_accuracy: share of extracted fields that no human had to change (completed docs).
- spot_check_accuracy: share of AUTO-ACCEPTED fields found correct when a random sample of
  auto-approved documents is fully re-checked. This is the honest safety number (escape rate =
  1 - this); first-pass accuracy alone would hide silent errors.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from sereno.config import get_settings
from sereno.models import Correction, Document, ExtractedField, ExtractionRun, utcnow

SECONDS_PER_FIELD_MANUAL = 8  # conservative manual keying time per field, used for "time saved"



def _needed_review(d: Document) -> bool:
    """Sent to a person because something was flagged (not merely picked for a spot check)."""
    return bool(d.review_entered_at) and (not d.is_qa_sample or bool(d.fields_flagged))

def _pct(n: float, d: float) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def dashboard(s, tenant_id: str, days: int = 30) -> dict:
    now = utcnow()
    since = now - timedelta(days=days)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    docs = s.execute(select(Document).where(Document.tenant_id == tenant_id, Document.created_at >= since)).scalars().all()
    ids = [d.id for d in docs]
    corrected = defaultdict(int)
    if ids:
        for fid, did in s.execute(select(ExtractedField.id, ExtractedField.document_id)
                                  .where(ExtractedField.document_id.in_(ids), ExtractedField.status == "corrected")):
            corrected[did] += 1

    done = [d for d in docs if d.status in ("ready", "exported") and d.processed_at]
    today_docs = [d for d in docs if d.created_at >= today0]
    open_q = s.execute(select(Document).where(Document.tenant_id == tenant_id, Document.reviewed_at.is_(None),
                                              Document.status.in_(["needs_review", "unreadable"]))).scalars().all()
    reviewed = [d for d in docs if d.reviewed_at and _needed_review(d)]
    turn = [(d.reviewed_at - d.review_entered_at).total_seconds() / 60 for d in reviewed]
    on_time = [d for d in reviewed if d.review_due_at and d.reviewed_at <= d.review_due_at]

    trend = defaultdict(lambda: {"fields": 0, "corrected": 0, "docs": 0, "auto": 0})
    for d in done:
        t = trend[_day(d.processed_at)]
        t["fields"] += d.fields_total
        t["corrected"] += corrected[d.id]
        t["docs"] += 1
        t["auto"] += int(d.fields_flagged == 0)
    trend_rows = [{"day": k, "accuracy": _pct(v["fields"] - v["corrected"], v["fields"]),
                   "straight_through": _pct(v["auto"], v["docs"]), "documents": v["docs"]}
                  for k, v in sorted(trend.items())]

    fields_total = sum(d.fields_total for d in done)
    fields_corr = sum(corrected[d.id] for d in done)
    auto_fields = sum(d.fields_total - d.fields_flagged for d in done)
    spot = spot_check_accuracy(s, [d for d in docs if d.is_qa_sample and d.reviewed_at])
    by_type = defaultdict(lambda: {"documents": 0, "needed_review": 0})
    for d in docs:
        if d.doc_type:
            by_type[d.doc_type]["documents"] += 1
            by_type[d.doc_type]["needed_review"] += int(_needed_review(d))
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "today": {
            "processed": sum(1 for d in today_docs if d.processed_at),
            "received": len(today_docs),
            "needed_review": sum(1 for d in today_docs if _needed_review(d)),
            "ready": sum(1 for d in today_docs if d.status in ("ready", "exported")),
        },
        "queue": {
            "open": len(open_q),
            "overdue": sum(1 for d in open_q if d.review_due_at and d.review_due_at < now),
            "oldest_due_at": min((d.review_due_at for d in open_q if d.review_due_at), default=None),
        },
        "turnaround": {
            "sla_minutes": get_settings().review_sla_minutes,
            "average_minutes": round(statistics.mean(turn), 1) if turn else None,
            "median_minutes": round(statistics.median(turn), 1) if turn else None,
            "on_time_pct": _pct(len(on_time), len(reviewed)),
            "reviewed": len(reviewed),
        },
        "accuracy": {
            "first_pass_accuracy_pct": _pct(fields_total - fields_corr, fields_total),
            "straight_through_pct": _pct(sum(1 for d in done if d.fields_flagged == 0), len(done)),
            "spot_check": spot,
            "fields_extracted": fields_total,
        },
        "time_saved_hours": round(auto_fields * SECONDS_PER_FIELD_MANUAL / 3600, 1),
        "conditions": conditions(s, docs),
        "trend": trend_rows,
        "by_type": dict(by_type),
        "totals": {"documents": len(docs), "completed": len(done)},
    }


def spot_check_accuracy(s, qa_docs: list[Document]) -> dict:
    if not qa_docs:
        return {"accuracy_pct": None, "documents": 0, "fields": 0}
    ids = [d.id for d in qa_docs]
    fields = s.execute(select(ExtractedField).where(ExtractedField.document_id.in_(ids),
                                                    ExtractedField.needs_review.is_(False))).scalars().all()
    wrong = sum(1 for f in fields if f.status == "corrected")
    return {"accuracy_pct": _pct(len(fields) - wrong, len(fields)), "documents": len(qa_docs), "fields": len(fields),
            "escapes": wrong}


def internal_metrics(s, days: int = 30) -> dict:
    """Cross-tenant, aggregate only. No document content, no tenant names."""
    since = utcnow() - timedelta(days=days)
    docs = s.execute(select(Document).where(Document.created_at >= since, Document.processed_at.is_not(None))).scalars().all()
    ids = [d.id for d in docs]
    fields = s.execute(select(ExtractedField).where(ExtractedField.document_id.in_(ids))).scalars().all() if ids else []
    docs_by_id = {d.id: d for d in docs}

    def slice_by(keyfn):
        acc = defaultdict(lambda: {"docs": set(), "fields": 0, "looked": 0, "corrected": 0, "flagged": 0,
                                   "auto_qa": 0, "auto_qa_wrong": 0})
        for f in fields:
            d = docs_by_id[f.document_id]
            if d.status not in ("ready", "exported"):
                continue
            for k in keyfn(d):
                a = acc[k]
                a["docs"].add(d.id)
                a["fields"] += 1
                a["flagged"] += int(f.needs_review)
                a["corrected"] += int(f.status == "corrected")
                a["looked"] += int(f.status in ("confirmed", "corrected") or d.is_qa_sample)
                if d.is_qa_sample and d.reviewed_at and not f.needs_review:
                    a["auto_qa"] += 1
                    a["auto_qa_wrong"] += int(f.status == "corrected")
        return {k: {"documents": len(v["docs"]), "fields": v["fields"],
                    "first_pass_accuracy_pct": _pct(v["fields"] - v["corrected"], v["fields"]),
                    "field_review_rate_pct": _pct(v["flagged"], v["fields"]),
                    "spot_check_fields": v["auto_qa"],
                    "escape_rate_pct": _pct(v["auto_qa_wrong"], v["auto_qa"])}
                for k, v in sorted(acc.items())}

    lang_key = lambda d: ["hindi/bilingual" if d.languages and any(l != "english" for l in d.languages) else "english_only"]  # noqa: E731
    calib = defaultdict(lambda: [0, 0])
    for f in fields:
        if f.status in ("confirmed", "corrected"):
            b = min(9, int(f.score * 10))
            calib[b][0] += 1
            calib[b][1] += int(f.status == "corrected")
    runs = s.execute(select(ExtractionRun).where(ExtractionRun.created_at >= since)).scalars().all()
    lat = sorted(d.processing_ms for d in docs if d.processing_ms)
    corrections = s.execute(select(Correction).where(Correction.created_at >= since)).scalars().all()
    per_field_ms = [c.time_on_field_ms for c in corrections if c.time_on_field_ms]
    reviewed = [d for d in docs if d.reviewed_at and d.review_entered_at]
    turn = sorted((d.reviewed_at - d.review_entered_at).total_seconds() / 60 for d in reviewed)
    return {
        "window_days": days,
        "documents": len(docs),
        "by_doc_type": slice_by(lambda d: [d.doc_type or "unknown"]),
        "by_quality_bucket": slice_by(lambda d: [d.quality_bucket or "unknown"]),
        "by_language": slice_by(lang_key),
        "by_handwriting": slice_by(lambda d: ["handwriting" if d.has_handwriting else "printed"]),
        "calibration": [{"score_bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "reviewed": n,
                         "error_rate_pct": _pct(w, n)} for b, (n, w) in sorted(calib.items())],
        "latency_ms": {"p50": _q(lat, 0.5), "p95": _q(lat, 0.95)},
        "cost_usd_per_doc": round(statistics.mean([d.cost_usd for d in docs if d.cost_usd]), 4) if any(d.cost_usd for d in docs) else None,
        "model_calls": len(runs),
        "model_errors": sum(1 for r in runs if r.error),
        "efficiency": efficiency(docs, runs, fields),
        "review": {"turnaround_p50_min": _q(turn, 0.5), "turnaround_p90_min": _q(turn, 0.9),
                   "on_time_pct": _pct(sum(1 for d in reviewed if d.review_due_at and d.reviewed_at <= d.review_due_at), len(reviewed)),
                   "seconds_per_field_p50": round(_q(sorted(per_field_ms), 0.5) / 1000, 1) if per_field_ms else None},
        "demo_readiness": demo_readiness(),
    }


def efficiency(docs: list[Document], runs: list, fields: list) -> dict:
    """The call budget in practice: calls per document, how many needed only the page reading,
    and how many values were proven without a model (docs/ARCHITECTURE.md, "Call budget")."""
    calls = Counter(r.document_id for r in runs if not r.error)
    n = [calls.get(d.id, 0) for d in docs]
    proven = sum(1 for f in fields if (f.features or {}).get("proven"))
    reread = sum(1 for f in fields if (f.features or {}).get("agree") or (f.features or {}).get("disagree"))
    return {"calls_per_doc": round(statistics.mean(n), 2) if n else None,
            "one_call_pct": _pct(sum(1 for x in n if x == 1), len(n)),
            "fields_proven_pct": _pct(proven, len(fields)),
            "fields_reread_pct": _pct(reread, len(fields))}


def _q(xs: list, q: float):
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def demo_readiness() -> dict:
    """Which (doc type x bucket) cells have passed the offline eval gate. Never demo the others."""
    p = Path("reports/eval_latest.json")
    if not p.exists():
        return {"status": "no_eval_run", "message": "No labelled evaluation has been run yet. Do not demo on customer documents.",
                "cells": []}
    rep = json.loads(p.read_text())
    return {"status": "ok", "run_at": rep.get("run_at"), "cells": rep.get("gate", [])}


# --- accuracy by document condition ---------------------------------------------------------------

MIN_FIELDS_FOR_CLAIM = 200  # below this a percentage is shown as "building baseline", not a claim


def _vernacular(d: Document) -> bool:
    return bool(d.languages) and any(l not in ("english", "other") for l in d.languages)


CONDITIONS = [
    ("overall", "All documents", lambda d: True),
    ("printed", "Printed", lambda d: not _vernacular(d) and not d.has_handwriting),
    ("vernacular", "Hindi & regional", _vernacular),
    ("handwritten", "Handwritten", lambda d: bool(d.has_handwriting)),
]

EVAL_BUCKETS = {"printed": ("digital", "good_scan", "printed", "poor_scan"), "vernacular": ("bilingual", "vernacular"),
                "handwritten": ("handwritten",), "overall": None}


def _benchmark(cond: str) -> dict | None:
    p = Path("reports/eval_latest.json")
    if not p.exists():
        return None
    rep = json.loads(p.read_text())
    docs = [d for d in rep.get("documents", []) if not d.get("synthetic") and
            (EVAL_BUCKETS[cond] is None or d.get("bucket") in EVAL_BUCKETS[cond])]
    f = sum(d["fields"] for d in docs)
    auto = sum(d["auto"] for d in docs)
    if not f:
        return None
    return {"documents": len(docs), "field_accuracy_pct": _pct(sum(d["correct"] for d in docs), f),
            "escape_rate_pct": _pct(sum(d["auto_wrong"] for d in docs), auto), "run_at": rep.get("run_at")}


def conditions(s, docs: list[Document]) -> list[dict]:
    done = [d for d in docs if d.status in ("ready", "exported") and d.processed_at]
    ids = [d.id for d in done]
    fields = s.execute(select(ExtractedField.document_id, ExtractedField.status, ExtractedField.needs_review)
                       .where(ExtractedField.document_id.in_(ids))).all() if ids else []
    per_doc = defaultdict(lambda: {"fields": 0, "corrected": 0, "caught": 0})
    for did, status, flagged in fields:
        x = per_doc[did]
        x["fields"] += 1
        if status == "corrected":
            x["corrected"] += 1
            x["caught"] += int(bool(flagged))
    out = []
    for key, label, pred in CONDITIONS:
        sel = [d for d in done if pred(d)]
        f = sum(per_doc[d.id]["fields"] for d in sel)
        c = sum(per_doc[d.id]["corrected"] for d in sel)
        caught = sum(per_doc[d.id]["caught"] for d in sel)
        daily = defaultdict(lambda: [0, 0])
        for d in sel:
            daily[_day(d.processed_at)][0] += per_doc[d.id]["fields"]
            daily[_day(d.processed_at)][1] += per_doc[d.id]["corrected"]
        out.append({
            "key": key, "label": label, "documents": len(sel), "fields": f,
            "accuracy_pct": _pct(f - c, f), "measured": f >= MIN_FIELDS_FOR_CLAIM,
            "caught_before_export_pct": _pct(caught, c) if c else None,
            "no_review_docs_pct": _pct(sum(1 for d in sel if d.fields_flagged == 0), len(sel)),
            "spot_check": spot_check_accuracy(s, [d for d in sel if d.is_qa_sample and d.reviewed_at]),
            "trend": [{"day": k, "accuracy": _pct(v[0] - v[1], v[0])} for k, v in sorted(daily.items())],
            "benchmark": _benchmark(key),
        })
    return out


