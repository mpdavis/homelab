"""Environment-driven configuration.

Everything is overridable so the same image can run against a different board,
a different model gateway, or a scratch output directory without a rebuild.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


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

    # --- Meeting furniture -------------------------------------------------
    # Where and when the board meets, used when the agenda PDF does not print
    # it. These belong beside `board`: change the board and these change with
    # it, which is what makes them a per-body fallback rather than a constant.
    # Ames City Council meets Tuesdays at 6:00 PM in City Hall.
    meeting_time: str = field(
        default_factory=lambda: _env("AMES_MEETING_TIME", "6:00 PM")
    )
    meeting_location: str = field(
        default_factory=lambda: _env("AMES_MEETING_LOCATION", "City Hall, 515 Clark Ave")
    )

    # --- Freshness and spend guardrails ------------------------------------
    # How long a meeting's folders must sit unchanged before it is digested.
    # The 2026-08-11 packet uploaded over 67 minutes; digesting mid-burst
    # guarantees rework. 0 disables the wait.
    quiet_period_minutes: int = field(
        default_factory=lambda: _env_int("AMES_QUIET_PERIOD_MINUTES", 120)
    )
    # Most revisions to pay for on one pass, after its original digest.
    revision_cap: int = field(
        default_factory=lambda: _env_int("AMES_REVISION_CAP", 5)
    )
    # Listing timestamps are the repository's local wall time with no zone
    # attached, so measuring how old one is requires knowing which clock wrote
    # it. Ames is Central. Only the quiet period depends on this — change
    # detection compares strings and needs no timezone at all.
    repo_timezone: str = field(
        default_factory=lambda: _env("AMES_REPO_TIMEZONE", "America/Chicago")
    )

    @property
    def quiet_period(self) -> timedelta:
        return timedelta(minutes=max(self.quiet_period_minutes, 0))

    def repo_now(self) -> datetime:
        """Now, as naive wall time on the repository's clock.

        Naive on purpose: it is compared against listing stamps, which are
        themselves naive. A misconfigured zone degrades the quiet period rather
        than breaking the run — see `freshness.Policy.settled`.
        """
        try:
            zone = ZoneInfo(self.repo_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            log.warning(
                "unknown AMES_REPO_TIMEZONE %r (%s); falling back to this "
                "machine's local time, which may skew the quiet period",
                self.repo_timezone,
                exc,
            )
            return datetime.now()
        return datetime.now(zone).replace(tzinfo=None)

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
