# Architecture decisions

## Why these choices

| Decision | Choice | Reason |
|---|---|---|
| Extraction engine | Gemini vision, strict JSON schema (`response_json_schema`), per doc type; the `LLMClient` protocol keeps a second provider (Claude) a small adapter away | No free-text parsing; schema violations impossible; not locked to one vendor |
| Model routing | Gemini Flash only for clean English digital PDFs; Gemini Pro otherwise; auto-escalation to Pro on any disagreement or failed check | Accuracy first; cheap model only where it provably reconciles |
| Refusal handling | A safety/refusal finish reason routes the doc to manual entry instead of failing silently (Claude backend also gets a server-side fallback beta) | A declined request never becomes a silent gap |
| Schema shape | Union-free: all values strings, parsed by our own Indian-format normaliser | Compiler limit is 16 union params; also keeps lakh grouping, Devanagari digits, day-first dates in tested code |
| Self-consistency | Pass B re-reads **every** field with a different prompt on the **original** pixels as zoomed strips; verifier on disagreements | Decorrelates errors (prompt, preprocessing, resolution); agreement becomes evidence for every field so reviewers only see what's uncertain |
| Arithmetic | Recomputed deterministically, tight tolerance (₹1 + ₹2/crore) | The model never grades its own maths; a 0.1% tolerance was found to hide ₹50 tax misreads |
| Confidence | Log-odds over evidence features, not model self-report; features persisted per field; weekly refit | Calibration comes from reviewer outcomes, not from the model |
| Whole-document penalty | Any failed arithmetic/compliance check multiplies every field score by 0.8 (bands drop), but routing only pulls in fields implicated in the failed check | Honours "a mismatch lowers trust in the whole document" without making the reviewer re-key it |
| Unreadable gate | Decided on original pixels (contrast-normalised sharpness, ink contrast, glyph height, ink ratio), before any model call | Enhancement can make a page look sharper without adding information |
| Frontend | Vanilla JS modules, no build, no framework, strict CSP | Instant load in a conference room; nothing to break; XSS-safe by construction |
| Queue | Postgres table with `SKIP LOCKED` | No Redis/Celery to operate during pilots |
| Machine-readable codes | Decode GST e-invoice QR (NIC-signed JWT), UPI QR and barcodes locally (zxing-cpp, OpenCV fallback, 1-3x scales) before any model call | Exact values at zero model error; a signed QR that disagrees with the print is a misread or an edited page. Codes fill only absent/illegible fields; they never silently overwrite a legible print |
| Third reading | Blind read of each field's zoomed crop (ink-enhanced, no label, no earlier answer) on hard docs and disagreements; majority vote, verifier only on 1/1/1 ties | A verifier that sees both candidates is anchored; a blind third read is an independent vote |
| Arithmetic repair | When checks fail, re-read the implicated figures; apply a fix only if it is the unique value that makes every check pass AND another reading saw it; digit-slip-only fixes are suggestions | Correlated misreads (both passes wrong the same way) are caught without the maths "inventing" a value |
| Company records | Own GSTINs (admin-set or learned from 3 reviewed invoices), vendor profile (name, bank, invoice-number shape), keyed-hash duplicate key, auto-template after 3 reviewed docs | Prior knowledge is the cheapest accuracy; bank-change and duplicate alerts stop payment fraud |
| Batch files | Triage assigns each page a document index; a multi-document file becomes child documents | Scanners batch 20 invoices into one PDF |
| No third-party IDP platform | Thin custom pipeline | Full IP ownership, no AGPL exposure for white-label/resale |

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
4. The blind third read costs one extra call per hard document; measure its catch rate on the
   eval set and drop it for buckets where it adds nothing.
5. ERP export profiles are configurable per tenant (column names, date format); a native
   connector only after the first customer confirms their ERP.
