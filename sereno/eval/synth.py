"""Synthetic Indian business documents with exact ground truth.

PURPOSE: pipeline smoke tests, regression tests and offline demos of the review UX.
NOT A SUBSTITUTE for the 100+ real-document validation in docs/EVALUATION.md: synthetic layouts
are cleaner and more regular than real vendor documents, so accuracy on them proves plumbing,
not product quality. Never quote synthetic accuracy to a customer.
"""
from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from sereno.validation.gst import STATE_CODES, gstin_check_char

FONT_DIR = Path("/usr/share/fonts/truetype")
_FONT_CANDIDATES = {
    "regular": ["freefont/FreeSans.ttf", "dejavu/DejaVuSans.ttf", "liberation/LiberationSans-Regular.ttf"],
    "bold": ["freefont/FreeSansBold.ttf", "dejavu/DejaVuSans-Bold.ttf", "liberation/LiberationSans-Bold.ttf"],
    "mono": ["freefont/FreeMono.ttf", "dejavu/DejaVuSansMono.ttf"],
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for rel in _FONT_CANDIDATES[kind]:
        p = FONT_DIR / rel
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


VENDORS = ["Shree Ganesh Engineering Works", "Patel Castings Pvt Ltd", "Kaveri Polymers LLP", "Bharat Fasteners Co.",
           "Om Sai Packaging Industries", "Sunrise Steel Traders", "Deccan Hydraulics Pvt Ltd", "Maruti Wire Products"]
BUYERS = ["Sahyadri Auto Components Ltd", "Narmada Pumps & Motors Ltd", "Vindhya Agro Equipment Pvt Ltd"]
ITEMS = [("MS Hex Bolt M12x50", "73181500", "NOS"), ("CI Pump Casing 4in", "84139190", "NOS"),
         ("HDPE Granules Grade B", "39012000", "KGS"), ("Corrugated Box 5 Ply", "48191010", "NOS"),
         ("Hydraulic Hose 1/2in", "40093100", "MTR"), ("Bright Bar EN8 25mm", "72152090", "KGS"),
         ("Ball Bearing 6205 ZZ", "84821011", "NOS"), ("Copper Winding Wire 22 SWG", "74081990", "KGS")]
CITIES = [("Pune", "27"), ("Nashik", "27"), ("Ahmedabad", "24"), ("Rajkot", "24"), ("Indore", "23"), ("Bengaluru", "29"),
          ("Chennai", "33"), ("Ludhiana", "03"), ("Faridabad", "06"), ("Coimbatore", "33")]


def make_gstin(rng: random.Random, state: str) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = "".join(rng.choice(letters) for _ in range(3)) + rng.choice("CPFH") + rng.choice(letters) + \
        f"{rng.randint(0, 9999):04d}" + rng.choice(letters)
    first14 = state + pan + str(rng.randint(1, 9)) + "Z"
    return first14 + gstin_check_char(first14)


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    return ((_ONES[h] + " Hundred" + (" " if r else "")) if h else "") + (_two(r) if r else "")


def rupees_in_words(amount: float) -> str:
    rupees, paise = int(amount), int(round((amount - int(amount)) * 100))
    parts = []
    crore, rem = divmod(rupees, 10_000_000)
    lakh, rem = divmod(rem, 100_000)
    thousand, rem = divmod(rem, 1000)
    if crore:
        parts.append(_three(crore) + " Crore")
    if lakh:
        parts.append(_two(lakh) + " Lakh")
    if thousand:
        parts.append(_two(thousand) + " Thousand")
    if rem:
        parts.append(_three(rem))
    s = "Rupees " + (" ".join(parts) or "Zero")
    if paise:
        s += " and Paise " + _two(paise)
    return s + " Only"


def inr(x: float) -> str:
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        g = []
        while len(head) > 2:
            g.insert(0, head[-2:])
            head = head[:-2]
        if head:
            g.insert(0, head)
        whole = ",".join(g + [tail])
    return ("-" if neg else "") + whole + "." + frac


@dataclass
class SynthDoc:
    doc_type: str
    truth: dict
    lines: list[dict]
    boxes: dict = field(default_factory=dict)  # key -> (page, {x0,y0,x1,y1})
    raw: dict = field(default_factory=dict)    # key -> text as printed
    image: np.ndarray | None = None
    pdf: bytes | None = None
    bucket: str = "digital"


# --- invoice -------------------------------------------------------------------------------------

def invoice_truth(rng: random.Random, n_lines: int = 3, intra: bool | None = None) -> tuple[dict, list[dict]]:
    city, st = rng.choice(CITIES)
    intra = rng.random() < 0.6 if intra is None else intra
    bcity, bst = (city, st) if intra else rng.choice([c for c in CITIES if c[1] != st])
    rate_pct = rng.choice([5.0, 12.0, 18.0, 18.0, 28.0])
    lines = []
    for item, hsn, unit in rng.sample(ITEMS, n_lines):
        qty = float(rng.choice([10, 25, 40, 50, 100, 120, 250, 500]))
        rate = round(rng.uniform(12, 2500), 2)
        taxable = round(qty * rate, 2)
        tax = round(taxable * rate_pct / 100, 2)
        line = {"description": item, "hsn_sac": hsn, "quantity": qty, "unit": unit, "rate": rate,
                "taxable_value": taxable, "gst_rate": rate_pct, "line_total": round(taxable + tax, 2)}
        if intra:
            line["cgst_amount"] = line["sgst_amount"] = round(tax / 2, 2)
            line["igst_amount"] = None
        else:
            line["cgst_amount"] = line["sgst_amount"] = None
            line["igst_amount"] = tax
        line["line_total"] = round(taxable + (line["cgst_amount"] or 0) * 2 + (line["igst_amount"] or 0), 2)
        lines.append(line)
    subtotal = round(sum(l["taxable_value"] for l in lines), 2)
    cgst = round(sum(l["cgst_amount"] or 0 for l in lines), 2) if intra else None
    igst = None if intra else round(sum(l["igst_amount"] for l in lines), 2)
    pre = subtotal + (cgst or 0) * 2 + (igst or 0)
    total = float(round(pre))
    ro = round(total - pre, 2)
    inv_date = date.today() - timedelta(days=rng.randint(1, 60))
    fy = inv_date.year if inv_date.month >= 4 else inv_date.year - 1
    truth = {
        "invoice_number": f"{rng.choice(['INV', 'TI', 'GST'])}/{fy % 100}-{(fy + 1) % 100}/{rng.randint(1, 4999):04d}",
        "invoice_date": inv_date.isoformat(),
        "supplier_name": rng.choice(VENDORS), "supplier_gstin": make_gstin(rng, st),
        "supplier_state": STATE_CODES[st], "buyer_name": rng.choice(BUYERS), "buyer_gstin": make_gstin(rng, bst),
        "place_of_supply": f"{bst}-{STATE_CODES[bst]}", "po_reference": f"PO/{rng.randint(1000, 9999)}",
        "subtotal": subtotal, "cgst_amount": cgst, "sgst_amount": cgst, "igst_amount": igst,
        "round_off": ro if ro else None, "grand_total": total, "amount_in_words": rupees_in_words(total),
        "supplier_address": f"Plot {rng.randint(1, 300)}, MIDC Industrial Area, {city}",
        "buyer_address": f"Gat No. {rng.randint(10, 999)}, {bcity}",
    }
    return truth, lines


class _Canvas:
    def __init__(self, w=1654, h=2339):
        self.img = Image.new("RGB", (w, h), "white")
        self.d = ImageDraw.Draw(self.img)
        self.w, self.h = w, h
        self.boxes: dict = {}
        self.raw: dict = {}

    def text(self, xy, s, size=26, kind="regular", key=None, fill=(20, 20, 20)):
        f = _font(kind, size)
        self.d.text(xy, s, font=f, fill=fill)
        if key:
            x0, y0, x1, y1 = self.d.textbbox(xy, s, font=f)
            self.boxes[key] = (1, {"x0": x0 / self.w, "y0": y0 / self.h, "x1": x1 / self.w, "y1": y1 / self.h})
            self.raw[key] = s
        return self.d.textbbox(xy, s, font=f)

    def label_value(self, x, y, label, value, key, size=24):
        b = self.text((x, y), label, size, "bold")
        self.text((b[2] + 12, y), value, size, key=key)

    def line(self, xy, width=2):
        self.d.line(xy, fill=(40, 40, 40), width=width)


def render_invoice(truth: dict, lines: list[dict], hindi_labels: bool = False) -> tuple[np.ndarray, dict, dict]:
    c = _Canvas()
    c.text((80, 60), "TAX INVOICE" + ("  /  कर बीजक" if hindi_labels else ""), 44, "bold")
    c.text((1180, 70), "ORIGINAL FOR RECIPIENT", 22, key="copy_type")
    c.text((80, 140), truth["supplier_name"], 34, "bold", key="supplier_name")
    c.text((80, 190), truth["supplier_address"], 24, key="supplier_address")
    c.label_value(80, 230, "GSTIN:", truth["supplier_gstin"], "supplier_gstin")
    c.label_value(80, 270, "State:", truth["supplier_state"], "supplier_state")
    c.label_value(1000, 150, "Invoice No:", truth["invoice_number"], "invoice_number")
    d = date.fromisoformat(truth["invoice_date"])
    c.label_value(1000, 195, "Date:", d.strftime("%d/%m/%Y"), "invoice_date")
    c.label_value(1000, 240, "PO Ref:", truth["po_reference"], "po_reference")
    c.line([(60, 320), (1594, 320)])
    c.text((80, 340), "Bill To" + (" / बिल प्राप्तकर्ता" if hindi_labels else ""), 24, "bold")
    c.text((80, 375), truth["buyer_name"], 28, key="buyer_name")
    c.text((80, 415), truth["buyer_address"], 24, key="buyer_address")
    c.label_value(80, 455, "GSTIN:", truth["buyer_gstin"], "buyer_gstin")
    c.label_value(1000, 375, "Place of Supply:", truth["place_of_supply"], "place_of_supply")
    y = 530
    cols = [80, 140, 560, 720, 830, 900, 1060, 1260, 1420]
    heads = ["#", "Description", "HSN", "Qty", "Unit", "Rate", "Taxable", "GST %", "Amount"]
    c.line([(60, y - 10), (1594, y - 10)])
    for x, hname in zip(cols, heads):
        c.text((x, y), hname, 22, "bold")
    y += 45
    c.line([(60, y - 8), (1594, y - 8)])
    for i, ln in enumerate(lines):
        vals = [str(i + 1), ln["description"], ln["hsn_sac"], f"{ln['quantity']:g}", ln["unit"], inr(ln["rate"]),
                inr(ln["taxable_value"]), f"{ln['gst_rate']:g}%", inr(ln["line_total"])]
        keys = [None, "description", "hsn_sac", "quantity", "unit", "rate", "taxable_value", "gst_rate", "line_total"]
        row_top = y
        for x, v, k in zip(cols, vals, keys):
            c.text((x, y), v, 22, key=f"line_items[{i}].{k}" if k else None)
        c.boxes[f"line_items[{i}]"] = (1, {"x0": 60 / c.w, "y0": (row_top - 4) / c.h, "x1": 1594 / c.w, "y1": (y + 34) / c.h})
        y += 48
    c.line([(60, y), (1594, y)])
    y += 30
    rows = [("Taxable Value", "subtotal", truth["subtotal"])]
    if truth["cgst_amount"] is not None:
        rows += [("CGST", "cgst_amount", truth["cgst_amount"]), ("SGST", "sgst_amount", truth["sgst_amount"])]
    else:
        rows += [("IGST", "igst_amount", truth["igst_amount"])]
    if truth.get("round_off"):
        rows.append(("Round Off", "round_off", truth["round_off"]))
    rows.append(("Grand Total" + (" / कुल" if hindi_labels else ""), "grand_total", truth["grand_total"]))
    for label, key, v in rows:
        c.text((1000, y), label, 24, "bold")
        c.text((1330, y), ("Rs. " if key == "grand_total" else "") + inr(v), 24, key=key)
        y += 42
    y += 20
    c.text((80, y), truth["amount_in_words"], 22, key="amount_in_words")
    c.text((1150, y + 160), "For " + truth["supplier_name"][:28], 22, "bold")
    c.text((1250, y + 260), "Authorised Signatory", 20)
    return np.asarray(c.img), c.boxes, c.raw


def render_invoice_pdf(truth: dict, lines: list[dict]) -> bytes:
    """Digital PDF with a real text layer (like one exported from Tally/SAP)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl

    buf = io.BytesIO()
    cv = rl.Canvas(buf, pagesize=A4)
    W, H = A4
    y = H - 50
    cv.setFont("Helvetica-Bold", 18)
    cv.drawString(40, y, "TAX INVOICE")
    cv.setFont("Helvetica", 9)
    cv.drawString(420, y, "ORIGINAL FOR RECIPIENT")
    y -= 30
    cv.setFont("Helvetica-Bold", 13)
    cv.drawString(40, y, truth["supplier_name"])
    cv.setFont("Helvetica", 10)
    cv.drawString(40, y - 16, truth["supplier_address"])
    cv.drawString(40, y - 32, f"GSTIN: {truth['supplier_gstin']}")
    cv.drawString(40, y - 48, f"State: {truth['supplier_state']}")
    cv.drawString(360, y, f"Invoice No: {truth['invoice_number']}")
    cv.drawString(360, y - 16, f"Date: {date.fromisoformat(truth['invoice_date']).strftime('%d-%m-%Y')}")
    cv.drawString(360, y - 32, f"PO Ref: {truth['po_reference']}")
    y -= 80
    cv.setFont("Helvetica-Bold", 10)
    cv.drawString(40, y, "Bill To")
    cv.setFont("Helvetica", 10)
    cv.drawString(40, y - 15, truth["buyer_name"])
    cv.drawString(40, y - 30, truth["buyer_address"])
    cv.drawString(40, y - 45, f"GSTIN: {truth['buyer_gstin']}")
    cv.drawString(360, y - 15, f"Place of Supply: {truth['place_of_supply']}")
    y -= 75
    cols = [40, 60, 230, 290, 330, 365, 425, 490, 520]
    cv.setFont("Helvetica-Bold", 8)
    for x, h in zip(cols, ["#", "Description", "HSN", "Qty", "Unit", "Rate", "Taxable", "GST%", "Amount"]):
        cv.drawString(x, y, h)
    cv.setFont("Helvetica", 8)
    for i, ln in enumerate(lines):
        y -= 16
        for x, v in zip(cols, [str(i + 1), ln["description"], ln["hsn_sac"], f"{ln['quantity']:g}", ln["unit"],
                               inr(ln["rate"]), inr(ln["taxable_value"]), f"{ln['gst_rate']:g}", inr(ln["line_total"])]):
            cv.drawString(x, y, v)
    y -= 30
    cv.setFont("Helvetica", 10)
    rows = [("Taxable Value", truth["subtotal"])]
    rows += ([("CGST", truth["cgst_amount"]), ("SGST", truth["sgst_amount"])] if truth["cgst_amount"] is not None
             else [("IGST", truth["igst_amount"])])
    if truth.get("round_off"):
        rows.append(("Round Off", truth["round_off"]))
    rows.append(("Grand Total", truth["grand_total"]))
    for label, v in rows:
        cv.drawString(360, y, label)
        cv.drawRightString(560, y, inr(v))
        y -= 15
    cv.drawString(40, y - 10, truth["amount_in_words"])
    cv.showPage()
    cv.save()
    return buf.getvalue()


# --- LR (bilingual) ------------------------------------------------------------------------------

def lr_truth(rng: random.Random) -> dict:
    (fc, fs), (tc, ts) = rng.sample(CITIES, 2)
    pk = rng.randint(5, 120)
    aw = float(rng.randint(300, 9000))
    cw = float(max(aw, round(aw / 100 + 0.5) * 100))
    rate = float(rng.choice([2.5, 3, 3.5, 4, 5]))
    fr = round(cw * rate, 2)
    oc = float(rng.choice([0, 150, 250, 400]))
    st = rng.choice(["MH", "GJ", "KA", "TN", "MP"])
    return {"lr_number": str(rng.randint(10000, 99999)), "lr_date": (date.today() - timedelta(days=rng.randint(1, 20))).isoformat(),
            "transporter_name": rng.choice(["Shri Balaji Roadlines", "Jai Hind Transport Co.", "Om Logistics Carriers"]),
            "transporter_gstin": make_gstin(rng, fs), "vehicle_number": f"{st}{rng.randint(1, 50):02d}{rng.choice(['AB', 'CD', 'GT', 'TR'])}{rng.randint(1000, 9999)}",
            "from_location": fc, "to_location": tc, "consignor_name": rng.choice(VENDORS), "consignor_gstin": make_gstin(rng, fs),
            "consignee_name": rng.choice(BUYERS), "consignee_gstin": make_gstin(rng, ts),
            "invoice_reference": f"INV/{rng.randint(1, 999):03d}", "packages_count": pk, "actual_weight_kg": aw,
            "charged_weight_kg": cw, "freight_rate": rate, "freight_amount": fr, "other_charges": oc or None,
            "total_freight": round(fr + oc, 2), "payment_mode": rng.choice(["To Pay", "Paid", "TBB"])}


def render_lr(t: dict) -> tuple[np.ndarray, dict, dict]:
    c = _Canvas(1654, 1169)  # A5 landscape-ish
    c.text((60, 40), t["transporter_name"].upper(), 40, "bold")
    c.text((60, 95), "माल रसीद / LORRY RECEIPT", 30, "bold")
    c.label_value(1100, 50, "L.R. No. / बिल्टी नं.:", t["lr_number"], "lr_number")
    c.label_value(1100, 95, "दिनांक / Date:", date.fromisoformat(t["lr_date"]).strftime("%d-%m-%Y"), "lr_date")
    c.label_value(60, 150, "GSTIN:", t["transporter_gstin"], "transporter_gstin")
    c.label_value(1100, 150, "गाड़ी नं. / Vehicle:", t["vehicle_number"], "vehicle_number")
    c.label_value(60, 210, "From / से:", t["from_location"], "from_location")
    c.label_value(600, 210, "To / तक:", t["to_location"], "to_location")
    c.label_value(60, 270, "प्रेषक / Consignor:", t["consignor_name"], "consignor_name")
    c.label_value(60, 310, "GSTIN:", t["consignor_gstin"], "consignor_gstin")
    c.label_value(60, 370, "प्रेषिती / Consignee:", t["consignee_name"], "consignee_name")
    c.label_value(60, 410, "GSTIN:", t["consignee_gstin"], "consignee_gstin")
    c.label_value(60, 470, "Invoice No.:", t["invoice_reference"], "invoice_reference")
    c.line([(40, 530), (1614, 530)])
    y = 560
    rows = [("नग / Packages", "packages_count", str(t["packages_count"])),
            ("असली वजन / Actual Wt (kg)", "actual_weight_kg", f"{t['actual_weight_kg']:g}"),
            ("चार्ज वजन / Charged Wt (kg)", "charged_weight_kg", f"{t['charged_weight_kg']:g}"),
            ("दर / Rate", "freight_rate", f"{t['freight_rate']:g}"),
            ("भाड़ा / Freight", "freight_amount", inr(t["freight_amount"]))]
    if t["other_charges"]:
        rows.append(("हमाली / Other", "other_charges", inr(t["other_charges"])))
    rows.append(("कुल / Total", "total_freight", inr(t["total_freight"])))
    for label, key, v in rows:
        c.text((60, y), label, 26, "bold")
        c.text((560, y), v, 28, "mono", key=key, fill=(25, 25, 110))
        y += 50
    c.label_value(1000, 560, "Freight:", t["payment_mode"], "payment_mode")
    c.text((1150, 1050), "For " + t["transporter_name"], 22, "bold")
    return np.asarray(c.img), c.boxes, c.raw


# --- degradations --------------------------------------------------------------------------------

def degrade(img: np.ndarray, kind: str, rng: random.Random) -> np.ndarray:
    out = img.copy()
    if kind == "good_scan":
        out = _rotate(out, rng.uniform(-0.8, 0.8))
        out = _noise(out, 3, rng)
        return _jpeg(out, 88)
    if kind == "poor_scan":
        out = _rotate(out, rng.uniform(-4, 4))
        out = cv2.GaussianBlur(out, (0, 0), rng.uniform(1.2, 1.8))
        h, w = out.shape[:2]
        out = cv2.resize(out, (int(w * 0.55), int(h * 0.55)), interpolation=cv2.INTER_AREA)
        out = _fade(out, 0.55)
        out = _noise(out, 9, rng)
        return _jpeg(out, 55)
    if kind == "carbon":
        out = _fade(out, 0.35)
        tint = np.array([200, 205, 255], dtype=np.float32) / 255
        out = np.clip(out.astype(np.float32) * tint + 30, 0, 255).astype(np.uint8)
        return _jpeg(_noise(out, 6, rng), 70)
    if kind == "unreadable":
        out = cv2.GaussianBlur(out, (0, 0), 9)
        return _fade(out, 0.1)
    return out


def _rotate(img, angle):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderValue=(250, 250, 250))


def _noise(img, sigma, rng):
    n = np.random.default_rng(rng.randint(0, 10**6)).normal(0, sigma, img.shape)
    return np.clip(img.astype(np.float32) + n, 0, 255).astype(np.uint8)


def _fade(img, strength):
    return np.clip(255 - (255 - img.astype(np.float32)) * strength, 0, 255).astype(np.uint8)


def _jpeg(img, q):
    ok, enc = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.cvtColor(cv2.imdecode(enc, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def to_jpeg_bytes(img: np.ndarray, q: int = 92) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=q, dpi=(200, 200))
    return buf.getvalue()


def make_invoice(rng: random.Random, bucket: str = "good_scan", n_lines: int = 3) -> SynthDoc:
    truth, lines = invoice_truth(rng, n_lines)
    if bucket == "digital":
        return SynthDoc("invoice", truth, lines, pdf=render_invoice_pdf(truth, lines), bucket=bucket)
    img, boxes, raw = render_invoice(truth, lines, hindi_labels=(bucket == "bilingual"))
    img = degrade(img, {"bilingual": "good_scan"}.get(bucket, bucket), rng)
    return SynthDoc("invoice", truth, lines, boxes, raw, image=img, bucket=bucket)


def make_lr(rng: random.Random, bucket: str = "carbon") -> SynthDoc:
    t = lr_truth(rng)
    img, boxes, raw = render_lr(t)
    return SynthDoc("lr", t, [], boxes, raw, image=degrade(img, bucket, rng), bucket=bucket)


def write_dataset(out: Path, n: int = 4, seed: int = 7) -> list[Path]:
    """Writes <bucket>/<name>.(jpg|pdf) + <name>.truth.json in the eval-harness format."""
    rng = random.Random(seed)
    paths = []
    plan = [("invoice", b) for b in ("digital", "good_scan", "poor_scan", "bilingual")] + [("lr", "carbon")]
    for doc_type, bucket in plan:
        for i in range(n):
            sd = make_invoice(rng, bucket) if doc_type == "invoice" else make_lr(rng, bucket)
            d = out / (bucket if doc_type == "invoice" else "bilingual_lr")
            d.mkdir(parents=True, exist_ok=True)
            name = f"{doc_type}_{bucket}_{i:03d}"
            f = d / (name + (".pdf" if sd.pdf else ".jpg"))
            f.write_bytes(sd.pdf if sd.pdf else to_jpeg_bytes(sd.image))
            (d / f"{name}.truth.json").write_text(json.dumps(
                {"doc_type": doc_type, "bucket": bucket, "synthetic": True, "fields": sd.truth, "line_items": sd.lines},
                indent=1, ensure_ascii=False))
            paths.append(f)
    return paths
