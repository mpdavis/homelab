"""Environment-driven configuration.

Everything is overridable so the same image can run against a different board,
a different model gateway, or a scratch output directory without a rebuild.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    # --- Source repository -------------------------------------------------
    weblink_base_url: str = field(
        default_factory=lambda: _env(
            "AMES_WEBLINK_BASE_URL", "https://publicdocs.cityofames.org/WebLink"
        )
    )
    weblink_repo: str = field(default_factory=lambda: _env("AMES_WEBLINK_REPO", "COA"))
    # "Clerk Files" — the root of everything the city clerk publishes.
    root_folder_id: int = field(
        default_factory=lambda: _env_int("AMES_ROOT_FOLDER_ID", 236500)
    )
    board: str = field(default_factory=lambda: _env("AMES_BOARD", "City Council"))

    # --- Model gateway -----------------------------------------------------
    # opencode zen speaks the Anthropic Messages API at /zen/v1/messages, so the
    # same client works against api.anthropic.com or a local bridge by swapping
    # the base URL.
    llm_base_url: str = field(
        default_factory=lambda: _env("AMES_LLM_BASE_URL", "https://opencode.ai/zen/v1")
    )
    llm_api_key: str = field(
        default_factory=lambda: os.environ.get("AMES_LLM_API_KEY", "").strip()
        or os.environ.get("OPENCODE_API_KEY", "").strip()
    )
    # Per-item summaries are the bulk of the tokens, so they default to a
    # cheaper model than the one that writes the final digest.
    item_model: str = field(
        default_factory=lambda: _env("AMES_ITEM_MODEL", "claude-sonnet-4-5")
    )
    digest_model: str = field(
        default_factory=lambda: _env("AMES_DIGEST_MODEL", "claude-sonnet-4-5")
    )
    # Characters of extracted PDF text handed to the model per packet item.
    # ~4 chars/token, so 120k chars is roughly a 30k-token prompt.
    item_char_budget: int = field(
        default_factory=lambda: _env_int("AMES_ITEM_CHAR_BUDGET", 120_000)
    )
    agenda_char_budget: int = field(
        default_factory=lambda: _env_int("AMES_AGENDA_CHAR_BUDGET", 200_000)
    )
    # Summary minutes run ~25k chars for a full meeting, so this is generous
    # headroom rather than a real constraint.
    minutes_char_budget: int = field(
        default_factory=lambda: _env_int("AMES_MINUTES_CHAR_BUDGET", 200_000)
    )
    max_concurrency: int = field(
        default_factory=lambda: _env_int("AMES_MAX_CONCURRENCY", 4)
    )

    # --- Output and delivery ----------------------------------------------
    state_dir: Path = field(
        default_factory=lambda: Path(_env("AMES_STATE_DIR", "/data/state"))
    )
    output_dir: Path = field(
        default_factory=lambda: Path(_env("AMES_OUTPUT_DIR", "/data/digests"))
    )
    # Named sinks from delivery.py: file, stdout, ntfy, smtp.
    delivery: list[str] = field(
        default_factory=lambda: _env_list("AMES_DELIVERY", ["file", "stdout"])
    )

    # Optional, and unset by default: the index page shows a spend estimate only
    # when both rates are supplied. Gateway prices change and vary by model, so
    # baking numbers into the image would ship a figure that quietly goes stale
    # and reads as authoritative.
    price_input_per_mtok: float | None = field(
        default_factory=lambda: _env_float("AMES_PRICE_INPUT_PER_MTOK")
    )
    price_output_per_mtok: float | None = field(
        default_factory=lambda: _env_float("AMES_PRICE_OUTPUT_PER_MTOK")
    )

    @property
    def prices_configured(self) -> bool:
        return (
            self.price_input_per_mtok is not None
            and self.price_output_per_mtok is not None
        )

    ntfy_url: str = field(default_factory=lambda: _env("NTFY_URL", ""))
    ntfy_topic: str = field(default_factory=lambda: _env("NTFY_TOPIC", ""))
    ntfy_token: str = field(default_factory=lambda: os.environ.get("NTFY_TOKEN", "").strip())

    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_username: str = field(default_factory=lambda: _env("SMTP_USERNAME", ""))
    smtp_password: str = field(
        default_factory=lambda: os.environ.get("SMTP_PASSWORD", "").strip()
    )
    smtp_starttls: bool = field(
        default_factory=lambda: _env("SMTP_STARTTLS", "true").lower() != "false"
    )
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM", ""))
    mail_to: list[str] = field(default_factory=lambda: _env_list("MAIL_TO", []))

    def require_llm(self) -> None:
        if not self.llm_api_key:
            raise SystemExit(
                "No model API key set. Export AMES_LLM_API_KEY (opencode zen key) "
                "or run with --dry-run to fetch documents without summarizing."
            )
