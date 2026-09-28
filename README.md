# Sereno Volante IDP

Accuracy-first document extraction for Indian mid-market manufacturers (₹300–5,000 cr).
Invoices, lorry receipts (LR/GR/bilty), POs, GRNs, contracts: clean PDFs, scans, phone photos,
carbon copies, handwriting, English + Hindi. Every field carries evidence-based confidence;
anything uncertain goes to a human, field by field. We never silently guess.

## Status (be honest in every customer conversation)

| Area | State |
|---|---|
| Pipeline, validation, review UI, dashboard, exports, 3-way match, templates, security base | Built, 63 automated tests passing |
| Accuracy engine: signed e-invoice QR, 3 blind readings + vote, evidence-backed repair, company records, batch split | Built; 17 scenario tests ([tests/test_accuracy_scenarios.py](tests/test_accuracy_scenarios.py)); QR decoding verified on real samples |
| Real web documents (25: printed, handwritten, Hindi, sideways, blank traps) | All non-model stages tested; 5 real-world bugs found and fixed ([datasets/web_v1](datasets/web_v1/README.md)). Model accuracy on them: needs `GEMINI_API_KEY` |
| Accuracy on **real** documents | **Not measured yet.** No labelled real set has been run. Synthetic data proves plumbing only |
| Demo gate | Closed for every document type × quality bucket until `reports/eval_latest.json` says otherwise |
| ERP connectors | CSV/Excel only, by design. Build Tally/SAP B1/other only after the first customer confirms their ERP |

Before any customer demo: run the 100+ real-document protocol in [docs/EVALUATION.md](docs/EVALUATION.md).

## Run it on your Mac (one command)

```bash
brew install python@3.12        # once; macOS ships Python 3.9, this needs 3.11+
git clone https://github.com/HarshChaudhariSigma7/idp.git && cd idp    # private repo: `gh auth login` first
git checkout claude/sereno-volante-doc-extraction-0ci9j0
./run_local.sh
```

First run: installs dependencies (1-3 min), builds a demo company with synthetic documents, prints
the login, opens the two-step QR code and the app at http://localhost:8000. Later runs start in
seconds. Everything stays inside the folder (`./var`), bound to localhost only.

| Log in as | You see |
|---|---|
| `cfo@demo.local` | dashboard, accuracy by condition, exports, 3-way match |
| `reviewer@demo.local` | review queue with 4 documents waiting (keyboard: Enter, E, S, Ctrl+Enter) |
| `admin@demo.local` | users, tamper-evident audit trail, subprocessors |
| `ops@demo.local` | internal metrics and the demo gate |

One password and one authenticator entry work for all four (printed in the terminal, saved in
`var/DEMO_LOGIN.txt`). Demo numbers are simulated and the app says so on every page.

- Read your own documents: put `GEMINI_API_KEY=...` in `.env` (created on first run; get one free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)), restart. `ANTHROPIC_API_KEY` still works if you set `SERENO_LLM_BACKEND=anthropic`.
- Real accuracy on 25 real web documents: `./run_local.sh --eval` (asks first; roughly $3-8 of Gemini API usage).
- Start over: `./run_local.sh --reset`. Other port: `--port 8010`.

## How a document flows

```
upload ─► encrypted temp store (TTL) ─► pre-check: DPI, skew, blur, contrast, ink, text layer
      ─► auto-correct: rotate, deskew, upscale, denoise, contrast, sharpen (logged per page)
      ─► unreadable gate (on ORIGINAL pixels) ─► human with plain reason, no extraction attempt
      ─► triage (type, rotation, languages, handwriting, issuer GSTIN, which pages belong to which
         document) ─► a batch PDF of several invoices/LRs is split into one document each
      ─► codes: GST e-invoice QR (signed JWT: GSTINs, number, date, total, item count, IRN), UPI QR,
         barcodes, decoded locally at 1-3x. Exact data, no model error
      ─► model routing: Gemini Flash only for clean English digital PDFs, Gemini Pro (effort xhigh when hard)
      ─► pass A: full strict-JSON schema, enhanced images (+ PDF text layer), with field locations
      ─► pass B: independent re-read of every field, different prompt, ORIGINAL pixels as zoomed strips
      ─► pass C (hard docs / disagreements): BLIND read of each field's zoomed crop, ink-enhanced,
         no labels or earlier answers ─► majority vote 3/3, 2/3; verifier only for 1/1/1 ties
      ─► deterministic checks: line math (incl. rate per 100/1000), GST reconciliation, totals,
         amount-in-words (English + Hindi), GSTIN checksum, financial year in the invoice number
         vs date, row count vs QR, printed vs signed QR
      ─► repair: when totals fail, re-read the implicated figures; fix only if a single unique value
         makes every check pass AND another reading saw it; otherwise suggest (key S) or flag
      ─► company records: own GSTINs (repair near-misses), vendor name/bank/number format history,
         bank-change fraud alert, duplicate (issuer + number) block, auto-template after 3 reviews
      ─► confidence per field = f(agreement, votes, codes, checks, quality, vendor history)
      ─► field-level routing → review queue with 4h SLA, or "Ready to export"
      ─► every model call, score, feature vector and human correction logged (the eval set)
```

## Run manually (developers)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]" reportlab
.venv/bin/pytest -q                                   # 63 tests, no API key needed
cp .env.example .env                                  # set GEMINI_API_KEY + SERENO_MASTER_KEY_B64
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
| `sereno/extraction/codes.py` | e-invoice/UPI QR + barcode decoding, JWT parse/verify, code-vs-print |
| `sereno/extraction/crossread.py` | blind zoomed-crop third reading and majority vote |
| `sereno/extraction/repair.py` | arithmetic-guided, evidence-backed misread repair and suggestions |
| `sereno/masterdata.py` + `validation/context.py` | own GSTINs, vendor profiles, duplicates, bank-change, FY and row-count checks |
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
