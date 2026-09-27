"""Runtime configuration. Every tunable that affects accuracy, security or retention lives here
so it is visible, versioned, and auditable. Override via environment variables prefixed SERENO_."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERENO_", env_file=".env", extra="ignore")

    env: str = "dev"  # dev | test | prod
    database_url: str = "sqlite:///./var/sereno.db"
    data_dir: Path = Path("./var/data")

    # --- Encryption -------------------------------------------------------------------------
    # 32-byte key, base64. In prod this comes from a KMS/secret manager, never from a file in git.
    master_key_b64: str = ""
    master_key_id: str = "local-v1"

    # --- Models -----------------------------------------------------------------------------
    # Accuracy first: Opus for anything that is not a trivially clean digital document.
    model_complex: str = "claude-opus-5"
    model_clean: str = "claude-sonnet-5"
    model_triage: str = "claude-sonnet-5"
    extraction_effort: str = "high"
    extraction_effort_hard: str = "xhigh"  # handwriting, Indic script, poor scans: think harder
    crop_reads: bool = True                # third blind read on zoomed crops (disputes, hard docs, failed maths)
    arithmetic_repair: bool = True
    # NIC IRP public keys (PEM) to verify e-invoice QR signatures; without them QR data is used unverified
    einvoice_public_keys_pem: list[str] = Field(default_factory=list)
    # Server-side refusal fallback for Opus-tier requests (Claude API only, beta).
    enable_refusal_fallback: bool = True
    llm_max_retries: int = 3
    llm_timeout_s: float = 300.0
    max_image_long_edge: int = 2400
    max_pages_per_document: int = 12
    # "fake" is used by tests and the offline demo seeder; never in prod.
    llm_backend: str = "anthropic"

    # --- Quality gates ----------------------------------------------------------------------
    blur_floor: float = 1.0           # contrast-normalised sharpness of the ORIGINAL scan; below => unreadable
    contrast_floor: float = 0.06      # ink-vs-paper contrast of the original; below => unreadable
    min_char_height_px: float = 5.0   # characters smaller than this cannot be recovered
    min_ink_ratio: float = 0.002      # essentially blank page

    # --- Confidence & routing ---------------------------------------------------------------
    auto_accept_threshold: float = 0.90   # tuned weekly by eval/backtest.py
    high_stakes_threshold: float = 0.95   # stricter bar for totals / taxes / quantities
    doc_arithmetic_penalty: float = 0.80  # multiplies every field score if any math check fails
    qa_sample_rate: float = 0.05          # share of fully auto-accepted docs sent to spot-check
    amount_tolerance_abs: float = 1.0     # rupees; covers legitimate round-off
    amount_tolerance_rel: float = 0.00002  # ₹2 per crore; anything looser hides real misreads

    # --- Review SLA -------------------------------------------------------------------------
    review_sla_minutes: int = 240  # 4-hour target, shown in product

    # --- Retention --------------------------------------------------------------------------
    doc_ttl_hours: int = 72               # raw docs + page images while processing/review
    doc_hard_cap_hours: int = 168         # absolute maximum, even if review is pending
    doc_grace_after_done_hours: int = 24  # after review/export completes
    eval_retention_days: int = 90         # only for tenants that opted in, in writing

    # --- Auth -------------------------------------------------------------------------------
    session_hours: int = 10
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    cookie_secure: bool = True

    # --- Worker -----------------------------------------------------------------------------
    worker_threads: int = 4
    worker_poll_s: float = 1.0
    run_worker_in_app: bool = True

    subprocessors: list[dict] = Field(default_factory=lambda: [
        {"name": "Anthropic, PBC", "purpose": "AI model inference (document reading/extraction)",
         "data": "Document page images and text, transient; not used for model training under commercial terms",
         "location": "United States", "link": "https://www.anthropic.com/legal/commercial-terms"},
        {"name": "Cloud host (to be confirmed per deployment, e.g. AWS ap-south-1 Mumbai)",
         "purpose": "Application hosting, encrypted storage, managed Postgres",
         "data": "Encrypted documents (time-boxed) and extracted data", "location": "India (target)",
         "link": ""},
    ])

    @property
    def is_test(self) -> bool:
        return self.env == "test"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
