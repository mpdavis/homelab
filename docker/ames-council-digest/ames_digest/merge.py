"""Splice post-meeting outcomes into the page written before the meeting.

Each meeting gets one page, not two. The preview pass writes it from the agenda
and packet; once the minutes are published the outcome pass reopens that same
Markdown and attaches, to every bullet under "Additional Reading", what council
actually did — in red, so a reader can tell at a glance which sentences are the
forecast and which are the record.

Bullets are matched by their bolded label rather than by position. The model is
handed the exact labels this module extracted and must echo them back, so a
label it reorders or invents simply fails to match and leaves that bullet with
the "not recorded" default — rather than stapling one item's vote onto another.

Everything here is line-oriented string surgery on Markdown. That is deliberate:
the alternative is asking the model to reproduce the whole page with outcomes
woven in, which pays to regenerate prose that was already correct and gives it a
chance to quietly reword the parts nobody asked it to touch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

NOTABLE_TOPICS = "Notable Topics"
ADDITIONAL_READING = "Additional Reading"
AFTER_THE_MEETING = "After the meeting"

# Red against the digest's white card: 6.6:1, comfortably past AA for body text.
# Inline rather than a CSS class because the same HTML is rendered in email
# clients, which strip <style> blocks.
UPDATE_COLOR = "#b42318"
UPDATE_PREFIX = "**Update:**"

# What a bullet gets when the minutes say nothing about it. Silence is a real
# outcome — an item can be pulled, deferred, or simply never reached — and a
# bullet left with no update at all reads as one the pass forgot.
NO_UPDATE = "Not recorded in the minutes."

# Emitted by the minutes-only fallback prompt, which has to write the page and
# its outcomes in a single call and so has no structured channel to put them in.
SENTINEL = "[UPDATE]"

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s+(.*)$")
LABEL_RE = re.compile(r"^\*\*(.+?)\*\*")
SPAN_RE = re.compile(
    r'\s*<span style="color:[^"]*">\s*' + re.escape(UPDATE_PREFIX) + r".*?</span>",
    re.DOTALL,
)
SPAN_INNER_RE = re.compile(r'<span style="color:[^"]*">(.*?)</span>', re.DOTALL)
SENTINEL_RE = re.compile(re.escape(SENTINEL) + r"\s*(.*?)\s*$", re.MULTILINE)

# Long enough to keep distinct items distinct, short enough that a model asked
# to copy it back does so reliably.
MAX_LABEL_CHARS = 80


@dataclass(frozen=True)
class Bullet:
    """One list item in a section, and the line span it occupies."""

    label: str
    start: int
    # Exclusive, and trimmed of trailing blank lines — so ``end - 1`` is the
    # line an update gets appended to, whether the list is tight or loose.
    end: int


def normalize(text: str) -> str:
    """Fold a label to what two spellings of the same item have in common."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _section(lines: list[str], title: str) -> tuple[int, int] | None:
    """Line bounds of a section's body, exclusive of its heading."""
    want = normalize(title)
    for i, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if not match or normalize(match.group(2)) != want:
            continue
        level = len(match.group(1))
        for j in range(i + 1, len(lines)):
            following = HEADING_RE.match(lines[j])
            if following and len(following.group(1)) <= level:
                return i + 1, j
        return i + 1, len(lines)
    return None


def _bullet_text(line: str) -> str | None:
    """A bullet's content, or None if this line does not open one.

    "Bullet" is what the prompt calls these; what the model actually emits is
    only sometimes a Markdown list. Left to itself it writes each one as a bare
    paragraph opening with the bolded label — no ``-`` in sight — and requiring
    the list marker silently found nothing to attach outcomes to. The label is
    what identifies an item here, so the label is what this looks for.
    """
    match = BULLET_RE.match(line)
    if match:
        return match.group(1).strip()
    stripped = line.strip()
    return stripped if LABEL_RE.match(stripped) else None


def _bullets(lines: list[str], start: int, end: int) -> list[Bullet]:
    starts = [i for i in range(start, end) if _bullet_text(lines[i]) is not None]
    bullets = []
    for n, first in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else end
        while stop > first + 1 and not lines[stop - 1].strip():
            stop -= 1
        text = _bullet_text(lines[first])
        assert text is not None  # `first` came from the same predicate
        labelled = LABEL_RE.match(text)
        # A bullet the model wrote without its bolded label still needs an
        # identity; its opening words are the best one available.
        label = (labelled.group(1) if labelled else text)[:MAX_LABEL_CHARS].strip()
        bullets.append(Bullet(label=label, start=first, end=stop))
    return bullets


def _span(text: str) -> str:
    return (
        f'<span style="color:{UPDATE_COLOR}">{UPDATE_PREFIX} '
        f'{" ".join(str(text).split())}</span>'
    )


def labels(body: str) -> list[str]:
    """The label of every Additional Reading bullet, in document order."""
    lines = body.splitlines()
    bounds = _section(lines, ADDITIONAL_READING)
    return [] if bounds is None else [b.label for b in _bullets(lines, *bounds)]


def apply_updates(body: str, updates: dict[str, str]) -> tuple[int, int, str]:
    """Attach an update to every Additional Reading bullet.

    Returns ``(matched, total, body)`` — how many bullets got a real outcome out
    of how many exist, so the caller can log a merge that mostly missed rather
    than shipping a page of "not recorded" and calling it a success.
    """
    lines = body.splitlines()
    bounds = _section(lines, ADDITIONAL_READING)
    if bounds is None:
        log.warning("no %r section in this page; nothing to update", ADDITIONAL_READING)
        return 0, 0, body

    keyed = {normalize(k): v for k, v in updates.items() if str(v).strip()}
    bullets = _bullets(lines, *bounds)
    matched = 0

    for bullet in bullets:
        outcome = keyed.get(normalize(bullet.label))
        if outcome:
            matched += 1
        else:
            log.debug("no outcome returned for %r", bullet.label)
        last = bullet.end - 1
        # Strip first: a forced re-run merges into a body that already carries
        # last run's update, and appending would stack them.
        lines[last] = SPAN_RE.sub("", lines[last]).rstrip() + " " + _span(
            outcome or NO_UPDATE
        )

    return matched, len(bullets), "\n".join(lines)


def promote_sentinels(body: str) -> str:
    """Style the fallback prompt's ``[UPDATE] ...`` tails as real updates."""
    return SENTINEL_RE.sub(lambda m: _span(m.group(1)), body)


def append_section(body: str, title: str, content: str) -> str:
    """Add a section to the end of a page, or return it untouched if empty."""
    content = content.strip()
    if not content:
        return body
    return f"{body.rstrip()}\n\n## {title}\n\n{content}\n"


def plaintext(body: str) -> str:
    """Unwrap the update spans, for sinks that carry text rather than HTML."""
    return SPAN_INNER_RE.sub(r"\1", body)
