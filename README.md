# Sereno Volante IDP

Accuracy-first document extraction for Indian mid-market manufacturers (₹300–5,000 cr).
Invoices, lorry receipts (LR/GR/bilty), POs, GRNs, contracts: clean PDFs, scans, phone photos,
carbon copies, handwriting, English + Hindi. Every field carries evidence-based confidence;
anything uncertain goes to a human, field by field. We never silently guess.

## Status (be honest in every customer conversation)

| Area | State |
|---|---|
| Pipeline, validation, review UI, dashboard, exports, 3-way match, templates, security base | Built, 73 automated tests passing |
| Accuracy engine: one page reading, deterministic proof, one zoomed re-read of what isn't proven, evidence-backed repair, company records, batch split | Built; scenario + call-budget tests ([tests/test_accuracy_scenarios.py](tests/test_accuracy_scenarios.py)); QR decoding verified on real samples |
| Auto-accept thresholds | Certified from spot checks with a statistical guarantee ([sereno/eval/backtest.py](sereno/eval/backtest.py)); needs real reviewed volume before it can certify |
| Real web documents (25: printed, handwritten, Hindi, sideways, blank traps) | All non-model stages tested; 5 real-world bugs found and fixed ([datasets/web_v1](datasets/web_v1/README.md)). Model accuracy on them: needs `ANTHROPIC_API_KEY` |
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
| `reviewer@demo.local` | review queue (keyboard: ↑↓ move, Enter confirm, 1/2 pick a reading, type to fix, ⌘/Ctrl+Enter finish) |
| `admin@demo.local` | users, tamper-evident audit trail, subprocessors |
| `ops@demo.local` | internal metrics and the demo gate |

One password and one authenticator entry work for all four (printed in the terminal, saved in
`var/DEMO_LOGIN.txt`). Demo numbers are simulated; a "Demo" tag in the header says so.

- Read your own documents: put `ANTHROPIC_API_KEY=...` in `.env` (created on first run), restart. Gemini also works: set `SERENO_LLM_BACKEND=gemini` and `GEMINI_API_KEY=...` instead.
- Real accuracy on 25 real web documents: `./run_local.sh --eval` (asks first; roughly $10-15 of API usage).
- Start over: `./run_local.sh --reset`. Other port: `--port 8010`.

## How a document flows

AI calls are the expensive, error-prone step, so the pipeline spends them only where nothing
cheaper can decide. Typical budget: digital PDF or e-invoice **1 call**, scan of a known type
**1-2 calls**, unknown-type scan on a new account **3 calls**.

```
upload ─► encrypted temp store (TTL) ─► pre-check: DPI, skew, blur, contrast, ink, text layer
      ─► auto-correct: rotate, deskew, upscale, denoise, contrast, sharpen (logged per page)
      ─► unreadable gate (on ORIGINAL pixels) ─► human with plain reason, no AI call
      ─► document type for free: the type the user picked, a signed e-invoice QR, the PDF text,
         or the company's usual type. A cheap triage call only for multi-page scans or a truly
         unknown scan (it also splits batch files into one document each)
      ─► codes: GST e-invoice QR (signed JWT: GSTINs, number, date, total, item count, IRN), UPI QR,
         barcodes, decoded locally. Exact data, no AI
      ─► CALL 1, page reading: full strict-JSON schema with a location for every value; margins
         trimmed, cached system prompt; Sonnet 5.5 for clean pages, Opus 5.5 for poor scans,
         handwriting, Hindi and LRs. It also reports the type it sees (wrong guess: re-read)
      ─► proof without AI: QR/barcode, exact arithmetic (qty × rate, line sums, GST, totals,
         amount in words), the PDF text layer, company records. Proven values are done
      ─► CALL 2 (only if something is unproven): one zoomed re-read of just those fields, by the
         other model, on ink-enhanced crops. Agree: done. Disagree: a person picks (key 1 or 2)
      ─► repair: when totals still fail, the arithmetic picks between the two readings; a fix is
         applied only if one unique value balances every check AND a reading saw it
      ─► company records: own GSTINs, vendor name/bank/number history, bank-change alert,
         duplicate block, auto-template after 3 reviews
      ─► confidence per field from evidence (never the model's own certainty) ─► review or ready
      ─► spot checks: a random 5% of all documents get every field confirmed; these certify the
         auto-accept thresholds (Learn then Test, exact binomial, document clustering corrected)
```

## Run manually (developers)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]" reportlab
.venv/bin/pytest -q                                   # 73 tests, no API key needed
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
| Weekly | Refit weights, certify thresholds from spot checks | `python -m sereno.eval.backtest --doc-type invoice [--apply]` |
| Before any demo / prompt change | Labelled eval, per bucket, gate | `python -m sereno.eval.run_eval eval_data/real` |
| Monthly | Audit chain integrity | `python scripts/manage.py verify-audit` |

## Code map

| Path | Purpose |
|---|---|
| `sereno/ingest/` | loading (PDF/image, text layer), quality scoring, auto-correction |
| `sereno/extraction/doc_specs.py` | field specs per doc type → strict JSON schemas (union-free, compiler-safe) |
| `sereno/extraction/{prompts,extractor,llm}.py` | versioned prompts, the page reading, Claude client (prompt caching, refusal fallback) |
| `sereno/extraction/doctype.py` | document type and page grouping from free signals (no AI call) |
| `sereno/extraction/evidence.py` | which values are proven without AI (QR, exact arithmetic, text layer, records) |
| `sereno/extraction/codes.py` | e-invoice/UPI QR + barcode decoding, JWT parse/verify, code-vs-print |
| `sereno/extraction/crossread.py` | one zoomed re-read of the unproven fields |
| `sereno/extraction/repair.py` | arithmetic-guided, evidence-backed misread repair and suggestions |
| `sereno/masterdata.py` + `validation/context.py` | own GSTINs, vendor profiles, duplicates, bank-change, FY and row-count checks |
| `sereno/validation/` | GST/arithmetic/format checks, GSTIN checksum, amount-in-words |
| `sereno/confidence/scorer.py` | evidence-based field confidence + routing rules |
| `sereno/pipeline.py` | orchestration, retention, SLA, assignment |
| `sereno/review.py` | reviewer actions, re-validation, vendor history |
| `sereno/matching/three_way.py` | PO × GRN × invoice reconciliation |
| `sereno/templates_onboarding.py` | "show me 3–5 examples" vendor templates |
| `sereno/security/` | AES-256-GCM envelope crypto, auth + TOTP MFA, RBAC, hash-chained audit |
| `sereno/eval/` | eval harness + demo gate, certified-threshold backtest, synthetic generator |
| `sereno/web/` | the app (no build step) |

Docs: [architecture](docs/ARCHITECTURE.md) · [security & audit controls](docs/SECURITY.md) ·
[subprocessors](docs/SUBPROCESSORS.md) · [evaluation protocol](docs/EVALUATION.md)
