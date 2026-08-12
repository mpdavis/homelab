"""Render a :class:`MeetingDigest` into the forms a delivery sink might want.

Three representations come out of one digest: Markdown (the canonical artifact
written to disk and read in a terminal), HTML (for an email body or a browser),
and a short plain-text form for push notifications.

Both passes of a meeting render to the same filename. The outcome pass rewrites
the page rather than adding a second one, so a reader has one URL per meeting
whose content grows once the minutes land.
"""

from __future__ import annotations

from dataclasses import dataclass

import markdown as markdown_lib

from . import merge
from .record import MeetingRecord
from .summarize import MeetingDigest

# Kept inline rather than in a stylesheet: email clients strip <style> blocks
# inconsistently, and the digest should look the same in a browser and an inbox.
HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Which pass last wrote this page. The index reads it back to mark a meeting
     as updated, which keeps that page self-describing: a digest restored from
     backup still lands in the right state without consulting the run ledger. -->
<meta name="ames-digest-phase" content="{phase}">
<title>{title}</title>
</head>
<body style="margin:0;padding:24px 16px;background:#f6f7f9;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
             color:#1f2328;line-height:1.55;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #d8dee4;
              border-radius:10px;padding:28px 30px;">
    <h1 style="margin:0 0 4px;font-size:20px;line-height:1.3;">{title}</h1>
    <p style="margin:0 0 20px;color:#59636e;font-size:13px;">{subtitle}</p>
    <hr style="border:none;border-top:1px solid #e4e8ec;margin:0 0 20px;">
    {body}
    <hr style="border:none;border-top:1px solid #e4e8ec;margin:24px 0 12px;">
    <p style="margin:0;color:#59636e;font-size:12px;">{footer}</p>
  </div>
