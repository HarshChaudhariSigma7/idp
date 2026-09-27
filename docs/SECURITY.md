# Security, privacy and audit controls

Built so ISO 27001 / SOC 2 becomes paperwork: the controls below exist from the first document
and accumulate history. Each row names the code that implements it.

| Control | Implementation | Code | ISO 27001:2022 Annex A | SOC 2 |
|---|---|---|---|---|
| Encryption at rest (AES-256) | AES-256-GCM envelope encryption: random data key per blob, wrapped by a KEK; sensitive DB columns (extracted values, model outputs, filenames, MFA secrets) encrypted at column level; key id stored with every ciphertext | `security/crypto.py` | 8.24 | CC6.1, CC6.7 |
| Customer-managed keys (later) | `KeyProvider` interface: add a KMS/CMK provider; per-tenant keys map via key id. No schema change | `security/crypto.py` | 8.24 | CC6.1 |
| Encryption in transit | TLS terminated at load balancer; HSTS; secure, HttpOnly, SameSite=Strict cookies | `api/app.py` | 8.20, 8.24 | CC6.7 |
| Time-boxed raw document storage | Raw uploads + page images in a separate encrypted store with expiry: 72h default, extended only while in review, **hard cap 7 days**, 24h after completion; sweep deletes blob + wrapped key (crypto-shred) and audits it | `storage/temp_store.py`, `pipeline.retention_sweep` | 8.10, 5.33 | C1.2, P4.2 |
| System of record separated | Postgres holds validated structured data; no raw documents | `models.py` | 8.10 | C1.1 |
| Eval retention only by written opt-in | `tenant.eval_retention_opt_in`, 90-day expiry | `pipeline.py` | 5.34 | P3.1 |
| Audit log | Every upload, processing step, model used, page view, review action, export, login, admin change. Hash-chained (tamper-evident); `verify-audit` detects edits/deletes | `security/audit.py` | 8.15, 8.17 | CC7.2, CC4.1 |
| No PII in logs | Audit meta sanitiser rejects anything but ids, numbers and status tokens (enforced + tested) | `security/audit.py` | 8.11, 8.15 | P4.3 |
| RBAC | Explicit permission table; reviewers see only documents assigned to them; Sereno ops sees aggregates only; tenant isolation on every query | `security/rbac.py` | 5.15, 8.3 | CC6.1, CC6.3 |
| MFA | TOTP mandatory for every account (no document access without it) | `security/auth.py` | 8.5 | CC6.1 |
| Password security | argon2id, 12-char minimum, lockout after 5 failures | `security/auth.py` | 5.17, 8.5 | CC6.1 |
| Session management | Server-side sessions, revocable, 10h expiry | `security/auth.py` | 8.5 | CC6.1 |
| Web hardening | Strict CSP (no inline script/style), CSRF header, X-Frame-Options DENY, nosniff, no-store on API, document text rendered via textContent only (XSS-safe), CSV formula-injection neutralised | `api/app.py`, `web/app.js`, `exports/tabular.py` | 8.26, 8.28 | CC6.8 |
| Subprocessor register | In product (Admin page, `/api/subprocessors`) and in docs | `config.py`, `docs/SUBPROCESSORS.md` | 5.19–5.22 | CC9.2 |
| Change traceability | Prompt version + model id logged per extraction; thresholds versioned with evidence | `extraction/prompts.py`, `ThresholdConfig` | 8.32 | CC8.1 |
| Monitoring of processing integrity | Accuracy/escape-rate metrics, calibration, QA sampling | `metrics.py`, `eval/` | 8.16 | PI1.4, PI1.5 |

## Still to do before a security questionnaire

1. Production KMS (AWS KMS in ap-south-1) replacing the env master key; key rotation runbook.
2. Deploy in India region; record residency in the subprocessor list per customer.
3. Alembic migrations baseline (schema is currently created with `create_all`).
4. SSO (SAML/OIDC) for larger customers; IP allow-listing option.
5. Centralised log shipping with 1-year retention; alerting on audit-chain breaks and failed logins.
6. Written policies (access review quarterly, incident response, vendor management, BCP) and
   the first quarterly access review exported from the Admin page.
7. Annual third-party penetration test.
8. DPDP Act 2023 notice + consent language in the customer contract (data fiduciary = customer,
   Sereno = data processor).
