"""Offline evaluation against labelled real documents, reported per quality bucket.

Dataset layout (see docs/EVALUATION.md):
    <dataset>/<bucket>/<name>.<pdf|jpg|png|tif>
    <dataset>/<bucket>/<name>.truth.json   {"doc_type": "invoice", "bucket": "poor_scan",
                                             "fields": {...}, "line_items": [...], "synthetic": false}
Buckets: digital, good_scan, poor_scan, handwritten, bilingual (+ any extra, e.g. bilingual_lr).

The number that matters most is the ESCAPE RATE: the share of fields the system auto-accepted
(sent to export without a human) that were wrong. Field accuracy alone hides that.

Runs the real pipeline (real Claude calls unless --fake) in an isolated temp database.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

GATE = {"min_docs": 20, "max_escape_rate_pct": 0.5, "min_field_accuracy_pct": 97.0}
EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def _items(dataset: Path):
    for truth in sorted(dataset.rglob("*.truth.json")):
        stem = truth.name[: -len(".truth.json")]
        src = next((p for p in truth.parent.glob(stem + ".*") if p.suffix.lower() in EXTS), None)
        if src:
            yield src, json.loads(truth.read_text())


def _compare(doc_fields, truth: dict):
    """Yields (key, ftype, correct, flagged) for every field that is in truth or was extracted."""
    from sereno.extraction.normalize import normalise, values_agree
    by_key = {f.key: f for f in doc_fields}
    tf = truth.get("fields", {})
    for f in doc_fields:
        if f.line_index is not None:
            continue
        expected = normalise(f.value_type, None if tf.get(f.field_name) is None else str(tf.get(f.field_name)))
        if f.field_name not in tf and f.value is None:
            continue
        yield f.key, f.value_type, values_agree(f.value_type, f.value, expected), f.needs_review
    tlines = truth.get("line_items") or []
    n = max(len(tlines), 1 + max([f.line_index for f in doc_fields if f.line_index is not None], default=-1))
    for i in range(n):
        tl = tlines[i] if i < len(tlines) else {}
        names = set(tl) | {f.field_name for f in doc_fields if f.line_index == i}
        for name in names:
            f = by_key.get(f"line_items[{i}].{name}")
            if f is None:
                if tl.get(name) is not None:
                    yield f"line_items[{i}].{name}", "missing", False, True  # missed row: counts as caught (doc is flagged)
                continue
            expected = normalise(f.value_type, None if tl.get(name) is None else str(tl.get(name)))
            yield f.key, f.value_type, values_agree(f.value_type, f.value, expected), f.needs_review


def run(dataset: Path, out_dir: Path, fake: bool = False, limit: int | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="sereno-eval-"))
    os.environ.update({"SERENO_DATABASE_URL": f"sqlite:///{tmp}/eval.db", "SERENO_DATA_DIR": str(tmp / "data"),
                       "SERENO_QA_SAMPLE_RATE": "0", "SERENO_ENV": "dev"})
    from sereno import config, db
    config.get_settings.cache_clear()
    db.reset_engine()
    from sqlalchemy import select

    from sereno import jobs, pipeline
    from sereno.db import init_db, session_scope
    from sereno.extraction.llm import FakeLLM, set_llm
    from sereno.models import Document, ExtractedField, Tenant, User
    init_db()
    with session_scope() as s:
        t = Tenant(name="eval", managed_review=False)
        s.add(t)
        s.flush()
        u = User(tenant_id=t.id, email="eval@local", name="eval", role="admin", password_hash="x")
        s.add(u)
        s.flush()
        tid, uid = t.id, u.id

    per_doc = []
    for n, (src, truth) in enumerate(_items(dataset)):
        if limit and n >= limit:
            break
        if fake:
            from sereno.eval.fake_model import TruthResponder
            from sereno.eval.synth import SynthDoc
            sd = SynthDoc(truth["doc_type"], truth.get("fields", {}), truth.get("line_items", []), bucket=truth.get("bucket", ""))
            set_llm(FakeLLM(TruthResponder(sd)))
        t0 = time.monotonic()
        with session_scope() as s:
            did = pipeline.ingest_upload(s, tid, uid, src.name, src.read_bytes(), truth.get("doc_type", "auto")).document_id
        jobs.drain()
        with session_scope() as s:
            d = s.get(Document, did)
            fields = s.execute(select(ExtractedField).where(ExtractedField.document_id == did)).scalars().all()
            rows = list(_compare(fields, truth)) if d.status not in ("unreadable", "failed") else []
            per_doc.append({"file": str(src.relative_to(dataset)), "bucket": truth.get("bucket") or src.parent.name,
                            "doc_type": truth.get("doc_type"), "synthetic": bool(truth.get("synthetic")),
                            "status": d.status, "model": d.model_used, "cost_usd": d.cost_usd or 0.0,
                            "seconds": round(time.monotonic() - t0, 2), "quality_bucket": d.quality_bucket,
                            "fields": len(rows), "correct": sum(r[2] for r in rows),
                            "auto": sum(1 for r in rows if not r[3]),
                            "auto_wrong": sum(1 for r in rows if not r[3] and not r[2]),
                            "wrong": sum(1 for r in rows if not r[2]),
                            "wrong_flagged": sum(1 for r in rows if not r[2] and r[3]),
                            "errors": [r[0] for r in rows if not r[2]][:20],
                            "escapes": [r[0] for r in rows if not r[2] and not r[3]][:20]})
        print(f"[{n + 1}] {per_doc[-1]['file']}: {per_doc[-1]['status']} "
              f"{per_doc[-1]['correct']}/{per_doc[-1]['fields']} correct, escapes={per_doc[-1]['escapes']}")

    report = summarise(per_doc)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"eval_{stamp}.json").write_text(json.dumps(report, indent=1))
    if not fake:
        (out_dir / "eval_latest.json").write_text(json.dumps(report, indent=1))
    (out_dir / f"eval_{stamp}.md").write_text(to_markdown(report))
    print(to_markdown(report))
    return report


def _cell(docs: list[dict]) -> dict:
    f = sum(d["fields"] for d in docs)
    auto = sum(d["auto"] for d in docs)
    wrong = sum(d["wrong"] for d in docs)
    pct = lambda n, d: round(100 * n / d, 2) if d else None  # noqa: E731
    return {
        "documents": len(docs),
        "unreadable_or_failed": sum(1 for d in docs if d["status"] in ("unreadable", "failed")),
        "field_accuracy_pct": pct(sum(d["correct"] for d in docs), f),
        "auto_accept_pct": pct(auto, f),
        "escape_rate_pct": pct(sum(d["auto_wrong"] for d in docs), auto),
        "error_catch_rate_pct": pct(sum(d["wrong_flagged"] for d in docs), wrong),
        "straight_through_docs_pct": pct(sum(1 for d in docs if d["status"] == "ready"), len(docs)),
        "all_fields_correct_docs_pct": pct(sum(1 for d in docs if d["fields"] and d["wrong"] == 0), len(docs)),
        "mean_seconds": round(statistics.mean(d["seconds"] for d in docs), 1) if docs else None,
        "mean_cost_usd": round(statistics.mean(d["cost_usd"] for d in docs), 4) if docs else None,
        "synthetic_docs": sum(1 for d in docs if d["synthetic"]),
    }


def summarise(per_doc: list[dict]) -> dict:
    cells = defaultdict(list)
    for d in per_doc:
        cells[f"{d['doc_type']}/{d['bucket']}"].append(d)
    by_cell = {k: _cell(v) for k, v in sorted(cells.items())}
    gate = []
    for k, c in by_cell.items():
        reasons = []
        if c["synthetic_docs"]:
            reasons.append("contains synthetic documents")
        if c["documents"] < GATE["min_docs"]:
            reasons.append(f"only {c['documents']} docs (need {GATE['min_docs']})")
        if c["escape_rate_pct"] is None or c["escape_rate_pct"] > GATE["max_escape_rate_pct"]:
            reasons.append(f"escape rate {c['escape_rate_pct']}% > {GATE['max_escape_rate_pct']}%")
        if c["field_accuracy_pct"] is None or c["field_accuracy_pct"] < GATE["min_field_accuracy_pct"]:
            reasons.append(f"field accuracy {c['field_accuracy_pct']}% < {GATE['min_field_accuracy_pct']}%")
        gate.append({"cell": k, "demo_safe": not reasons, "reasons": reasons})
    return {"run_at": datetime.now(timezone.utc).isoformat(), "gate_rules": GATE, "overall": _cell(per_doc),
            "by_cell": by_cell, "gate": gate, "documents": per_doc}


def to_markdown(r: dict) -> str:
    cols = ["documents", "field_accuracy_pct", "escape_rate_pct", "error_catch_rate_pct", "auto_accept_pct",
            "straight_through_docs_pct", "unreadable_or_failed", "mean_seconds", "mean_cost_usd"]
    lines = [f"# Evaluation {r['run_at']}", "", "| cell | " + " | ".join(cols) + " | demo-safe |",
             "|" + "---|" * (len(cols) + 2)]
    gate = {g["cell"]: g for g in r["gate"]}
    for k, c in r["by_cell"].items():
        g = gate[k]
        lines.append(f"| {k} | " + " | ".join(str(c[x]) for x in cols) + f" | {'YES' if g['demo_safe'] else 'no: ' + '; '.join(g['reasons'])} |")
    o = r["overall"]
    lines += ["", f"Overall: {o['documents']} docs, field accuracy {o['field_accuracy_pct']}%, "
                  f"escape rate {o['escape_rate_pct']}%, errors caught {o['error_catch_rate_pct']}%."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--fake", action="store_true", help="plumbing check with the truth-driven fake model (no API calls)")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    run(a.dataset, a.out, a.fake, a.limit)


if __name__ == "__main__":
    main()
