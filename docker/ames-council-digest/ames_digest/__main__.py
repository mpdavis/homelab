"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Config
from . import archive, index
from .delivery import DeliveryError, build_sinks, deliver_all
from .llm import LLMClient
from .meetings import Meeting, MeetingSource
from .pdftext import extract
from .render import render
from .state import PHASE_OUTCOME, PHASE_PREVIEW, PHASES, State
from .summarize import MeetingSummarizer
from .weblink import WebLinkClient

log = logging.getLogger("ames_digest")


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a date as YYYY-MM-DD, got {value!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ames-digest",
        description=(
            "Summarize Ames city council agendas and packets from the city's "
            "public Laserfiche repository."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug-level logging"
    )
    parser.add_argument(
        "--board",
        help="board or commission to track (default: $AMES_BOARD or City Council)",
    )

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="digest new meetings and deliver them")
    run.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="only consider meetings this recent (default: 30)",
    )
    run.add_argument(
        "--lookahead-days",
        type=int,
        default=10,
        help=(
            "also digest upcoming meetings this far ahead, once their packet "
            "has been posted (default: 10)"
        ),
    )
    run.add_argument(
        "--meeting",
        type=_parse_date,
        action="append",
        metavar="YYYY-MM-DD",
        help="digest a specific meeting date; repeatable, implies --force",
    )
    run.add_argument(
        "--limit",
        type=int,
        default=3,
        help="most meetings to digest in one run (default: 3)",
    )
    run.add_argument(
        "--phase",
        choices=[*PHASES, "both"],
        default="both",
        help=(
            "which pass to run: preview (agenda + packet, before the meeting), "
            "outcome (minutes, after it), or both (default)"
        ),
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="re-digest passes already recorded in state",
    )
    run.add_argument(
        "--no-state",
        action="store_true",
        help="do not read or write the state file",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and extract documents but make no model calls",
    )
    run.add_argument(
        "--delivery",
        help="comma-separated sinks, overriding $AMES_DELIVERY (file,stdout,ntfy,smtp)",
    )

    sub.add_parser(
        "index",
        help="rebuild the index page from the digests on disk (no model calls)",
    )

    listing = sub.add_parser("list", help="show known meetings and their digest state")
    listing.add_argument(
        "--since-days",
        type=int,
        default=365,
        help="how far back to look (default: 365)",
    )

    return parser


def _years_for_window(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))


@dataclass
class Job:
    """One digest to produce: a meeting and which pass to run over it."""

    meeting: Meeting
    phase: str

    def __str__(self) -> str:
        return f"{self.meeting.key} ({self.phase})"


def _pending_phases(
    meeting: Meeting,
    args: argparse.Namespace,
    state: State | None,
    today: date,
) -> list[str]:
    """Which passes this meeting still needs, given what's published.

    Assumes documents are already loaded — the caller decides whether a
    meeting is worth that request.
    """
    wanted = PHASES if args.phase == "both" else (args.phase,)
    pending = []

    for phase in wanted:
        if state is not None and not args.force and state.seen(meeting.key, phase):
            continue

        if phase == PHASE_PREVIEW:
            if not meeting.has_preview_documents:
                continue
            # The preview is most useful before the meeting, so upcoming ones
            # are in scope — but only once the packet is actually up. Running
            # it early would burn the single shot state gives us.
            if meeting.meeting_date > today and not (
                meeting.agenda and meeting.packet_items
            ):
                log.debug("%s: packet not posted yet", meeting.key)
                continue
        elif phase == PHASE_OUTCOME:
            # Minutes appear about a week after the meeting; until then there
            # is simply nothing to report.
            if not meeting.has_minutes:
                log.debug("%s: minutes not posted yet", meeting.key)
                continue

        pending.append(phase)

    return pending


