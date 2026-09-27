# Evaluation protocol (gate before any customer demo)

Rule: **no document type × quality bucket is shown to a customer until it passes this gate on real
documents.** Synthetic documents never count. If Hindi or handwriting accuracy is lower, say so in
the demo and show the confidence routing catching it.

## 1. Collect ≥100 real documents (target 150)

| Bucket | Min | Source ideas | Notes |
|---|---|---|---|
| `digital` | 20 | Tally / SAP B1 exported invoice PDFs | text layer present |
| `good_scan` | 20 | office scanner, 200–300 DPI | printed, straight |
| `poor_scan` | 20 | WhatsApp-forwarded photos, 100 DPI faxes, skewed | include folds, shadows, stamps over totals |
| `handwritten` | 20 | kachcha bills, handwritten LRs, hand-filled qty/amounts on printed forms | |
| `bilingual` | 20 | LRs / challans / bilty with Hindi labels or Hindi entries, Devanagari numerals | **specific bilingual LR set required** |

Get them from pilot prospects under NDA, from Sereno's own purchases, or from transporters directly.
Redact nothing on the image (it changes difficulty); store under `eval_data/` (git-ignored, encrypted disk).

## 1b. Starter stress set (public web)

`bash datasets/web_v1/fetch.sh` downloads 25 real public documents with labels. Run
`python -m sereno.eval.offline_checks eval_data/web/labelled` (no key) and
`python -m sereno.eval.run_eval eval_data/web/labelled --out reports/web_v1` (with key).
Offline checks also decode every QR/barcode and compare it with the labels: on the current set
the Tally e-invoice QR matches its print 7/7, UPI amounts match, and one public e-invoice sample
has printed GSTINs that differ from its signed QR (an edited sample: exactly the tamper case the
product flags). It is a regression set, not a benchmark.

## 2. Label

For each file `eval_data/real/<bucket>/<name>.pdf|jpg`, create `<name>.truth.json`:

```json
{"doc_type": "invoice", "bucket": "poor_scan", "synthetic": false,
 "fields": {"invoice_number": "TI/25-26/0412", "invoice_date": "2025-06-05", "supplier_gstin": "27AAPFU0939F1ZV",
            "subtotal": 33159.0, "cgst_amount": 2984.31, "sgst_amount": 2984.31, "grand_total": 39128.0, "...": "..."},
 "line_items": [{"description": "MS Hex Bolt M12x50", "quantity": 1000, "rate": 12.0, "taxable_value": 12000.0,
                 "gst_rate": 18, "...": "..."}]}
```

Two people label independently; a third resolves disagreements. Only fields that exist on the page
are labelled; missing fields are omitted (the harness counts a hallucinated value as an error).
Labelling cost: ~6–10 min per document → ~2 person-days for 150 documents.

## 3. Run

```bash
export ANTHROPIC_API_KEY=...            # real model calls
python -m sereno.eval.run_eval eval_data/real --out reports
```

Output: `reports/eval_<ts>.md|json` and `reports/eval_latest.json` (read by the internal metrics
page to show which cells are demo-safe).

## 4. Metrics (per doc type × bucket)

| Metric | Meaning | Why |
|---|---|---|
| field accuracy | extracted value == truth | headline, but incomplete |
| **escape rate** | wrong AND auto-accepted ÷ auto-accepted | the number that hurts customers; must be ~0 |
| error catch rate | wrong AND flagged ÷ wrong | how well routing catches mistakes |
| auto-accept rate | fields not sent to review | drives reviewer cost |
| straight-through docs | docs needing no review | the CFO number |
| unreadable/failed | refused by the quality gate | should match human judgement |
| mean seconds / cost | latency and model cost per doc | unit economics |
| code agreement | printed fields that match the e-invoice QR | free ground truth on every e-invoice |
| repair precision | automatic repairs that a reviewer kept | must stay ~100% or repair turns to suggest-only |

## 5. Gate (in `sereno/eval/run_eval.py::GATE`)

A cell is **demo-safe** only if: ≥20 real docs, escape rate ≤0.5%, field accuracy ≥97%.
Tighten, never loosen, without a written reason.

## 6. After the gate

- Re-run on every prompt/model change (`PROMPT_VERSION` is logged per extraction).
- Production QA spot checks (5% of auto-approved docs by default, `SERENO_QA_SAMPLE_RATE`) keep
  measuring the escape rate on live traffic; the weekly backtest uses them to set thresholds.
- Report accuracy to customers per bucket, with sample sizes. Never a single blended number.
