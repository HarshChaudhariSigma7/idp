import random

import numpy as np

from sereno.eval.synth import degrade, invoice_truth, render_invoice, render_invoice_pdf
from sereno.ingest import loader, preprocess, quality


def _clean():
    t, l = invoice_truth(random.Random(1), 3)
    img, _, _ = render_invoice(t, l)
    return img, t, l


def test_digital_pdf_detected_with_text_layer():
    t, l = invoice_truth(random.Random(2), 2)
    doc = loader.load(render_invoice_pdf(t, l))
    assert doc.is_digital
    assert t["supplier_gstin"] in doc.pages[0].text
    q = quality.assess(doc.pages[0].image, doc.pages[0].source_dpi, True)
    assert quality.bucket(q) == "digital"


def test_skew_is_measured_and_corrected():
    img, _, _ = _clean()
    import cv2
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), 3.0, 1.0)
    skewed = cv2.warpAffine(img, m, (w, h), borderValue=(255, 255, 255))
    q = quality.assess(skewed)
    assert 2.3 <= abs(q.skew_deg) <= 3.7
    fixed, applied = preprocess.enhance(skewed, q)
    assert any(a.startswith("deskew:") and a != "deskew:reverted" for a in applied)
    assert abs(quality.estimate_skew(cv2.cvtColor(fixed, cv2.COLOR_RGB2GRAY))) < 0.8


def test_poor_scan_scores_worse_than_good_scan():
    img, _, _ = _clean()
    rng = random.Random(3)
    good = quality.assess(degrade(img, "good_scan", rng))
    poor = quality.assess(degrade(img, "poor_scan", rng))
    assert quality.bucket(good) == "good_scan"
    assert quality.bucket(poor) == "poor_scan"
    assert poor.sharpness < good.sharpness
    assert poor.char_height_px < good.char_height_px


def test_unreadable_is_refused_not_guessed():
    img, _, _ = _clean()
    bad = degrade(img, "unreadable", random.Random(4))
    q = quality.assess(bad)
    assert quality.unreadable_reason(q, 1.0, 0.06, 5.0, 0.002) is not None
    import cv2
    very_blurry = quality.assess(cv2.GaussianBlur(img, (0, 0), 5))
    assert "blurry" in quality.unreadable_reason(very_blurry, 1.0, 0.06, 5.0, 0.002)
    # a degraded but readable scan must NOT be refused
    poor = quality.assess(degrade(img, "poor_scan", random.Random(5)))
    assert quality.unreadable_reason(poor, 1.0, 0.06, 5.0, 0.002) is None


def test_blank_page_is_unreadable():
    blank = np.full((2000, 1400, 3), 250, np.uint8)
    q = quality.assess(blank)
    assert "blank" in quality.unreadable_reason(q, 1.0, 0.06, 5.0, 0.002)


def test_heic_rejected_with_plain_message():
    try:
        loader.sniff_mime(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20)
    except loader.UnsupportedDocument as e:
        assert "JPEG" in str(e)
    else:
        raise AssertionError("expected rejection")
