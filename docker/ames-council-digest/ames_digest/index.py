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


@dataclass(frozen=True)
class DigestEntry:
    stem: str
    title: str
    meeting_date: date | None
    has_markdown: bool

    @property
    def sort_key(self) -> tuple[date, str]:
        # Undated files sort last rather than crashing the comparison.
        return (self.meeting_date or date.min, self.stem)


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
    """Every digest in the directory, newest meeting first."""
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


def render_index(entries: list[DigestEntry]) -> str:
    if not entries:
        body = '<p class="empty">No digests yet.</p>'
        count = "Nothing published yet."
    else:
        items = []
        for entry in entries:
            markdown_link = (
                f' · <a href="{html.escape(entry.stem)}.md">Markdown</a>'
                if entry.has_markdown
                else ""
            )
            when = (
                entry.meeting_date.strftime("%B %-d, %Y")
                if entry.meeting_date
                else "date unknown"
            )
            items.append(
                f'      <li><a href="{html.escape(entry.stem)}.html">'
                f"{html.escape(entry.title)}</a>"
                f'<div class="meta">{html.escape(when)}{markdown_link}</div></li>'
            )
        body = "<ul>\n" + "\n".join(items) + "\n    </ul>"
        count = f"{len(entries)} meeting{'s' if len(entries) != 1 else ''}"

    return INDEX_TEMPLATE.format(count=html.escape(count), body=body)


def rebuild(output_dir: Path) -> int:
    """Regenerate index.html from the directory's contents. Returns the count."""
    entries = collect(output_dir)
    (output_dir / INDEX_FILENAME).write_text(render_index(entries), encoding="utf-8")
    return len(entries)
