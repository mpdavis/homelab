"""Deciding whether a meeting we have already digested has changed underneath us.

The clerk uploads a packet over an hour or so and then revises documents in
place, sometimes the next day, hours before the meeting. We digest once and
never look again, so a run that lands mid-upload captures a partial packet
permanently. Confirmed in live data: on the 2026-08-11 packet, all 33 documents
were created on 8/10 and three were revised on 8/11 — **with no change to the
item count**, so counting documents would have missed it entirely. It has to be
timestamps.

Three layers, cheapest first:

1. **Folder fingerprint** — free, every poll. Laserfiche propagates a folder's
   ``LastModified`` up from its children, and discovery already lists the year
   folders those rows come from. Comparing the stamp we stored at digest time
   against the one in front of us costs zero extra requests, so a no-op poll
   stays at the nine listings the README is careful about.
2. **Manifest diff** — one listing per tree, only for meetings that failed
   layer 1. Which documents were added, removed, or modified. This is what lets
   layer 1 be deliberately over-sensitive: a folder touched with no surviving
   child change costs one listing and stops before any model call.
3. **Policy** — when we are willing to act at all. Detection and cheap
   re-summarization make revisions possible and affordable; policy is what stops
   a CronJob polling every ten minutes from reworking itself into a large bill.

Comparisons are on the **raw timestamp strings**, never parsed ordering. The
repository serves naive local wall time, so string inequality sidesteps DST and
clock skew and still catches a stamp moving backwards. Parsing happens only for
the quiet period, which needs an age rather than an identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .meetings import Meeting
from .state import PHASE_OUTCOME, PHASE_PREVIEW
from .weblink import Entry, parse_listing_datetime

log = logging.getLogger(__name__)

# Which document trees each pass reads, and therefore which folder stamps make
# up its fingerprint. A revised agenda changes segmentation, so the preview
# watches both of its trees.
PHASE_TREES: dict[str, tuple[str, ...]] = {
    PHASE_PREVIEW: ("agenda", "packet"),
    PHASE_OUTCOME: ("minutes",),
}


def fingerprint(meeting: Meeting, phase: str) -> dict[str, str]:
    """The folder stamps this pass depends on, as stored in state."""
    return {
        tree: meeting.folder_stamps[tree]
        for tree in PHASE_TREES.get(phase, ())
        if meeting.folder_stamps.get(tree)
    }


def documents(meeting: Meeting, phase: str) -> list[Entry]:
    """Exactly the documents this pass reads — no more, no less.

    The combined ``~Master`` packet PDF is deliberately excluded when individual
    items exist: it duplicates them, so a revision that only touches the master
    is not a change to anything we summarize, and treating it as one would buy a
    full re-digest for nothing.
    """
    if phase == PHASE_OUTCOME:
        return [meeting.minutes] if meeting.minutes else []

    found = [meeting.agenda] if meeting.agenda else []
    if meeting.packet_items:
        found += list(meeting.packet_items)
    elif meeting.packet_master:
        found.append(meeting.packet_master)
    return found


def manifest(entries: list[Entry]) -> dict[str, dict]:
    """``{entry_id: {mod, pages, name}}`` for a pass's documents.

    Keyed by string because that is what survives a JSON round trip; an int key
    written to state comes back as a string and would compare unequal to every
    live entry, quietly reporting the whole packet as new.
    """
    return {
        str(entry.entry_id): {
            "mod": entry.last_modified_text,
            "pages": entry.page_count,
            "name": entry.name,
        }
        for entry in entries
    }


@dataclass
class ManifestDiff:
    """What changed between the documents we digested and the ones served now."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    @property
    def resummarize(self) -> set[int]:
        """Entry ids whose text we have to pay for again."""
        return {int(e) for e in (*self.added, *self.modified)}

    @property
    def reusable(self) -> set[int]:
        return {int(e) for e in self.unchanged}

    def __str__(self) -> str:
        return (
            f"{len(self.added)} added, {len(self.modified)} modified, "
            f"{len(self.removed)} removed, {len(self.unchanged)} unchanged"
        )


def diff_manifests(before: dict, after: dict) -> ManifestDiff:
    """Classify each document. A malformed stored manifest reads as all-new."""
    before = before if isinstance(before, dict) else {}
    result = ManifestDiff()

    for entry_id, current in after.items():
        previous = before.get(entry_id)
        if not isinstance(previous, dict):
            result.added.append(entry_id)
        elif previous.get("mod") != current.get("mod"):
            result.modified.append(entry_id)
        else:
            result.unchanged.append(entry_id)

    result.removed = [e for e in before if e not in after]

    for bucket in (result.added, result.removed, result.modified, result.unchanged):
        bucket.sort()
    return result


# --- policy -----------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """The spend guardrails around acting on a detected revision.

    Detection is free and re-summarization is cheap; neither is a reason to
    re-digest without limit. Every field here exists to bound a way the
    CronJob could otherwise bill us repeatedly for work nobody asked for.
    """

    # How long a meeting's folders must sit unchanged before its first digest.
    # The 2026-08-11 packet uploaded over 67 minutes; digesting mid-burst
    # guarantees rework. 0 disables the wait.
    quiet_period: timedelta = timedelta(minutes=120)
    # Most revisions we will pay for on one pass, after the original digest.
    revision_cap: int = 5
    # The repository serves naive local wall time with no zone, so an age needs
    # to be measured against the same wall clock. Ames is Central.
    now: datetime | None = None

    def age_of(self, stamp: str) -> timedelta | None:
        """How long ago a listing stamp was written, or None if unreadable."""
        when = parse_listing_datetime(stamp)
        if when is None or self.now is None:
            return None
        return self.now - when

    def settled(self, stamps: dict[str, str]) -> bool:
        """Whether every folder has been quiet long enough to digest.

        Fails **open**: an unreadable stamp, or one that appears to be in the
        future because the two clocks disagree, counts as settled. A quiet
        period that silently does nothing is a missed optimization; one that
        wedges the pipeline forever is an outage.
        """
        if not self.quiet_period:
            return True
        for tree, stamp in stamps.items():
            age = self.age_of(stamp)
            if age is None:
                log.debug("%s stamp %r is unreadable; treating as settled", tree, stamp)
                continue
            if age < timedelta(0):
                log.warning(
                    "%s folder stamp %r is in the future — check that the "
                    "repository timezone is configured correctly; treating as settled",
                    tree,
                    stamp,
                )
                continue
            if age < self.quiet_period:
                return False
        return True


def frozen_reason(
    meeting: Meeting, phase: str, *, outcome_recorded: bool, today: date
) -> str | None:
    """Why this pass may never be revised again, or None if it still may.

    Only ever consulted for a **revision**. A pass that has never run is not
    frozen by anything here — otherwise no past meeting would ever get its
    outcome written at all.
    """
    if outcome_recorded:
        # The meeting is over and its result is published. A clerk tidying old
        # folders must not re-bill us for history nobody is re-reading.
        return "its outcome is already published"
    if phase == PHASE_PREVIEW and meeting.meeting_date < today:
        return "the meeting has already happened"
    return None