def _select(
    meetings: list[Meeting],
    args: argparse.Namespace,
    state: State | None,
    cutoff: date,
    today: date,
    source: MeetingSource,
) -> list[Job]:
    """Choose what to digest, listing documents only for real candidates.

    Ordering matters for politeness as much as speed: the date and state
    filters are free, so they run first and keep a routine poll from listing
    the documents of meetings whose passes are all long since done.
    """
    if args.meeting:
        wanted = set(args.meeting)
        selected = [m for m in meetings if m.meeting_date in wanted]
        for miss in sorted(wanted - {m.meeting_date for m in selected}):
            log.error("no meeting found on %s", miss.isoformat())
        jobs = []
        for meeting in selected:
            source.load_documents(meeting)
            jobs += [
                Job(meeting, p) for p in _pending_phases(meeting, args, state, today)
            ]
        return jobs

    horizon = today + timedelta(days=max(args.lookahead_days, 0))
    candidates = [m for m in meetings if cutoff <= m.meeting_date <= horizon]
    if state is not None and not args.force:
        # Skip only meetings with nothing left to do in either pass.
        candidates = [
            m
            for m in candidates
            if not all(state.seen(m.key, p) for p in PHASES)
        ]

    # Newest first, so a limited run covers the meeting a reader most wants.
    candidates.sort(key=lambda m: (m.meeting_date, m.label), reverse=True)

    limit = max(args.limit, 0) if args.limit else None
    jobs: list[Job] = []
    for meeting in candidates:
        if limit is not None and len(jobs) >= limit:
            break
        source.load_documents(meeting)
        # A folder created ahead of its posting. Skip it and look further back
        # rather than letting it consume a slot in the limit.
        if not meeting.has_documents:
            continue
        jobs += [Job(meeting, p) for p in _pending_phases(meeting, args, state, today)]

    return jobs[:limit] if limit is not None else jobs


def _refresh_index(cfg: Config) -> None:
    """Keep the web server's landing page current even on a no-op run.

    The file sink rebuilds the index whenever it writes a digest, but a run
    with nothing new never reaches it — and on a fresh volume that would leave
    the server with no index at all until the first meeting lands.
    """
    if "file" not in cfg.delivery:
        return
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        count = index.rebuild(cfg.output_dir)
        log.debug("index refreshed (%d digests)", count)
    except OSError as exc:
        # Cosmetic; never fail a run over it.
        log.warning("could not refresh the index page: %s", exc)


