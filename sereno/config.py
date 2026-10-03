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
    # One page read, then deterministic verification, then ONE zoomed re-read of whatever the
    # checks could not prove (docs/ARCHITECTURE.md, "Call budget"). The first read uses Sonnet on
    # clean/good documents and Opus on poor scans and LRs; the re-read is always Opus on zoomed
    # crops: a different model and a different view, so the two readings fail independently.
    # Provider: llm_backend ("anthropic" | "gemini" | "fake"); "fake" is for tests and the offline
    # demo only. Gemini (sereno/extraction/gemini_llm.py) is opt-in via SERENO_LLM_BACKEND=gemini.
    llm_backend: str = "anthropic"
    model_complex: str = "claude-opus-5-5"
    model_clean: str = "claude-sonnet-5-5"
    model_triage: str = "claude-sonnet-5-5"   # only for multi-page scans (batch splitting)
    model_verify: str = "claude-opus-5-5"
    # Vision accuracy on these models comes from zoomed crops more than from extra thinking, so the
    # page read runs at medium effort and only hard documents go to high.
    extraction_effort: str = "medium"
    extraction_effort_hard: str = "high"
    verify_effort: str = "medium"
    crop_reads: bool = True                # the zoomed re-read of unverified fields
    max_verify_items: int = 40
    arithmetic_repair: bool = True
    # NIC IRP public keys (PEM) to verify e-invoice QR signatures; without them QR data is used unverified
    einvoice_public_keys_pem: list[str] = Field(default_factory=list)
    # Server-side refusal fallback for Opus-tier requests. Anthropic backend only; ignored on Gemini.
    enable_refusal_fallback: bool = True
    llm_max_retries: int = 3
    llm_timeout_s: float = 300.0
    # Image tokens scale with pixel area (~1 per 28x28 patch, 2576 px max). Digital PDFs also send
    # their text layer, so they need fewer pixels; poor scans get the full resolution.
    max_image_long_edge: int = 2576
    page_edge_digital: int = 1800
    page_edge_good: int = 2200
    max_pages_per_document: int = 12

    # --- Quality gates ----------------------------------------------------------------------
    blur_floor: float = 1.0           # contrast-normalised sharpness of the ORIGINAL scan; below => unreadable
    contrast_floor: float = 0.06      # ink-vs-paper contrast of the original; below => unreadable
    min_char_height_px: float = 5.0   # characters smaller than this cannot be recovered
    min_ink_ratio: float = 0.002      # essentially blank page

    # --- Confidence & routing ---------------------------------------------------------------
    auto_accept_threshold: float = 0.90   # tuned weekly by eval/backtest.py
    high_stakes_threshold: float = 0.95   # stricter bar for totals / taxes / quantities
    doc_arithmetic_penalty: float = 0.80  # multiplies every field score if any math check fails
    qa_sample_rate: float = 0.05          # share of documents where a person confirms every field (calibration)
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
