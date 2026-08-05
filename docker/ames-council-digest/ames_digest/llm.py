"""Minimal Anthropic Messages API client.

opencode zen exposes an Anthropic-compatible endpoint at
``https://opencode.ai/zen/v1/messages``, and so do api.anthropic.com and local
bridges like Meridian. Talking to the wire format directly (rather than through
a vendor SDK) keeps the image small and avoids coupling the gateway's behavior
to an SDK version.

Auth is sent as both ``x-api-key`` and ``Authorization: Bearer`` because
gateways differ on which one they read; each ignores the other. This is not
belt-and-braces: zen's ``/messages`` reads ``x-api-key`` specifically and
answers "Missing API key" to a Bearer-only request, even though its docs show
Bearer for the OpenAI-shaped endpoints.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    """A request failed after exhausting retries."""


@dataclass
class Usage:
    """Running token totals across a run, for the cost line in the digest."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, payload: dict) -> None:
        usage = payload.get("usage") or {}
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        # Cache reads still bill, but at a discount; fold them into input so the
        # reported number never understates usage.
        self.input_tokens += int(usage.get("cache_read_input_tokens") or 0)
        self.input_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        self.calls += 1


@dataclass
class LLMClient:
    base_url: str
    api_key: str
    timeout: float = 300.0
    max_attempts: int = 5
    usage: Usage = field(default_factory=Usage)
    _client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """Send a single-turn message and return the concatenated text blocks."""
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": self.api_key,
            "authorization": f"Bearer {self.api_key}",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                assert self._client is not None
                resp = self._client.post(
                    f"{self.base_url}/messages", json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleep(attempt, None)
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                last_error = LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")
                self._sleep(attempt, resp.headers.get("retry-after"))
                continue

            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:800]}")

            payload = resp.json()
            self.usage.add(payload)
            return "".join(
                block.get("text", "")
                for block in payload.get("content") or []
                if block.get("type") == "text"
            ).strip()

        raise LLMError(f"request failed after {self.max_attempts} attempts: {last_error}")

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60.0))
                return
            except ValueError:
                pass
        # Exponential backoff with jitter, capped so a stuck gateway doesn't
        # stall a CronJob past its deadline.
        delay = min(2 ** attempt, 30) * (0.5 + random.random() / 2)
        log.debug("retrying in %.1fs (attempt %d)", delay, attempt)
        time.sleep(delay)
