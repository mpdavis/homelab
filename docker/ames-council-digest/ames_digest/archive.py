"""Machine-readable record of what a preview digest found.

The outcome pass needs to say "council approved *this*, the thing the packet
described" — which means it needs the preview's per-item summaries. Re-deriving
them would mean re-downloading and re-summarizing the whole packet a second
time, at full token cost, to reconstruct something already computed.

So each preview writes a small JSON sidecar next to the state file. It lives in
the state directory rather than the output directory because it is pipeline
state, not a delivered artifact — it must exist regardless of which delivery
sinks are configured.

Missing or unreadable archives are not an error: the outcome pass degrades to
summarizing the minutes on their own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE_DIRNAME = "meetings"
ARCHIVE_VERSION = 1

# Meeting keys are already slugs, but this file name reaches the filesystem —
# refuse anything that could climb out of the archive directory.
SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _path(state_dir: Path, key: str) -> Path:
    if not SAFE_KEY_RE.match(key):
        raise ValueError(f"unsafe meeting key for a filename: {key!r}")
    return state_dir / ARCHIVE_DIRNAME / f"{key}.json"


def save_preview(state_dir: Path, key: str, items: list[dict]) -> None:
    """Persist a preview's item summaries for later cross-reference."""
    path = _path(state_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": ARCHIVE_VERSION, "key": key, "items": items}

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


def load_preview(state_dir: Path, key: str) -> list[dict]:
    """Item summaries from a previous preview, or [] if there is no usable archive."""
    try:
        path = _path(state_dir, key)
    except ValueError as exc:
        log.warning("%s", exc)
        return []

    if not path.exists():
        log.debug("no preview archive for %s", key)
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("preview archive for %s is unreadable (%s)", key, exc)
        return []

    items = payload.get("items")
    if not isinstance(items, list):
        log.warning("preview archive for %s has no item list", key)
        return []
    return [i for i in items if isinstance(i, dict)]
