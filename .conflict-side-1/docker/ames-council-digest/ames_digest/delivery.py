"""Where a rendered digest goes.

Delivery is deliberately behind a one-method interface so the decision about
how these summaries reach a reader — a file you open, a push notification, an
email, a mailing list — stays a configuration choice rather than a rewrite.
``AMES_DELIVERY`` names the active sinks; ``file`` and ``stdout`` need no
credentials and are the default.

Adding a sink means writing one class and registering it in :data:`SINKS`.
"""

from __future__ import annotations

import logging
import smtplib
import sys
from email.message import EmailMessage
from typing import Protocol

import httpx

from . import index
from . import web
from .config import Config
from .render import RenderedDigest

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """A sink was asked to deliver but is not usably configured."""


class Sink(Protocol):
    name: str

    def deliver(self, rendered: RenderedDigest, cfg: Config) -> str:
        """Deliver the digest and return a one-line description of where it went."""


def gateway_prices(cfg: Config) -> tuple[float, float] | None:
    """Gateway rates for the index's spend estimate, if both are configured."""
    if not cfg.prices_configured:
        return None
    return (cfg.price_input_per_mtok, cfg.price_output_per_mtok)  # type: ignore[return-value]


class FileSink:
    """Write Markdown and HTML next to each other in the output directory.

    Also refreshes ``index.html``, which is what the digest web server serves
    as its landing page.
    """

    name = "file"

    def deliver(self, rendered: RenderedDigest, cfg: Config) -> str:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        md_path = cfg.output_dir / f"{rendered.filename_stem}.md"
        html_path = cfg.output_dir / f"{rendered.filename_stem}.html"
        md_path.write_text(rendered.markdown, encoding="utf-8")
        html_path.write_text(rendered.html, encoding="utf-8")
        if rendered.record is not None:
            rendered.record.save(cfg.output_dir)
        count = index.rebuild(cfg.output_dir, cfg.state_dir, gateway_prices(cfg))
        web.publish(cfg.output_dir)
        return f"wrote {md_path}, {html_path.name}, and {rendered.filename_stem}.json (index: {count} meetings)"


class StdoutSink:
    """Print the Markdown digest — the useful default for a manual run."""

    name = "stdout"

    def deliver(self, rendered: RenderedDigest, cfg: Config) -> str:
        print(rendered.markdown, file=sys.stdout, flush=True)
        return "printed to stdout"


class NtfySink:
    """Push the digest to an ntfy topic."""

    name = "ntfy"

    def deliver(self, rendered: RenderedDigest, cfg: Config) -> str:
        if not cfg.ntfy_url or not cfg.ntfy_topic:
            raise DeliveryError("ntfy delivery needs NTFY_URL and NTFY_TOPIC")

        headers = {
            "Title": rendered.subject,
            "Tags": "classical_building",
            "Markdown": "yes",
        }
        if cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

        url = f"{cfg.ntfy_url.rstrip('/')}/{cfg.ntfy_topic}"
        # ntfy truncates very long messages; Notable Topics leads the page and
        # is the part that belongs on a phone anyway.
        resp = httpx.post(
            url, content=rendered.text[:3800].encode("utf-8"), headers=headers, timeout=30
        )
        resp.raise_for_status()
        return f"pushed to {url}"


class SmtpSink:
    """Send the digest as a multipart HTML email over SMTP."""

    name = "smtp"

    def deliver(self, rendered: RenderedDigest, cfg: Config) -> str:
        if not cfg.smtp_host or not cfg.mail_from or not cfg.mail_to:
            raise DeliveryError(
                "smtp delivery needs SMTP_HOST, MAIL_FROM, and MAIL_TO"
            )

        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = cfg.mail_from
        message["To"] = ", ".join(cfg.mail_to)
        message.set_content(rendered.markdown)
        message.add_alternative(rendered.html, subtype="html")

        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as server:
            if cfg.smtp_starttls:
                server.starttls()
            if cfg.smtp_username:
                server.login(cfg.smtp_username, cfg.smtp_password)
            server.send_message(message)

        return f"emailed {len(cfg.mail_to)} recipient(s) via {cfg.smtp_host}"


SINKS: dict[str, type] = {
    FileSink.name: FileSink,
    StdoutSink.name: StdoutSink,
    NtfySink.name: NtfySink,
    SmtpSink.name: SmtpSink,
}


def build_sinks(cfg: Config) -> list[Sink]:
    sinks: list[Sink] = []
    for name in cfg.delivery:
        sink_cls = SINKS.get(name)
        if sink_cls is None:
            raise DeliveryError(
                f"unknown delivery sink {name!r}; choose from {', '.join(sorted(SINKS))}"
            )
        sinks.append(sink_cls())
    return sinks


def deliver_all(rendered: RenderedDigest, cfg: Config, sinks: list[Sink]) -> list[str]:
    """Run every sink, letting one failure not cost the others.

    Raises if every sink failed — a digest that reached nobody is a failed run.
    """
    results: list[str] = []
    failures: list[str] = []
    for sink in sinks:
        try:
            results.append(f"{sink.name}: {sink.deliver(rendered, cfg)}")
        except Exception as exc:
            log.error("delivery via %s failed: %s", sink.name, exc)
            failures.append(f"{sink.name}: {exc}")

    if failures and not results:
        raise DeliveryError("; ".join(failures))
    return results + [f"FAILED {f}" for f in failures]
