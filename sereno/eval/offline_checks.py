"""Offline test of every stage that runs WITHOUT the model, on a labelled real dataset:

1. Pre-check: quality signals, bucket, auto-corrections, unreadable gate, rotation hint.
2. Validators on ground truth: on a correctly-read document every arithmetic/compliance check
   should pass unless the document itself is inconsistent. A check failing on true values is a
   FALSE ALARM (costs reviewer time) unless the paper really is wrong; each failure is listed so
   a human can classify it.

    python -m sereno.eval.offline_checks eval_data/web/labelled
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from sereno.eval.run_eval import _items
from sereno.extraction.doc_specs import SPECS
from sereno.extraction.normalize import normalise
from sereno.extraction.result import ExtractedDoc, FieldValue, line_key
from sereno.ingest import loader, preprocess, quality
from sereno.validation.checks import run_checks


def truth_doc(truth: dict) -> ExtractedDoc | None:
    spec = SPECS.get(truth["doc_type"])
    if spec is None:
        return None
    tf = truth.get("fields", {})
    header = {f.name: FieldValue(f.name, f, None, normalise(f.type, None if tf.get(f.name) is None else str(tf[f.name])))
              for f in spec.fields}
    lines = [{f.name: FieldValue(line_key(i, f.name), f, i,
                                 normalise(f.type, None if li.get(f.name) is None else str(li[f.name])))
              for f in spec.line_fields} for i, li in enumerate(truth.get("line_items") or [])]
    return ExtractedDoc(spec, header, lines)


def run(dataset: Path) -> dict:
    rows = []
    for src, truth in _items(dataset):
        t0 = time.monotonic()
        doc = loader.load(src.read_bytes(), src.name)
        p = doc.pages[0]
        rot = quality.quarter_turn_hint(p.image)
        img = preprocess.rotate_quarter(p.image, rot) if rot else p.image
        q0 = quality.assess(img, p.source_dpi, p.native_text)
        _, applied = preprocess.enhance(img, q0)
        reason = quality.unreadable_reason(q0, 1.0, 0.06, 5.0, 0.002)
        td = truth_doc(truth)
        checks = run_checks(td) if td else []
        failed = [c for c in checks if c.failed and c.check_id != "required_fields"]
        rows.append({
            "file": src.name, "bucket": truth["bucket"], "doc_type": truth["doc_type"], "tags": truth.get("tags", []),
            "size": f"{p.image.shape[1]}x{p.image.shape[0]}", "quality_bucket": "unreadable" if reason else quality.bucket(q0),
            "unreadable_reason": reason, "rotation_hint": rot, "skew": q0.skew_deg, "char_h": round(q0.char_height_px, 1),
            "sharpness_norm": round(q0.sharpness_norm, 1), "text_contrast": round(q0.text_contrast, 2),
            "corrections": applied, "ms": int((time.monotonic() - t0) * 1000),
            "checks_run": sum(1 for c in checks if c.status != "skip"),
            "checks_failed_on_truth": [f"{c.check_id}: {c.message}" for c in failed]})
    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[r["bucket"]].append(r)
    summary = {b: {"docs": len(rs), "quality_buckets": dict(Counter(r["quality_bucket"] for r in rs)),
                   "refused_unreadable": sum(1 for r in rs if r["unreadable_reason"]),
                   "auto_corrected": sum(1 for r in rs if r["corrections"]),
                   "checks_run": sum(r["checks_run"] for r in rs),
                   "checks_failed_on_truth": sum(len(r["checks_failed_on_truth"]) for r in rs),
                   "mean_ms": round(sum(r["ms"] for r in rs) / len(rs))} for b, rs in sorted(by_bucket.items())}
    return {"summary": summary, "documents": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--out", type=Path, default=Path("reports/offline_checks.json"))
    a = ap.parse_args()
    rep = run(a.dataset)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    for r in rep["documents"]:
        print(f"{r['bucket']:<12} {r['file']:<30} {r['size']:>10} q={r['quality_bucket']:<10} rot={r['rotation_hint']:<3} "
              f"skew={r['skew']:+5.1f} charH={r['char_h']:<5} sharpN={r['sharpness_norm']:<7} "
              f"fix={','.join(c.split(':')[0] for c in r['corrections']) or '-'}")
        if r["unreadable_reason"]:
            print(f"{'':12} REFUSED: {r['unreadable_reason']}")
        for c in r["checks_failed_on_truth"]:
            print(f"{'':12} CHECK FAILED ON TRUTH: {c}")
    print(json.dumps(rep["summary"], indent=1))


if __name__ == "__main__":
    main()
