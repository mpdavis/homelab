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

from .state import State, Totals

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
  /* KPI row: a handful of headline numbers is a stat row, not a chart.
     No series hues here — the numbers wear text tokens and the surface
     carries the grouping. */
  .kpis {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 20px; }}
  .kpi {{ flex: 1 1 130px; background: #f6f8fa; border: 1px solid #e4e8ec;
          border-radius: 8px; padding: 10px 12px; }}
  .kpi .label {{ color: #59636e; font-size: 11px; line-height: 1.3; }}
  /* Proportional figures: tabular-nums is for columns that must align, and
     makes a standalone display number look loose. */
  .kpi .value {{ font-size: 19px; font-weight: 600; margin-top: 2px;
                 letter-spacing: -0.01em; }}
  .kpi .sub {{ color: #59636e; font-size: 11px; margin-top: 1px; }}
  footer {{ margin-top: 24px; border-top: 1px solid #e4e8ec; padding-top: 12px;
            color: #59636e; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    .card {{ background: #161b22; border-color: #30363d; }}
    li, footer {{ border-color: #30363d; }}
    a {{ color: #4493f8; }}
    .sub, .meta, .empty, footer {{ color: #9198a1; }}
    /* Dark steps are chosen against the dark surface, not flipped from light. */
    .kpi {{ background: #0d1117; border-color: #30363d; }}
    .kpi .label, .kpi .sub {{ color: #9198a1; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <h1>Ames Council Digests</h1>
    <p class="sub">{count}</p>
    {kpis}
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


def compact(n: int) -> str:
    """Auto-compact a count: 1,284 / 12.9K / 4.21M.

    The K cutoff is 999,500 rather than 1,000,000 so a value that would round
    up to "1000.0K" rolls over to "1.00M" instead.
    """
    if n < 10_000:
        return f"{n:,}"
    if n < 999_500:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def _tile(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{html.escape(sub)}</div>' if sub else ""
    return (
        '      <div class="kpi">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f"{sub_html}</div>"
    )


def render_kpis(totals: Totals | None, prices: tuple[float, float] | None) -> str:
    """The KPI row. A handful of headline numbers — a stat row, not a chart."""
    if totals is None or not totals.digests:
        return ""

    # Four tiles is the most that fits the card on one row; the input/output
    # split rides along as secondary text rather than earning tiles of its own.
    tiles = [
        _tile(
            "Tokens used",
            compact(totals.total_tokens),
            f"{compact(totals.input_tokens)} in · {compact(totals.output_tokens)} out",
        ),
        _tile(
            "Digests",
            compact(totals.digests),
            f"{totals.previews} preview · {totals.outcomes} outcome",
        ),
    ]

    # Records predating call tracking contribute tokens but no call count, so a
    # bare "0" would understate rather than inform. Omit the tile until there is
    # something real to show, and mark the total as a floor while any remain.
    if totals.calls:
        suffix = "+" if totals.records_missing_calls else ""
        tiles.append(
            _tile("Model calls", f"{compact(totals.calls)}{suffix}", "across all digests")
        )

    if prices is not None:
        tiles.append(
            _tile("Est. spend", f"${totals.cost(*prices):,.2f}", "at configured rates")
        )

    return '<div class="kpis">\n' + "\n".join(tiles) + "\n    </div>"


def _links(entry: DigestEntry | None, label: str) -> str:
    if entry is None:
        return ""
    stem = html.escape(entry.stem)
    link = f'<a href="{stem}.html">{label}</a>'
    if entry.has_markdown:
        link += f' <a href="{stem}.md" class="md">(md)</a>'
    return link


def render_index(
    rows: list[MeetingRow],
    totals: Totals | None = None,
    prices: tuple[float, float] | None = None,
) -> str:
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

    return INDEX_TEMPLATE.format(
        count=html.escape(count),
        kpis=render_kpis(totals, prices),
        body=body,
    )


def rebuild(
    output_dir: Path,
    state_dir: Path | None = None,
    prices: tuple[float, float] | None = None,
) -> int:
    """Regenerate index.html from the directory's contents. Returns the count.

    ``state_dir`` supplies the usage ledger for the KPI row. It is optional so
    the index can still be rebuilt from a directory of digests alone.
    """
    rows = collect_meetings(output_dir)

    totals = None
    if state_dir is not None:
        try:
            totals = State.load(state_dir).totals()
        except OSError as exc:
            # The page is worth publishing without its counters.
            log.warning("could not read usage totals: %s", exc)

    (output_dir / INDEX_FILENAME).write_text(
        render_index(rows, totals, prices), encoding="utf-8"
    )
    return len(rows)
