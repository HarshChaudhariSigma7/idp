import random

from sereno.eval.synth import invoice_truth, make_gstin, rupees_in_words
from sereno.extraction.doc_specs import INVOICE, LR
from sereno.extraction.normalize import normalise, parse_amount, parse_date, raw_consistent
from sereno.extraction.result import ExtractedDoc, FieldValue, line_key
from sereno.validation.checks import run_checks
from sereno.validation.gst import gstin_problem, state_code_for, words_to_amount


def build(spec, truth, lines):
    header = {f.name: FieldValue(f.name, f, None, truth.get(f.name)) for f in spec.fields}
    ls = [{f.name: FieldValue(line_key(i, f.name), f, i, ln.get(f.name)) for f in spec.line_fields}
          for i, ln in enumerate(lines)]
    return ExtractedDoc(spec, header, ls)


def failed(checks):
    return {c.check_id for c in checks if c.failed}


def test_gstin_checksum_real_and_misread():
    assert gstin_problem("27AAPFU0939F1ZV") is None
    assert gstin_problem("24AAACC1206D1ZM") is None
    assert "check digit" in gstin_problem("27AAPFU0939F1ZY")   # last char misread
    assert "check digit" in gstin_problem("27AAPFU0989F1ZV")   # 3 -> 8
    assert "format" in gstin_problem("27AAPFU0939F1Z")
    rng = random.Random(1)
    for _ in range(50):
        assert gstin_problem(make_gstin(rng, "27")) is None


def test_indian_number_and_date_parsing():
    assert parse_amount("₹ 1,23,456.50/-") == parse_amount("123456.50")
    assert float(parse_amount("१२,३४५.००")) == 12345.0
    assert float(parse_amount("(1,200.00)")) == -1200.0
    assert parse_date("05/06/2025").isoformat() == "2025-06-05"  # day-first
    assert parse_date("5-Jun-25").isoformat() == "2025-06-05"
    assert normalise("percent", "9% + 9%") == 18.0
    assert raw_consistent("date", "2025-05-06", "05/06/2025") is False  # model swapped day/month
    assert raw_consistent("number", 123456.5, "1,23,456.50") is True


def test_amount_in_words_round_trip():
    for amt in (1.0, 99.5, 1234.0, 100000.0, 123456.75, 20500000.0, 987654321.0):
        assert float(words_to_amount(rupees_in_words(amt))) == amt
    assert state_code_for("27-Maharashtra") == "27"
    assert state_code_for("Tamil Nadu") == "33"


def test_clean_invoice_passes_all_checks():
    rng = random.Random(3)
    for _ in range(30):
        truth, lines = invoice_truth(rng, n_lines=rng.randint(1, 6))
        checks = run_checks(build(INVOICE, truth, lines))
        assert not failed(checks), [c.message for c in checks if c.failed]


def test_arithmetic_catches_misread_total_and_tax():
    truth, lines = invoice_truth(random.Random(4), 3, intra=True)
    bad = dict(truth, grand_total=truth["grand_total"] + 900)  # 1 digit misread
    f = failed(run_checks(build(INVOICE, bad, lines)))
    assert "grand_total" in f and "amount_in_words" in f
    bad2 = dict(truth, cgst_amount=truth["cgst_amount"] + 50)
    f2 = failed(run_checks(build(INVOICE, bad2, lines)))
    assert {"cgst_eq_sgst", "grand_total"} <= f2
    lines3 = [dict(lines[0], quantity=lines[0]["quantity"] + 1)] + lines[1:]
    assert "line_math:0" in failed(run_checks(build(INVOICE, truth, lines3)))


def test_tax_type_must_match_states():
    truth, lines = invoice_truth(random.Random(5), 2, intra=True)
    # intra-state supply billed as IGST is a compliance error
    t = dict(truth, igst_amount=truth["cgst_amount"] * 2, cgst_amount=None, sgst_amount=None)
    ls = [dict(l, igst_amount=(l["cgst_amount"] or 0) * 2, cgst_amount=None, sgst_amount=None) for l in lines]
    assert "tax_type_vs_state" in failed(run_checks(build(INVOICE, t, ls)))


def test_lr_freight_math():
    t = {"lr_number": "123", "lr_date": None, "charged_weight_kg": 1000.0, "actual_weight_kg": 950.0,
         "freight_rate": 3.0, "freight_amount": 3000.0, "other_charges": 200.0, "total_freight": 3200.0,
         "packages_count": 10}
    assert not {"freight_math", "total_freight"} & failed(run_checks(build(LR, t, [])))
    t2 = dict(t, total_freight=3800.0)
    assert "total_freight" in failed(run_checks(build(LR, t2, [])))
    t3 = dict(t, freight_rate=30.0, freight_amount=300.0, total_freight=500.0)  # per-quintal rate
    assert "freight_math" not in failed(run_checks(build(LR, t3, [])))
