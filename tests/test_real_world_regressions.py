"""Bugs found by running the pipeline on real web-sourced documents (eval_data/web, not in git),
reproduced synthetically so they stay fixed."""
import random

import cv2
import numpy as np

from sereno.eval.synth import invoice_truth, render_invoice
from sereno.extraction.doc_specs import INVOICE
from sereno.ingest import quality
from sereno.validation.checks import run_checks
from tests.test_validation import build, failed


def _img():
    t, l = invoice_truth(random.Random(7), 3)
    return render_invoice(t, l)[0]


def test_paper_texture_does_not_trigger_false_low_resolution_refusal():
    # real case: readable pink voucher photo refused as "too low resolution" (speckle outvoted glyphs)
    img = _img().copy()
    rng = np.random.default_rng(1)
    ys, xs = rng.integers(0, img.shape[0], 15000), rng.integers(0, img.shape[1], 15000)
    for y, x in zip(ys, xs):
        cv2.rectangle(img, (int(x), int(y)), (int(x) + 1, int(y) + 1), (110, 110, 110), -1)
    q = quality.assess(img)
    assert q.char_height_px >= 12
    assert quality.unreadable_reason(q, 1.0, 0.06, 5.0, 0.002) is None


def test_sideways_scan_detected_without_model():
    img = _img()
    assert quality.quarter_turn_hint(img) == 0
    assert quality.quarter_turn_hint(cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)) == 90


def test_einvoice_line_total_without_per_line_tax_columns():
    # real case: NIC e-invoice prints taxable + rate + total per line, tax only in the footer
    t = {"subtotal": 47120.0, "cgst_amount": 4240.8, "sgst_amount": 4240.8, "grand_total": 55601.6,
         "supplier_gstin": None}
    lines = [{"quantity": 124.0, "rate": 380.0, "taxable_value": 47120.0, "gst_rate": 18.0, "line_total": 55601.6}]
    assert not {c for c in failed(run_checks(build(INVOICE, t, lines))) if c.startswith("line_total")}


def test_tax_inclusive_mixed_rate_receipt_is_not_a_false_alarm():
    # real case: POS receipt, items at 0/3/5/12%, line amounts tax-inclusive, no per-line rate column
    t = {"subtotal": 900.0, "igst_amount": 68.0, "grand_total": 968.0}
    lines = [{"quantity": 1.0, "rate": r, "line_total": lt} for r, lt in
             [(400.0, 448.0), (100.0, 105.0), (100.0, 103.0), (150.0, 150.0), (50.0, 50.0), (100.0, 112.0)]]
    assert "tax_rate_reconcile" not in failed(run_checks(build(INVOICE, t, lines)))
