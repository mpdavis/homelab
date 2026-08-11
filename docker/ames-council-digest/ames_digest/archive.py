"""Machine-readable record of what a preview digest produced.

A meeting has one page, written before the meeting and updated after it. The
outcome pass therefore needs two things the preview already computed: the page's
Markdown, so it can splice outcomes into the bullets rather than rewrite them,
and the per-item records, so it can say "council approved *this*, the thing the
packet described". Re-deriving either would mean re-downloading and
re-summarizing the whole packet at full token cost.

Each item is stored whole, not trimmed to what today's page renders, and carries
the entry id and last-modified time of the document it was read from. That makes
this file the answer to "what did we summarize, and from which version" — the
question a later run has to ask, because the clerk revises packet documents in
place after we have already digested them.

So each preview writes a small JSON sidecar next to the state file. It lives in
the state directory rather than the output directory because it is pipeline
state, not a delivered artifact — it must exist regardless of which delivery
sinks are configured.

Missing or unreadable archives are not an error: the outcome pass degrades to
writing the page from the minutes on their own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .llm import Usage

log = logging.getLogger(__name__)

ARCHIVE_DIRNAME = "meetings"
# v1 stored the item summaries alone, because the outcome pass wrote its own
# separate page and had nothing to merge into.
# v2 added the page body, so the outcome pass could splice rather than rewrite.
# v3 widened each item from summary/significance/amount to the full record, and
# added the per-document `last_modified` a re-run needs to tell a revised packet
# item from one already paid for. Nothing reads v2 items: the service is not
# live, so the old shape is simply re-summarized rather than migrated.
# v4 added the segmented agenda — the meeting's time and place, and its items in
# printed order including the ones no packet document matched. The outcome pass
# reads it back rather than paying to segment the agenda a second time.
ARCHIVE_VERSION = 4

# Meeting keys are already slugs, but this file name reaches the filesystem —
# refuse anything that could climb out of the archive directory.
SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class PreviewArchive:
    """What the preview pass left behind for the outcome pass to build on."""

    items: list[dict] = field(default_factory=list)
    # The segmented agenda, stored raw and deserialized by the caller so this
    # module keeps knowing nothing about the shape of what it persists. Empty
    # for an archive written before v4, or for a meeting with no agenda.
    agenda: dict = field(default_factory=dict)
    # Empty for a v1 archive, or when no preview ever ran. Both mean the same
    # thing to the caller: there is no page to update, so write one.
    body: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    generated_at: str = ""

    @property
    def has_page(self) -> bool:
        return bool(self.body.strip())

    def by_entry_id(self) -> dict[int, dict]:
        """The archived items keyed by the repository entry they came from.

        ``items`` stays a list because agenda order is part of the record and a
        dict on disk would lose it. This is the lookup that order costs: given a
        fresh listing, what did we summarize for this document, and from which
        version of it. Items with no usable entry id are dropped rather than
        collected under 0 — they cannot be matched to a listing row anyway.
        """
        keyed = {}
        for item in self.items:
            entry_id = item.get("entry_id")
            if isinstance(entry_id, int) and entry_id:
                keyed[entry_id] = item
        return keyed


def _path(state_dir: Path, key: str) -> Path:
    if not SAFE_KEY_RE.match(key):
        raise ValueError(f"unsafe meeting key for a filename: {key!r}")
    return state_dir / ARCHIVE_DIRNAME / f"{key}.json"


def save_preview(
    state_dir: Path,
    key: str,
    items: list[dict],
    *,
    agenda: dict,
    body: str,
    usage: Usage,
    model: str,
    generated_at: str,
) -> None:
    """Persist everything the outcome pass needs to update this page in place."""
    path = _path(state_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": ARCHIVE_VERSION,
        "key": key,
        "items": items,
        "agenda": agenda,
        "body": body,
        "model": model,
        "generated_at": generated_at,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "calls": usage.calls,
        },
    }

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{key}-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _usage(payload: dict) -> Usage:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        calls=int(raw.get("calls") or 0),
    )


def load_preview(state_dir: Path, key: str) -> PreviewArchive:
    """The previous preview's output, or an empty archive if there is none.

    A v1 file still yields its item summaries; it simply has no page body, so
    the caller falls back to writing the page from the minutes.
    """
    empty = PreviewArchive()

    try:
        path = _path(state_dir, key)
    except ValueError as exc:
        log.warning("%s", exc)
        return empty

    if not path.exists():
        log.debug("no preview archive for %s", key)
        return empty

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("preview archive for %s is unreadable (%s)", key, exc)
        return empty

    items = payload.get("items")
    if not isinstance(items, list):
        log.warning("preview archive for %s has no item list", key)
        items = []

    body = payload.get("body")
    if payload.get("version") == 1:
        log.info(
            "%s has a v1 preview archive (no page body); the outcome will be "
            "written from the minutes alone",
            key,
        )

    agenda = payload.get("agenda")

    return PreviewArchive(
        items=[i for i in items if isinstance(i, dict)],
        agenda=agenda if isinstance(agenda, dict) else {},
        body=body if isinstance(body, str) else "",
        usage=_usage(payload),
        model=str(payload.get("model") or ""),
        generated_at=str(payload.get("generated_at") or ""),
    )
