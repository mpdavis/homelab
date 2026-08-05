"""Build the index page listing every digest written so far.

Rebuilt from the output directory itself rather than from run state, so it
self-heals: a digest restored from backup, or one written before the index
existed, still shows up on the next run.

Titles are read back out of each digest's ``<title>`` tag — the same value
:func:`ames_digest.render.subject_line` put there — so the index and the pages
it links can't drift apart.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_FILENAME = "index.html"

TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
# Every digest stem embeds its meeting date: city-council-2026-07-28[-tax-levy].
STEM_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ames Council Digests</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 24px 16px; background: #f6f7f9; color: #1f2328;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.55;
  }}
  .card {{
    max-width: 640px; margin: 0 auto; background: #fff; border: 1px solid #d8dee4;
    border-radius: 10px; padding: 28px 30px;
  }}
  h1 {{ margin: 0 0 4px; font-size: 20px; line-height: 1.3; }}
  .sub {{ margin: 0 0 20px; color: #59636e; font-size: 13px; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ border-top: 1px solid #e4e8ec; padding: 12px 0; }}
  a {{ color: #0969da; text-decoration: none; font-weight: 600; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ color: #59636e; font-size: 12px; margin-top: 2px; }}
  .meta a {{ font-weight: 400; }}
  .md {{ color: #59636e; }}
  .empty {{ color: #59636e; font-style: italic; }}
  footer {{ margin-top: 24px; border-top: 1px solid #e4e8ec; padding-top: 12px;
            color: #59636e; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    .card {{ background: #161b22; border-color: #30363d; }}
    li, footer {{ border-color: #30363d; }}
    a {{ color: #4493f8; }}
    .sub, .meta, .empty, footer {{ color: #9198a1; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <h1>Ames Council Digests</h1>
    <p class="sub">{count}</p>
    {body}
    <footer>Machine-generated summaries of documents published by the Ames city
    clerk. Verify against the source documents before acting on them.</footer>
  </div>
</body>
</html>
"""


OUTCOME_SUFFIX = "-outcome"


@dataclass(frozen=True)
class DigestEntry:
    stem: str
    title: str
    meeting_date: date | None
    has_markdown: bool

    @property
    def is_outcome(self) -> bool:
        return self.stem.endswith(OUTCOME_SUFFIX)

    @property
    def meeting_stem(self) -> str:
        """The preview's stem — the identity both passes of a meeting share."""
        return (
            self.stem[: -len(OUTCOME_SUFFIX)] if self.is_outcome else self.stem
        )

    @property
    def sort_key(self) -> tuple[date, str]:
        # Undated files sort last rather than crashing the comparison.
        return (self.meeting_date or date.min, self.stem)


@dataclass
class MeetingRow:
    """Both passes of one meeting, shown as a single entry."""

    meeting_stem: str
    preview: DigestEntry | None = None
    outcome: DigestEntry | None = None

    @property
    def title(self) -> str:
        # The preview's title is the plain meeting name; the outcome's carries
        # a "what council decided" suffix that would read oddly as the heading.
        if self.preview:
            return self.preview.title
        if self.outcome:
            return self.outcome.title.split(" — what council decided")[0]
        return self.meeting_stem

    @property
    def meeting_date(self) -> date | None:
        for entry in (self.preview, self.outcome):
            if entry and entry.meeting_date:
                return entry.meeting_date
        return None

    @property
    def sort_key(self) -> tuple[date, str]:
        return (self.meeting_date or date.min, self.meeting_stem)


def _title_of(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return path.stem
    m = TITLE_RE.search(head)
    return html.unescape(m.group(1).strip()) if m else path.stem


def _date_of(stem: str) -> date | None:
    m = STEM_DATE_RE.search(stem)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def collect(output_dir: Path) -> list[DigestEntry]:
    """Every digest file in the directory, newest meeting first."""
    entries = [
        DigestEntry(
            stem=path.stem,
            title=_title_of(path),
            meeting_date=_date_of(path.stem),
            has_markdown=path.with_suffix(".md").exists(),
        )
        for path in sorted(output_dir.glob("*.html"))
        if path.name != INDEX_FILENAME
    ]
    return sorted(entries, key=lambda e: e.sort_key, reverse=True)


def collect_meetings(output_dir: Path) -> list[MeetingRow]:
    """Digests grouped by meeting, so both passes share one row."""
    rows: dict[str, MeetingRow] = {}
    for entry in collect(output_dir):
        row = rows.setdefault(entry.meeting_stem, MeetingRow(entry.meeting_stem))
        if entry.is_outcome:
            row.outcome = entry
        else:
            row.preview = entry
    return sorted(rows.values(), key=lambda r: r.sort_key, reverse=True)


def _links(entry: DigestEntry | None, label: str) -> str:
    if entry is None:
        return ""
    stem = html.escape(entry.stem)
    link = f'<a href="{stem}.html">{label}</a>'
    if entry.has_markdown:
        link += f' <a href="{stem}.md" class="md">(md)</a>'
    return link


def render_index(rows: list[MeetingRow]) -> str:
    if not rows:
        body = '<p class="empty">No digests yet.</p>'
        count = "Nothing published yet."
    else:
        items = []
        for row in rows:
            when = (
                row.meeting_date.strftime("%B %-d, %Y")
                if row.meeting_date
                else "date unknown"
            )
            # The heading links to the outcome once it exists — that's the
            # fuller account — and to the preview before then.
            primary = row.outcome or row.preview
            assert primary is not None  # a row exists only if one pass wrote a file
            parts = [
                _links(row.preview, "Before the meeting"),
                _links(row.outcome, "What council decided"),
            ]
            meta = " · ".join([html.escape(when), *[p for p in parts if p]])
            items.append(
                f'      <li><a href="{html.escape(primary.stem)}.html">'
                f"{html.escape(row.title)}</a>"
                f'<div class="meta">{meta}</div></li>'
            )
        body = "<ul>\n" + "\n".join(items) + "\n    </ul>"
        count = f"{len(rows)} meeting{'s' if len(rows) != 1 else ''}"

    return INDEX_TEMPLATE.format(count=html.escape(count), body=body)


def rebuild(output_dir: Path) -> int:
    """Regenerate index.html from the directory's contents. Returns the count."""
    rows = collect_meetings(output_dir)
    (output_dir / INDEX_FILENAME).write_text(render_index(rows), encoding="utf-8")
    return len(rows)