def _dry_run(meeting: Meeting, weblink: WebLinkClient, cfg: Config) -> None:
    print(f"\n{meeting}")
    docs = []
    if meeting.agenda:
        docs.append(("agenda", meeting.agenda))
    docs += [("item", item) for item in meeting.packet_items]

    usable = 0
    for kind, doc in docs:
        try:
            data = weblink.download(doc.entry_id)
            budget = cfg.agenda_char_budget if kind == "agenda" else cfg.item_char_budget
            result = extract(data, budget)
            status = (
                f"{result.page_count}p {len(result.text):>7,} chars"
                f"{' TRUNCATED' if result.truncated else ''}"
                if result.usable
                else f"UNUSABLE ({result.error or 'no text layer'})"
            )
            usable += 1 if result.usable else 0
        except Exception as exc:
            status = f"FETCH FAILED ({exc})"
        print(f"  [{kind:6}] {doc.name[:66]:<66} {status}")
    print(f"  -> {usable}/{len(docs)} documents have usable text")


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    today = date.today()
    cutoff = today - timedelta(days=args.since_days)
    if args.meeting:
        cutoff = min(args.meeting)

    state = None if args.no_state else State.load(cfg.state_dir)

    with WebLinkClient(cfg.weblink_base_url, cfg.weblink_repo) as weblink:
        source = MeetingSource(weblink, cfg.root_folder_id, cfg.board)
        # Widen the year range to the lookahead horizon so a late-December run
        # still sees January's meetings.
        horizon = today + timedelta(days=max(args.lookahead_days, 0))
        meetings = source.discover(_years_for_window(cutoff, horizon))
        log.info("discovered %d meetings for %s", len(meetings), cfg.board)

        selected = _select(meetings, args, state, cutoff, today, source)
        if not selected:
            log.info("no new meetings to digest")
            _refresh_index(cfg)
            return 0

        log.info(
            "%d digest(s) to produce: %s",
            len(selected),
            ", ".join(str(j) for j in selected),
        )

        if args.dry_run:
            # Both passes of a meeting share its documents, so report each
            # meeting once. Meeting is a mutable dataclass and so unhashable —
            # dedupe on the key it already defines for exactly this purpose.
            seen_keys: set[str] = set()
            for job in selected:
                if job.meeting.key in seen_keys:
                    continue
                seen_keys.add(job.meeting.key)
                _dry_run(job.meeting, weblink, cfg)
            return 0

        cfg.require_llm()
        sinks = build_sinks(cfg)
        llm = LLMClient(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        summarizer = MeetingSummarizer(cfg, weblink, llm)

        failures = 0
        for job in selected:
            meeting = job.meeting
            try:
                if job.phase == PHASE_PREVIEW:
                    digest = summarizer.run_preview(meeting)
                else:
                    # The archive is what lets the outcome say "council
                    # approved the thing the packet described" without paying
                    # to summarize the packet a second time. Its absence is
                    # survivable: run_outcome falls back to the minutes alone.
                    digest = summarizer.run_outcome(
                        meeting, archive.load_preview(cfg.state_dir, meeting.key)
                    )

                rendered = render(digest)
                for line in deliver_all(rendered, cfg, sinks):
                    log.info("%s -> %s", job, line)
            except (RuntimeError, DeliveryError) as exc:
                # One bad job must not abort the rest, and must not be recorded
                # as done — the next run retries it.
                log.error("failed to digest %s: %s", job, exc)
                failures += 1
                continue

            if state is not None:
                if job.phase == PHASE_PREVIEW:
                    try:
                        archive.save_preview(
                            cfg.state_dir,
                            meeting.key,
                            [i.to_archive() for i in digest.items if i.ok],
                        )
                    except (OSError, ValueError) as exc:
                        # Costs the outcome pass its cross-reference, not the
                        # digest that was just delivered.
                        log.warning("could not archive %s: %s", meeting.key, exc)

                state.record(
                    meeting.key,
                    job.phase,
                    meeting_date=meeting.meeting_date.isoformat(),
                    board=meeting.board,
                    items=len(digest.items),
                    input_tokens=digest.usage.input_tokens,
                    output_tokens=digest.usage.output_tokens,
                )
                state.save()

        llm.close()
        return 1 if failures else 0


def cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    today = date.today()
    cutoff = today - timedelta(days=args.since_days)
    state = State.load(cfg.state_dir)

    with WebLinkClient(cfg.weblink_base_url, cfg.weblink_repo) as weblink:
        source = MeetingSource(weblink, cfg.root_folder_id, cfg.board)
        # Include the rest of the current year so scheduled meetings show up.
        meetings = source.discover(_years_for_window(cutoff, date(today.year, 12, 31)))
        shown = [m for m in meetings if m.meeting_date >= cutoff]
        # Unlike a routine run, this is a human asking what's there — the item
        # counts are the point, so pay for the listings within the window.
        for meeting in shown:
            source.load_documents(meeting)
    print(f"{cfg.board}: {len(shown)} meetings since {cutoff.isoformat()}\n")
    print(
        f"{'date':<12} {'items':>5}  {'agenda':<7} {'minutes':<8} "
        f"{'preview':<8} {'outcome':<8} when"
    )
    for meeting in shown:
        when = "upcoming" if meeting.meeting_date > today else "past"
        print(
            f"{meeting.meeting_date.isoformat():<12} "
            f"{len(meeting.packet_items):>5}  "
            f"{'yes' if meeting.agenda else '-':<7} "
            f"{'yes' if meeting.has_minutes else '-':<8} "
            f"{'done' if state.seen(meeting.key, PHASE_PREVIEW) else '-':<8} "
            f"{'done' if state.seen(meeting.key, PHASE_OUTCOME) else '-':<8} {when}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = Config()
    if args.board:
        cfg.board = args.board
    if getattr(args, "delivery", None):
        cfg.delivery = [p.strip() for p in args.delivery.split(",") if p.strip()]

    command = args.command or "run"
    if command == "list":
        return cmd_list(args, cfg)
    if command == "index":
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        count = index.rebuild(cfg.output_dir)
        print(f"indexed {count} digest(s) in {cfg.output_dir}")
        return 0

    # `run` is the default when no subcommand is given; argparse hasn't filled
    # its defaults in that case, so supply them.
    for name, default in (
        ("since_days", 30),
        ("lookahead_days", 10),
        ("meeting", None),
        ("limit", 3),
        ("phase", "both"),
        ("force", False),
        ("no_state", False),
        ("dry_run", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    return cmd_run(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
