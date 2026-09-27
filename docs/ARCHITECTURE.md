# Architecture decisions

## Why these choices

| Decision | Choice | Reason |
|---|---|---|
| Extraction engine | Claude vision, strict JSON schema (`output_config.format`), per doc type | No free-text parsing; schema violations impossible |
| Model routing | Sonnet only for clean English digital PDFs; Opus otherwise; auto-escalation to Opus on any disagreement or failed check | Accuracy first; cheap model only where it provably reconciles |
| Refusal handling | Server-side fallback (`fallbacks: "default"`) on Opus; refusals route the doc to manual entry | A declined request never becomes a silent gap |
| Schema shape | Union-free: all values strings, parsed by our own Indian-format normaliser | Compiler limit is 16 union params; also keeps lakh grouping, Devanagari digits, day-first dates in tested code |
| Self-consistency | Pass B re-reads **every** field with a different prompt on the **original** pixels as zoomed strips; verifier on disagreements | Decorrelates errors (prompt, preprocessing, resolution); agreement becomes evidence for every field so reviewers only see what's uncertain |
| Arithmetic | Recomputed deterministically, tight tolerance (₹1 + ₹2/crore) | The model never grades its own maths; a 0.1% tolerance was found to hide ₹50 tax misreads |
| Confidence | Log-odds over evidence features, not model self-report; features persisted per field; weekly refit | Calibration comes from reviewer outcomes, not from the model |
| Whole-document penalty | Any failed arithmetic/compliance check multiplies every field score by 0.8 (bands drop), but routing only pulls in fields implicated in the failed check | Honours "a mismatch lowers trust in the whole document" without making the reviewer re-key it |
| Unreadable gate | Decided on original pixels (contrast-normalised sharpness, ink contrast, glyph height, ink ratio), before any model call | Enhancement can make a page look sharper without adding information |
| Frontend | Vanilla JS modules, no build, no framework, strict CSP | Instant load in a conference room; nothing to break; XSS-safe by construction |
| Queue | Postgres table with `SKIP LOCKED` | No Redis/Celery to operate during pilots |
| No third-party IDP platform | Thin custom pipeline | Full IP ownership, no AGPL exposure for white-label/resale |

## Data model (Postgres)

`tenants` · `users` · `auth_sessions` · `documents` (metadata only, no raw bytes) · `pages` (quality
signals + corrections applied) · `extraction_runs` (every model call: model, prompt version, tokens,
latency, encrypted output) · `extracted_fields` (value, raw text, location, score, band, routing
reason, feature vector, human outcome) · `validation_results` · `corrections` (who, what, when, time
on field) · `audit_events` (hash chain) · `jobs` · `templates` · `vendor_field_stats` ·
`match_sets` · `threshold_configs` (versioned, with evidence).

## Known limits / next engineering steps

1. Real-document eval not yet run (see EVALUATION.md). Expect the scorer weights to move.
2. Handwriting-heavy LRs may need a third read on low-agreement fields; decide from eval data.
3. Line-item alignment between passes is positional/greedy; long invoices (50+ lines) need a
   stronger alignment (description similarity) if eval shows row-shift errors.
4. Multi-invoice PDFs (several invoices in one file) are flagged by the model as an anomaly but
   not yet split automatically.
5. Duplicate-invoice detection (same vendor + invoice no. + amount) is a cheap, high-value add.
6. ERP export profiles are configurable per tenant (column names, date format); a native
   connector only after the first customer confirms their ERP.
