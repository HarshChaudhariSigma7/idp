"""Ground truth for real web-sourced documents. Labelled visually by Claude on 2026-09-27; needs a
second human pass before numbers are quoted anywhere. Images are third-party: internal testing only,
never committed to git or shown to customers."""
import json, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2] / "eval_data" / "web"
RAW = ROOT / "raw"
OUT = ROOT / "labelled"
L = lambda desc, **k: {"description": desc, **k}
DOCS = {
 "inv_gogstbill_a5.jpg": dict(doc_type="invoice", bucket="printed", tags=["template_render"], fields=dict(
    invoice_number="GST-3525-26", invoice_date="2025-07-23", supplier_name="Gujarat Freight Tools",
    supplier_gstin="27CORPP3939N1ZQ", buyer_name="Shiv Engineering", buyer_gstin="32AABBA7890B1ZB",
    place_of_supply="Kerala (32)", eway_bill_number="78456378", subtotal=3805.00, igst_amount=684.90,
    grand_total=4490.00, bank_ifsc="ICIC045F"),
    line_items=[L("Bosch All-in-One Metal Hand Tool Kit", hsn_sac="8302", quantity=1, unit="NOS", rate=2535.00, taxable_value=2535.00, gst_rate=18, igst_amount=456.30, line_total=2991.30),
                L("Taparia Universal Tool Kit", hsn_sac="8302", quantity=1, unit="NOS", rate=1270.00, taxable_value=1270.00, gst_rate=18, igst_amount=228.60, line_total=1498.60)]),
 "inv_surya_a4.jpg": dict(doc_type="invoice", bucket="printed", tags=["template_render", "internally_inconsistent_discount"], fields=dict(
    invoice_number="GM-2026-204", invoice_date="2026-02-06", supplier_name="GreenMart Traders", supplier_gstin="07AAAAA0000A1Z5",
    buyer_gstin="07BBBBB1111B1Z1", place_of_supply="Delhi", vehicle_number="DL01AB1234", subtotal=1135.00, discount_total=35.00,
    cgst_amount=65.75, sgst_amount=65.75, round_off=0.50, grand_total=1267.00, bank_ifsc="SBIN0000123", bank_account_number="123456789012"),
    line_items=[L("Toor Dal 1kg", hsn_sac="0713", rate=180, taxable_value=350, discount=10, gst_rate=5, cgst_amount=8.75, sgst_amount=8.75, line_total=367.50),
                L("Hair Oil 500ml", hsn_sac="3305", rate=320, taxable_value=300, discount=20, gst_rate=18, cgst_amount=27.00, sgst_amount=27.00, line_total=354.00),
                L("Turmeric Powder 200g", hsn_sac="0910", rate=70, taxable_value=210, discount=0, gst_rate=5, cgst_amount=5.25, sgst_amount=5.25, line_total=220.50),
                L("Soft Drink 1L", hsn_sac="2202", rate=140, taxable_value=275, discount=5, gst_rate=18, cgst_amount=24.75, sgst_amount=24.75, line_total=324.50)]),
 "inv_billingsoftware.webp": dict(doc_type="invoice", bucket="printed", tags=["template_render", "invalid_gstin_printed"], fields=dict(
    invoice_number="X33", invoice_date="2018-02-21", due_date="2018-03-03", po_reference="02", po_date="2018-01-24",
    supplier_name="Sorina TEST 123", supplier_gstin="123456711111111", buyer_name="Ab Company", buyer_gstin="09AAMFC0376K1Z4",
    igst_amount=3224.40, cess_amount=218.50, discount_total=1730.00, round_off=0.10, grand_total=27425.00),
    must_review=["supplier_gstin"]),
 "inv_sleekbill.png": dict(doc_type="invoice", bucket="printed", tags=["template_render", "receipt_layout"], fields=dict(
    invoice_number="IN-15", invoice_date="2025-01-23", supplier_name="SLEEK BILL", supplier_gstin="27AAFCV2449G1Z7",
    subtotal=900.00, igst_amount=68.00, grand_total=968.00),
    line_items=[L("Orange Powder", quantity=1, rate=400, line_total=448), L("Walnuts 5% Tax Item", quantity=1, rate=100, line_total=105),
                L("Coin 3% Tax Item", quantity=1, rate=100, line_total=103), L("Rose Water", quantity=1, rate=150, line_total=150),
                L("Glicerene", quantity=1, rate=50, line_total=50), L("Cheese 12% Tax Item", quantity=1, rate=100, line_total=112)]),
 "inv_tally_einvoice.jpg": dict(doc_type="invoice", bucket="printed", tags=["tally_export", "e_invoice"], fields=dict(
    invoice_number="SHB/456/20", invoice_date="2020-12-20", supplier_name="Surabhi Hardwares, Bangalore", supplier_gstin="29AACCT3705E000",
    buyer_name="Kiran Enterprises", buyer_gstin="29AAFFC8126N1ZZ",
    irn="fef1df90406b928db26a62f816debc9bb5256d9375e60dc4226653cc23a8c595",
    subtotal=3500.00, cgst_amount=315.00, sgst_amount=315.00, grand_total=4130.00),
    line_items=[L("12MM**", hsn_sac="1005", quantity=7, rate=500.00, taxable_value=3500.00)], must_review=["supplier_gstin"]),
 "inv_gstzen.png": dict(doc_type="invoice", bucket="printed", tags=["template_render", "old_date"], fields=dict(
    invoice_number="17-18/JH/97", invoice_date="2017-07-26", supplier_name="CloudZen Software Labs Pvt. Ltd.", supplier_gstin="20QXOCC9424D1Z5",
    buyer_name="Cipla Ltd", buyer_gstin="08AKOCX6349P1ZL", subtotal=38991.00, igst_amount=8933.68, round_off=0.32, grand_total=47925.00),
    line_items=[L("OTHR BLCHD WOVN FBRCS WGHNG >200 G/M2", hsn_sac="521222", quantity=9, unit="GMS", rate=1344, taxable_value=12096, gst_rate=18, igst_amount=2177.28, line_total=14273.28),
                L("OTER BEANS DRIED & SHLD", hsn_sac="071339", quantity=7, unit="UNT", rate=1106, taxable_value=7742, gst_rate=18, igst_amount=1393.56, line_total=9135.56),
                L("GLAZIERS & GRAFTING PUTY, RESIN ELEMNTS NON RFRCTRY SRFCNG PRPN FR FLOORS, WALL ETC", hsn_sac="321490", quantity=7, unit="CCM", rate=1335, taxable_value=9345, gst_rate=28, igst_amount=2616.60, line_total=11961.60),
                L("OTHER, FRESH OR CHILLED", hsn_sac="020735", quantity=7, unit="BTL", rate=1081, taxable_value=7567, gst_rate=28, igst_amount=2118.76, line_total=9685.76),
                L("OTHER COSMETIC & TOILT PRPN N E S", hsn_sac="33079090", quantity=3, unit="MLT", rate=747, taxable_value=2241, gst_rate=28, igst_amount=627.48, line_total=2868.48)]),
 "inv_sleekbill_einv.png": dict(doc_type="invoice", bucket="printed", tags=["e_invoice", "high_res"], fields=dict(
    invoice_number="CB-253", invoice_date="2023-11-18", supplier_name="TAMILNADU MAX ENTERPRISES", supplier_gstin="33ETOBX6699S3Z3",
    buyer_name="CHENNAI AIR PRODUCTS", buyer_gstin="33NSVBX9968F4Z0", place_of_supply="TAMIL NADU",
    subtotal=87420.00, cgst_amount=7867.80, sgst_amount=7867.80, round_off=0.40, grand_total=103156.00),
    line_items=[L("R134a - Value- 2 cylinders x 62 kgs", hsn_sac="29034500", quantity=124, unit="KGS", rate=380, taxable_value=47120, gst_rate=18, line_total=55601.60),
                L("R404A - Hiflon - 2 cylinders x 45 kgs(above all in our returnable cylinders)", hsn_sac="38276100", quantity=90, unit="KGS", rate=440, taxable_value=39600, gst_rate=18, line_total=46728.00),
                L("Shipping and Packing Charges - Delivery Charges", hsn_sac="996511", quantity=1, unit="OTH", rate=700, taxable_value=700, gst_rate=18, line_total=826.00)]),
 "inv_scribd_gst.jpg": dict(doc_type="invoice", bucket="printed", tags=["mostly_blank", "invalid_gstin_printed"], fields=dict(
    invoice_number="0029", invoice_date="2018-03-04", supplier_name="AJAY VERMA & Sons", supplier_gstin="07AAFD8457JU3",
    buyer_gstin="07AAFD8457JU3"), must_review=["supplier_gstin", "grand_total"]),
 "inv_vyapar.webp": dict(doc_type="invoice", bucket="printed", tags=["blank_template"], fields={}, must_review=["invoice_number", "grand_total"]),
 "lr_vyapar_transport.webp": dict(doc_type="invoice", bucket="printed", tags=["blank_template"], fields={}, must_review=["invoice_number", "grand_total"]),
 "lr_busy_transport.webp": dict(doc_type="invoice", bucket="printed", tags=["template_placeholders"], fields=dict(
    invoice_number="0004/25-26", invoice_date="2025-07-13", due_date="2025-07-28", grand_total=200.00),
    must_review=["supplier_gstin"]),
 "lr_scribd_102.jpg": dict(doc_type="lr", bucket="printed", tags=["computer_bilty"], fields=dict(
    lr_number="102", lr_date="2025-04-12", transporter_name="BALAJI ROADLINES", transporter_gstin="24CLYPP9882L2ZP",
    vehicle_number="RJ27GF0315", freight_amount=0, total_freight=0, gst_paid_by="Consignee")),
 "lr_scribd_mohan.jpg": dict(doc_type="lr", bucket="printed", tags=["computer_lr"], fields=dict(
    lr_number="238", lr_date="2024-03-14", transporter_gstin="29AECPU3917N1ZU", vehicle_number="KA32AA0039",
    from_location="ADONI", to_location="HUMNABAD", consignor_name="SAKARE SREENIVASULU", total_freight=27000, payment_mode="PAID")),
 "lr_lorryto_manual.png": dict(doc_type="lr", bucket="handwritten", tags=["photo", "handwritten_block_caps"], fields=dict(
    lr_number="7523", lr_date="2024-05-24", vehicle_number="MH12AB4521", from_location="MUMBAI", to_location="NAGPUR",
    consignor_name="SHREE ENTERPRISES", consignee_name="GUPTA TRADERS", packages_count=15,
    goods_description="INDUSTRIAL MACHINERY PARTS", actual_weight_kg=2750, freight_amount=18500, total_freight=18500, payment_mode="TO PAY")),
 "lr_scribd_household.jpg": dict(doc_type="lr", bucket="poor_scan", tags=["rotated_90", "scan", "stamped_entries"], fields=dict(
    lr_number="277", lr_date="2021-03-31", transporter_gstin="07EZEPS3404R1ZR", from_location="Bhubaneshwar", to_location="Lakhanpur")),
 "lr_scribd_transport.jpg": dict(doc_type="lr", bucket="handwritten", tags=["redacted", "scan", "partial"], fields=dict(
    lr_number="156", lr_date="2024-07-13", transporter_gstin="07CEEPS6421M2Z2", from_location="Pune", to_location="Goa"),
    must_review=["total_freight", "consignor_name"]),
 "lr_linkedin.jpg": dict(doc_type="lr", bucket="handwritten", tags=["carbon_copy", "redacted", "dense_form"], fields=dict(
    lr_date="2025-03-31", packages_count=25, total_freight=4500), must_review=["consignor_gstin", "consignee_gstin"]),
 "lr_justdial_rajdhani.jpg": dict(doc_type="other", bucket="poor_scan", tags=["blank_voucher", "photo", "not_an_lr"], fields={}),
 "hw_localcircles_bill.jpeg": dict(doc_type="invoice", bucket="handwritten", tags=["photo", "crumpled", "overwritten_total"], fields=dict(
    invoice_number="085", invoice_date="2019-07-14", supplier_name="Kabliwala's", grand_total=1025.00),
    line_items=[L("Kaju Diamond", quantity=1, rate=375, line_total=375), L("Roasted Kaju", quantity=1, rate=475, line_total=475),
                L("Chana B/Chilka", quantity=1, rate=175, line_total=175)]),
 "hw_scribd_fatehabad.jpg": dict(doc_type="invoice", bucket="vernacular", tags=["hindi_print", "handwritten", "photo", "pink_paper"], fields=dict(
    invoice_number="1466", invoice_date="2024-01-25", supplier_name="गुलशन बुक डिपो", grand_total=480.00),
    line_items=[L(None, quantity=10, rate=40, line_total=400), L(None, quantity=1, rate=80, line_total=80)]),
 "hw_tripadvisor_memo.jpg": dict(doc_type="invoice", bucket="vernacular", tags=["hindi_header", "handwritten", "very_low_res"], fields=dict(
    grand_total=260.00)),
 "hw_archive_cashmemo_1960.jpg": dict(doc_type="invoice", bucket="vernacular", tags=["devanagari_numerals_handwritten", "aged_paper", "nepali"], fields=dict(
    supplier_name="Dhakhwa House"), must_review=["grand_total", "invoice_date"]),
 "hw_alamy_1941.jpg": dict(doc_type="invoice", bucket="handwritten", tags=["cursive", "watermark", "rupee_anna_currency"], fields=dict(
    supplier_name="Milton & Co."), must_review=["grand_total"]),
 "hw_motorcycle_receipt.jpg": dict(doc_type="invoice", bucket="handwritten", tags=["photo", "perspective", "low_light", "stamp"], fields=dict(
    supplier_name="KUSUM SALES CORPORATION", grand_total=49152.00)),
}
DOCS["inv_surya_a5.jpg"] = {**DOCS["inv_surya_a4.jpg"], "tags": ["template_render", "a5_layout", "internally_inconsistent_discount"]}

if OUT.exists():
    shutil.rmtree(OUT)
for name, t in DOCS.items():
    d = OUT / t["bucket"]
    d.mkdir(parents=True, exist_ok=True)
    stem = name.rsplit(".", 1)[0]
    shutil.copy(RAW / name, d / name)
    lines = [{k: v for k, v in li.items() if v is not None} for li in t.get("line_items", [])]
    (d / f"{stem}.truth.json").write_text(json.dumps({
        "doc_type": t["doc_type"], "bucket": t["bucket"], "synthetic": False, "source": "web",
        "labelled_by": "claude-visual-2026-09-27 (needs second human pass)", "tags": t["tags"],
        "fields": t["fields"], "line_items": lines, "must_review": t.get("must_review", [])}, indent=1, ensure_ascii=False))
print(len(DOCS), "labelled")