</body>
</html>
"""

# Python-Markdown emits bare tags; email clients need the spacing spelled out.
INLINE_STYLES = {
    "<h2>": '<h2 style="margin:24px 0 8px;font-size:16px;line-height:1.3;">',
    "<h3>": '<h3 style="margin:20px 0 6px;font-size:14px;line-height:1.3;">',
    "<p>": '<p style="margin:0 0 12px;">',
    "<ul>": '<ul style="margin:0 0 12px;padding-left:22px;">',
    "<ol>": '<ol style="margin:0 0 12px;padding-left:22px;">',
    "<li>": '<li style="margin:0 0 8px;">',
    "<a ": '<a style="color:#0969da;" ',
}


@dataclass
class RenderedDigest:
    subject: str
    markdown: str
    html: str
    text: str
    filename_stem: str
    record: MeetingRecord | None = None


def _pretty_date(digest: MeetingDigest) -> str:
    return digest.meeting.meeting_date.strftime("%B %-d, %Y")


def subject_line(digest: MeetingDigest) -> str:
    # No "what council decided" suffix any more: one page carries the meeting
    # from preview to outcome, and a title that changed underneath a reader's
    # bookmark would suggest a different document.
    return f"{digest.meeting.display_name} — {_pretty_date(digest)}"


def filename_stem(digest: MeetingDigest) -> str:
    """Output basename. Both passes write this same file."""
    return digest.meeting.key


def _subtitle(digest: MeetingDigest) -> str:
    """The line under the title: when and where, then the source documents."""
    links = []
    if digest.minutes_url:
        links.append(f"[Minutes]({digest.minutes_url})")
    if digest.agenda_url:
        links.append(f"[Agenda]({digest.agenda_url})")
    if digest.packet_url:
        links.append(f"[Full packet]({digest.packet_url})")
    # One dot-separated run rather than two lines: the template's subtitle is a
    # single <p>, and the docket's stacked header arrives with the web
    # templates in phase 4 — which do not share this email renderer.
    return " · ".join(p for p in (digest.agenda.venue, *links) if p)


def _appendix(digest: MeetingDigest) -> list[str]:
    """A linked index of every packet item, so nothing is invisible."""
    orphans = digest.agenda.orphans
    if not digest.items and not orphans:
        return []

    lines = ["", "---", ""]

    if digest.items:
        lines += ["### Every item in this packet", ""]
    for item in digest.items:
        label = f"**{item.code}** " if item.code else ""
        note = ""
        if not item.ok:
            # Skip reasons can carry a whole HTTP error body; the reader needs
            # the gist, and the full text is already in the run's logs.
            reason = " ".join((item.skipped or "unknown").split())
            if len(reason) > 120:
                reason = reason[:117] + "…"
            note = f" — _not summarized: {reason}_"
        elif item.significance != "routine":
            note = f" — _{item.significance}_"
            if item.amount:
                note = f" — _{item.significance}, {item.amount}_"
        lines.append(f"- {label}[{item.title}]({item.url}){note}")

    if orphans:
        # The other half of the appendix's promise. Plenty of agenda business
        # never produces a packet document — proclamations, public forum,
        # council referrals — and a title with no PDF is still a title the
        # reader is entitled to see.
        lines += ["", "### On the agenda, with no packet document", ""]
        for entry in orphans:
            number = f"**{entry.item_number}** " if entry.item_number else ""
            note = f" — _{entry.section}_" if entry.section else ""
            lines.append(f"- {number}{entry.title}{note}")

    return lines


def _footer(digest: MeetingDigest) -> str:
    # The page is one artifact built by up to two passes, so it reports what the
    # whole thing cost. Billing only the update pass would show a few thousand
    # tokens for a page whose packet summaries cost half a million.
    usage = digest.total_usage

    scope = []
    if digest.items:
        scope.append(f"{len(digest.items)} packet items")
    if digest.is_outcome:
        scope.append("official summary minutes")

    when = f"Generated {digest.generated_at.strftime('%Y-%m-%d %H:%M')}"
    if digest.is_outcome and digest.preview_generated_at:
        when = (
            f"Generated {digest.preview_generated_at.strftime('%Y-%m-%d %H:%M')}, "
            f"updated {digest.generated_at.strftime('%Y-%m-%d %H:%M')}"
        )
    if digest.revision:
        # The clerk revises packet documents in place after we have digested
        # them. A reader comparing this page against the packet deserves to
        # know it was rebuilt, and how many times.
        plural = "s" if digest.revision > 1 else ""
        when += f" · rebuilt after {digest.revision} source revision{plural}"

    return (
        f"{when} by ames-council-digest using {digest.model} · "
        f"{' · '.join(scope) or 'no source documents'} · "
        f"{usage.calls} model calls, "
        f"{usage.input_tokens:,} in / {usage.output_tokens:,} out tokens · "
        "Summaries are machine-generated — verify against the source documents "
        "before acting on them."
    )


def render(digest: MeetingDigest) -> RenderedDigest:
    title = subject_line(digest)
    source = _subtitle(digest)

    md_parts = [f"# {title}", ""]
    if source:
        md_parts += [source, ""]
    md_parts.append(digest.body_markdown.strip())
    md_parts += _appendix(digest)
    md_parts += ["", "---", "", f"_{_footer(digest)}_", ""]
    markdown_text = "\n".join(md_parts)

    # The <h1>/source/footer chrome is supplied by the template, so only the
    # body and appendix get converted.
    body_md = "\n".join([digest.body_markdown.strip(), *_appendix(digest)])
    body_html = markdown_lib.markdown(body_md, extensions=["extra", "sane_lists"])
    for tag, styled in INLINE_STYLES.items():
        body_html = body_html.replace(tag, styled)

    subtitle_html = markdown_lib.markdown(source).replace("<p>", "").replace("</p>", "")
    for tag, styled in INLINE_STYLES.items():
        subtitle_html = subtitle_html.replace(tag, styled)

    html = HTML_TEMPLATE.format(
        title=title,
        subtitle=subtitle_html,
        body=body_html,
        footer=_footer(digest),
        phase=digest.kind,
    )

    return RenderedDigest(
        subject=subject_line(digest),
        markdown=markdown_text,
        html=html,
        # The update spans are markup a push notification would show raw, so the
        # text form keeps the words and drops the styling.
        text=merge.plaintext(digest.body_markdown.strip()),
        filename_stem=filename_stem(digest),
    )


def render_record(record: MeetingRecord) -> RenderedDigest:
    """Render a stored record; this path must never need an LLM."""
    rendered = render(record.to_digest())
    rendered.record = record
    return rendered
