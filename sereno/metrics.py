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
from sereno.models import Correction, Document, ExtractedField, ExtractionRun, Page, Template, ValidationResult, utcnow

SECONDS_PER_FIELD_MANUAL = 8  # conservative manual keying time per field, used for "time saved"


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
    reviewed = [d for d in docs if d.reviewed_at and d.review_entered_at and not d.is_qa_sample]
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
            by_type[d.doc_type]["needed_review"] += int(bool(d.review_entered_at) and not d.is_qa_sample)
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "today": {
            "processed": sum(1 for d in today_docs if d.processed_at),
            "received": len(today_docs),
            "needed_review": sum(1 for d in today_docs if d.review_entered_at and not d.is_qa_sample),
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
        "technology": technology(s, tenant_id, docs),
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
        "review": {"turnaround_p50_min": _q(turn, 0.5), "turnaround_p90_min": _q(turn, 0.9),
                   "on_time_pct": _pct(sum(1 for d in reviewed if d.review_due_at and d.reviewed_at <= d.review_due_at), len(reviewed)),
                   "seconds_per_field_p50": round(_q(sorted(per_field_ms), 0.5) / 1000, 1) if per_field_ms else None},
        "demo_readiness": demo_readiness(),
    }


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


# --- accuracy by document condition + the technology behind it ----------------------------------

MIN_FIELDS_FOR_CLAIM = 200  # below this a percentage is shown as "building baseline", not a claim


def _vernacular(d: Document) -> bool:
    return bool(d.languages) and any(l not in ("english", "other") for l in d.languages)


CONDITIONS = [
    ("overall", "Overall accuracy", lambda d: True,
     ["Two independent AI readings of every field", "Every figure re-checked by GST & arithmetic rules",
      "Confidence from evidence, not model self-report"]),
    ("printed", "Printed & digital", lambda d: not _vernacular(d) and not d.has_handwriting,
     ["PDF text-layer cross-check on digital files", "Deskew, upscale and contrast repair on scans",
      "GSTIN checksum, HSN, e-way and IRN validation"]),
    ("vernacular", "Vernacular", _vernacular,
     ["Devanagari & regional script reading, strongest model only", "Indic numerals (०-९) normalised in tested code",
      "Bilingual label dictionary for LRs, bilty & challans"]),
    ("handwritten", "Handwritten", lambda d: bool(d.has_handwriting),
     ["Zoomed re-read on original pixels", "Verifier resolves every disagreement, then a person confirms",
      "Unreadable pages refused before any guess"]),
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
    for key, label, pred, tech in CONDITIONS:
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
            "benchmark": _benchmark(key), "technology": tech,
        })
    return out


def technology(s, tenant_id: str, docs: list[Document]) -> list[dict]:
    """Live counters for each stage of the pipeline: what the machinery actually did."""
    ids = [d.id for d in docs]
    if not ids:
        return []
    pages = s.execute(select(Page.corrections_applied).where(Page.document_id.in_(ids))).scalars().all()
    corr = Counter(c.split(":")[0] for cs in pages for c in (cs or []) if not c.endswith(":reverted"))
    feats = s.execute(select(ExtractedField.features, ExtractedField.needs_review, ExtractedField.status)
                      .where(ExtractedField.document_id.in_(ids))).all()
    double = sum(1 for f, _, _ in feats if f and (f.get("agree") or f.get("disagree")))
    disagree = sum(1 for f, _, _ in feats if f and f.get("disagree"))
    human = sum(1 for _, _, st in feats if st in ("confirmed", "corrected"))
    checks = s.execute(select(ValidationResult.status).where(ValidationResult.document_id.in_(ids),
                                                             ValidationResult.stage == "extracted")).scalars().all()
    verify = s.execute(select(func.count()).select_from(ExtractionRun).where(ExtractionRun.document_id.in_(ids),
                                                                             ExtractionRun.pass_name == "verify")).scalar() or 0
    models = Counter((d.model_used or "").replace("claude-", "") for d in docs if d.model_used)
    ms = sorted(c.time_on_field_ms for c in s.execute(select(Correction).where(Correction.document_id.in_(ids))).scalars()
                if c.time_on_field_ms)
    templates = s.execute(select(func.count()).select_from(Template).where(Template.tenant_id == tenant_id,
                                                                           Template.status == "active")).scalar() or 0
    return [
        {"step": "Quality pre-check", "value": len(pages), "unit": "pages checked",
         "detail": f"{sum(1 for d in docs if d.status == 'unreadable')} refused as unreadable instead of guessed"},
        {"step": "Auto-correction", "value": sum(corr.values()), "unit": "fixes applied",
         "detail": ", ".join(f"{v} {k}" for k, v in corr.most_common(4)) or "none needed"},
        {"step": "Dual independent reading", "value": double, "unit": "fields read twice",
         "detail": " · ".join(f"{v} {k}" for k, v in models.most_common()) or ""},
        {"step": "Disagreement verifier", "value": disagree, "unit": "disagreements caught",
         "detail": f"resolved on zoomed crops in {verify} document(s), then sent to a person"},
        {"step": "GST & arithmetic checks", "value": len(checks), "unit": "checks run",
         "detail": f"{sum(1 for c in checks if c == 'fail')} mismatches stopped before export"},
        {"step": "Human confirmation", "value": human, "unit": "fields confirmed by people",
         "detail": (f"median {round(ms[len(ms) // 2] / 1000, 1)}s per field" if ms else "") +
                   (f" · {templates} vendor template(s) active" if templates else "")},
    ]
