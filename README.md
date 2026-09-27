# Sereno Volante IDP

Accuracy-first document extraction for Indian mid-market manufacturers (₹300–5,000 cr).
Invoices, lorry receipts (LR/GR/bilty), POs, GRNs, contracts: clean PDFs, scans, phone photos,
carbon copies, handwriting, English + Hindi. Every field carries evidence-based confidence;
anything uncertain goes to a human, field by field. We never silently guess.

## Status (be honest in every customer conversation)

| Area | State |
|---|---|
| Pipeline, validation, review UI, dashboard, exports, 3-way match, templates, security base | Built, 38 automated tests passing |
| Real web documents (25: printed, handwritten, Hindi, sideways, blank traps) | All non-model stages tested; 5 real-world bugs found and fixed ([datasets/web_v1](datasets/web_v1/README.md)). Model accuracy on them: needs `ANTHROPIC_API_KEY` |
| Accuracy on **real** documents | **Not measured yet.** No labelled real set has been run. Synthetic data proves plumbing only |
| Demo gate | Closed for every document type × quality bucket until `reports/eval_latest.json` says otherwise |
| ERP connectors | CSV/Excel only, by design. Build Tally/SAP B1/other only after the first customer confirms their ERP |

Before any customer demo: run the 100+ real-document protocol in [docs/EVALUATION.md](docs/EVALUATION.md).

## How a document flows

```
upload ─► encrypted temp store (TTL) ─► pre-check: DPI, skew, blur, contrast, ink, text layer
      ─► auto-correct: rotate, deskew, upscale, denoise, contrast, sharpen (logged per page)
      ─► unreadable gate (on ORIGINAL pixels) ─► human with plain reason, no extraction attempt
      ─► triage (type, rotation, languages, handwriting, issuer GSTIN → vendor template)
      ─► model routing: Sonnet only for clean English digital PDFs, Opus for everything else,
         auto-escalate to Opus if anything disagrees or fails to reconcile
      ─► pass A: full strict-JSON schema, enhanced images (+ PDF text layer), with field locations
      ─► pass B: independent re-read of every field, different prompt, ORIGINAL pixels as zoomed strips
      ─► verifier on disagreements only (zoomed crops, picks A / B / neither)
      ─► deterministic checks: line math, GST rate reconciliation, CGST=SGST, IGST vs intra-state,
         totals composition, round-off, amount-in-words, GSTIN checksum, formats, dates
      ─► confidence per field = f(agreement, checks, quality, vendor history, text-layer grounding)
      ─► field-level routing → review queue with 4h SLA, or "Ready to export"
      ─► every model call, score, feature vector and human correction logged (the eval set)
```

## Run locally

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]" reportlab
.venv/bin/pytest -q                                   # 33 tests, no API key needed
cp .env.example .env                                  # set ANTHROPIC_API_KEY + SERENO_MASTER_KEY_B64
.venv/bin/python scripts/manage.py create-tenant "Acme Castings Ltd" admin@acme.in "Priya K" no
.venv/bin/uvicorn sereno.api.app:app --port 8000      # http://localhost:8000
```

Offline demo with synthetic data (no API calls, clearly labelled synthetic):
`python scripts/manage.py seed-demo <tenant_id> 12`

Production: `docker compose up` (Postgres + app). Postgres is required in production (job queue
uses `SKIP LOCKED`; audit hash chain uses an advisory lock). SQLite is dev only.

## Operating rhythm

| When | What | Command |
|---|---|---|
| Every upload | Everything logged: model output, features, scores, corrections | automatic |
| Continuous | Retention sweep deletes expired raw documents | automatic (worker) |
| Weekly | Refit weights + thresholds from reviewer outcomes | `python -m sereno.eval.backtest --doc-type invoice [--apply]` |
| Before any demo / prompt change | Labelled eval, per bucket, gate | `python -m sereno.eval.run_eval eval_data/real` |
| Monthly | Audit chain integrity | `python scripts/manage.py verify-audit` |

## Code map

| Path | Purpose |
|---|---|
| `sereno/ingest/` | loading (PDF/image, text layer), quality scoring, auto-correction |
| `sereno/extraction/doc_specs.py` | field specs per doc type → strict JSON schemas (union-free, compiler-safe) |
| `sereno/extraction/{prompts,extractor,llm}.py` | versioned prompts, passes A/B/verify, Claude client |
| `sereno/validation/` | GST/arithmetic/format checks, GSTIN checksum, amount-in-words |
| `sereno/confidence/scorer.py` | evidence-based field confidence + routing rules |
| `sereno/pipeline.py` | orchestration, retention, SLA, assignment |
| `sereno/review.py` | reviewer actions, re-validation, vendor history |
| `sereno/matching/three_way.py` | PO × GRN × invoice reconciliation |
| `sereno/templates_onboarding.py` | "show me 3–5 examples" vendor templates |
| `sereno/security/` | AES-256-GCM envelope crypto, auth + TOTP MFA, RBAC, hash-chained audit |
| `sereno/eval/` | eval harness + demo gate, weekly backtest, synthetic generator |
| `sereno/web/` | the app (no build step) |

Docs: [architecture](docs/ARCHITECTURE.md) · [security & audit controls](docs/SECURITY.md) ·
[subprocessors](docs/SUBPROCESSORS.md) · [evaluation protocol](docs/EVALUATION.md)
