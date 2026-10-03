# Architecture decisions

## Why these choices

| Decision | Choice | Reason |
|---|---|---|
| Extraction engine | Claude vision, strict JSON schema (`output_config.format`), per doc type; the `LLMClient` protocol keeps a second provider (Gemini, in `gemini_llm.py`) a config flag away | No free-text parsing; schema violations impossible; not locked to one vendor |
| Call budget | One page reading per document; deterministic proof; one zoomed re-read of only the unproven fields; no verifier call | See "Call budget" below. Each extra call must buy accuracy that nothing cheaper can |
| Model routing | Sonnet 5.5 (medium effort) for clean pages; Opus 5.5 (high effort) for poor scans, handwriting, Indic scripts, LRs; the re-read always uses the other model | Two structurally different readers decorrelate errors; strong model only where the page is hard |
| Refusal handling | Server-side fallback (`fallbacks: "default"`); refusals route the doc to manual entry | A declined request never becomes a silent gap |
| Prompt caching | Field guide + instructions sent as one cached system block (~1.4k tokens, above the 512-token minimum) | Repeat documents pay 5-10% of the system-prompt input price |
| Schema shape | Union-free: all values strings, parsed by our own Indian-format normaliser | Compiler limit is 16 union params; also keeps lakh grouping, Devanagari digits, day-first dates in tested code |
| Second reading | Only fields nothing proved; zoomed, ink-enhanced crops; blind (no label, no earlier answer); different model | Re-reading proven values buys nothing; a reader that sees the first answer is anchored |
| Arithmetic | Recomputed deterministically, tight tolerance (₹1 + ₹2/crore) | The model never grades its own maths; a 0.1% tolerance was found to hide ₹50 tax misreads |
| Confidence | Log-odds over evidence features, not model self-report; features persisted per field | The model's own confidence does not separate right from wrong extractions |
| Auto-accept threshold | Certified, not tuned: fit/calibration split by document, spot-check labels only, Learn-then-Test fixed sequence, exact binomial tails, design effect for fields on one document, separate stricter α for high-stakes fields | "At most 1% of auto-accepted fields wrong (0.3% for totals and tax), with 95% confidence" becomes a checkable claim |
| Spot checks | 5% of **all** documents, every field confirmed (enforced on completion) | A uniform sample is the only unbiased label source for every threshold; flagged-only labels are biased |
| Whole-document penalty | Any failed arithmetic/compliance check multiplies every field score by 0.8 (bands drop), but routing only pulls in fields implicated in the failed check | Honours "a mismatch lowers trust in the whole document" without making the reviewer re-key it |
| Unreadable gate | Decided on original pixels (contrast-normalised sharpness, ink contrast, glyph height, ink ratio), before any model call | Enhancement can make a page look sharper without adding information |
| Frontend | Vanilla JS modules, no build, no framework, strict CSP | Instant load in a conference room; nothing to break; XSS-safe by construction |
| Queue | Postgres table with `SKIP LOCKED` | No Redis/Celery to operate during pilots |
| Machine-readable codes | Decode GST e-invoice QR (NIC-signed JWT), UPI QR and barcodes locally (zxing-cpp, OpenCV fallback, 1-3x scales) before any model call | Exact values at zero model error; a signed QR that disagrees with the print is a misread or an edited page. Codes fill only absent/illegible fields; they never silently overwrite a legible print |
| Arithmetic repair | When checks fail, pick between the page reading, the zoomed reading and the QR; apply a fix only if it is the unique value that makes every check pass AND a reading saw it; digit-slip-only fixes are suggestions | The maths decides between readings without "inventing" a value |
| Company records | Own GSTINs (admin-set or learned from 3 reviewed invoices), vendor profile (name, bank, invoice-number shape), keyed-hash duplicate key, auto-template after 3 reviewed docs | Prior knowledge is the cheapest accuracy; bank-change and duplicate alerts stop payment fraud |
| Document type | Free signals first: the user's choice (remembered per browser), a signed e-invoice QR, a keyword vote on the PDF text, the company's dominant type (>= 20 docs, >= 85%); triage call only otherwise | Most uploads need no classification call; a wrong guess costs one repeated reading, caught because the page reading reports what it sees |
| Batch files | Digital PDFs split by document number in the text layer; scans by triage; a multi-document file becomes child documents | Scanners batch 20 invoices into one PDF |
| No third-party IDP platform | Thin custom pipeline | Full IP ownership, no AGPL exposure for white-label/resale |

## Call budget

| Document | AI calls | Why |
|---|---|---|
| Digital PDF, type known or named in its text | 1 | The text layer and arithmetic prove nearly every value |
| GST e-invoice (photo or scan) | 1 | The signed QR names the type and proves GSTINs, number, date, total |
| Scan of a known type, everything adds up | 1-2 | Re-read only what arithmetic, QR and records could not prove |
| Unknown-type scan on a new account, or multi-page scan | 2-3 | Plus one cheap triage call at 1000px |

Research behind it:

- Cascades: cheap checks before expensive model calls (FrugalGPT, Chen et al., 2023).
- Selective classification: accept only where risk is provably low (Geifman & El-Yaniv, 2017).
- Risk control with a guarantee: Learn then Test (Angelopoulos et al., 2021); per-field pitfalls
  for document extraction (document clustering, refit leakage, ties: arXiv 2608.14639).
- Two structurally different readings as the disagreement signal; model confidence, log-probs and
  self-consistency fail to separate right from wrong, and schema-guided readers invent rather
  than omit (ExtractConf, arXiv 2606.24420; Xiong et al., ICLR 2024).
- Zoomed crops beat re-reading the full page for small text (MLLMs Know Where to Look, ICLR 2025).
- Plain repeated sampling adds little on strong models (arXiv 2511.00751), so no voting rounds.

## Data model (Postgres)

`tenants` · `users` · `auth_sessions` · `documents` (metadata only, no raw bytes) · `pages` (quality
signals + corrections applied) · `extraction_runs` (every model call: model, prompt version, tokens,
latency, encrypted output) · `extracted_fields` (value, raw text, location, score, band, routing
reason, feature vector, human outcome) · `validation_results` · `corrections` (who, what, when, time
on field) · `audit_events` (hash chain) · `jobs` · `templates` · `vendor_field_stats` ·
`match_sets` · `threshold_configs` (versioned, with evidence) · `vendor_profiles` (keyed by pseudonymised
GSTIN: name, bank account, invoice-number shapes). New nullable columns are added automatically on
start-up (`db._add_missing_columns`); destructive changes go through migrations.

## Known limits / next engineering steps

1. Real-document eval not yet run with a key (see EVALUATION.md). Expect the scorer weights to move.
2. E-invoice QR signatures are verified only when NIC public keys are configured
   (`SERENO_EINVOICE_PUBLIC_KEYS_PEM`); otherwise the QR is trusted as data, not as proof.
3. Marathi amount-in-words is not parsed (Hindi is). Tamil/Telugu/Gujarati words: not yet.
4. The zoomed re-read covers every unproven field on hard documents; measure its catch rate per
   bucket on the eval set and narrow it where it adds nothing.
6. Threshold certificates need volume: about 600 spot-checked fields per group before any
   threshold can be certified at α = 1%. Until then the default thresholds stay.
5. ERP export profiles are configurable per tenant (column names, date format); a native
   connector only after the first customer confirms their ERP.
