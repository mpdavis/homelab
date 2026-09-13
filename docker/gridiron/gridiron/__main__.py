"""Command line entry point.

The CLI is the research surface — the web UI shows results, this is where they
are produced. Typical first run:

    gridiron ingest --seasons 2015-2026
    gridiron features build
    gridiron backtest --model decomposed --seasons 2019-2025
    gridiron sweep --model decomposed --seasons 2019-2025
    gridiron analyze brand-premium
    gridiron edges
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import season_range, settings
from .models.ratings import MIN_TRAIN_GAMES


def _parse_seasons(text: str | None) -> list[int] | None:
    """Accept "2019-2024", "2019,2021,2023" or a single year."""
    if not text:
        return None
    seasons: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            seasons.extend(range(int(start), int(end) + 1))
        else:
            seasons.append(int(part))
    return sorted(set(seasons))


def _print_table(frame, max_rows: int = 60) -> None:
    import pandas as pd

    if frame is None or len(frame) == 0:
        print("(no rows)")
        return
    with pd.option_context(
        "display.max_rows", max_rows,
        "display.max_columns", 40,
        "display.width", 200,
        "display.float_format", lambda v: f"{v:,.3f}",
    ):
        print(frame.to_string(index=False))


def cmd_status(args) -> int:
    from .db import table_counts

    cfg = settings()
    print(f"database        {cfg.db_path}")
    print(f"cfbd key        {'set' if cfg.has_cfbd else 'MISSING'}")
    print(f"odds api key    {'set' if cfg.has_odds_api else 'MISSING'}")
    print(f"seasons         {season_range()[0]}-{season_range()[1]}")
    print()
    for table, count in table_counts().items():
        print(f"{table:<20} {count:>12,}")
    return 0


def cmd_ingest(args) -> int:
    from .ingest import DATASETS, ingest_seasons

    seasons = _parse_seasons(args.seasons)
    datasets = tuple(args.datasets.split(",")) if args.datasets else DATASETS
    report = ingest_seasons(
        seasons, datasets, skip_plays_for_complete_seasons=not args.force_plays
    )
    print(report.summary())
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    return 0 if report.ok else 1


def cmd_odds(args) -> int:
    from .ingest import refresh_live_odds

    rows = refresh_live_odds()
    print(f"stored {rows} live odds rows")
    return 0


def cmd_features(args) -> int:
    from .db import cursor
    from .features.build import build_team_game, fit_fp_curve

    seasons = _parse_seasons(args.seasons)
    with cursor() as conn:
        if args.refit_curve:
            curve = fit_fp_curve(conn, fit_key="global")
            print(f"refit field-position curve over {len(curve)} buckets")
        rows = build_team_game(conn, seasons)
    print(f"built team_game: {rows} rows")
    return 0


def _model_params(args) -> dict:
    params: dict = {}
    if args.half_life is not None:
        params["half_life_days"] = args.half_life
    if args.ridge_lambda is not None:
        params["ridge_lambda"] = args.ridge_lambda
    if args.fp_half_life is not None:
        params["fp_half_life_days"] = args.fp_half_life
    if args.fp_lambda is not None:
        params["fp_ridge_lambda"] = args.fp_lambda
    for pair in args.param or []:
        key, _, value = pair.partition("=")
        try:
            params[key] = float(value)
        except ValueError:
            params[key] = value
    return params


def cmd_backtest(args) -> int:
    from .backtest import BacktestConfig, run_backtest

    seasons = _parse_seasons(args.seasons)
    config = BacktestConfig(
        model=args.model,
        params=_model_params(args),
        first_season=min(seasons) if seasons else None,
        last_season=max(seasons) if seasons else None,
        provider=args.provider,
        edge_threshold=args.threshold,
        price=args.price,
        bet_line=args.bet_line,
        min_train_games=args.min_train_games,
        label=args.label or "",
    )
    result = run_backtest(config, persist=not args.no_persist)
    metrics = result["metrics"]

    print(f"run {metrics['run_id']}  model={metrics['model']}")
    print(f"  params            {metrics['params']}")
    print(f"  games evaluated   {metrics['games_evaluated']:,}")
    print(f"  bets              {metrics['bets']:,} at |edge| >= {config.edge_threshold}")
    print(
        f"  record            {metrics['wins']}-{metrics['losses']}-"
        f"{metrics['pushes']}  ({metrics['win_rate']:.1%}, "
        f"breakeven {metrics['breakeven_rate']:.1%})"
    )
    print(
        f"  roi               {metrics['roi']:+.2%}  "
        f"[95% CI {metrics['roi_ci_low']:+.2%} .. {metrics['roi_ci_high']:+.2%}]"
    )
    print(f"  profit            {metrics['profit_units']:+.2f} units")
    print(
        f"  mae model/market  {metrics['mae_model']} / {metrics['mae_market']} points"
    )
    if "clv_pts_mean" in metrics:
        print(
            f"  closing line val  {metrics['clv_pts_mean']:+.2f} pts, "
            f"{metrics['clv_positive_rate']:.1%} positive"
        )
    verdict = (
        "the interval clears zero — this survived the sample"
        if metrics["beats_breakeven"]
        else "the interval includes zero — indistinguishable from noise"
    )
    print(f"  verdict           {verdict}")

    if args.detail:
        print("\nby edge bucket (all games, not just bets):")
        import pandas as pd

        _print_table(pd.DataFrame(metrics["by_edge_bucket"]))
        print("\nby season:")
        _print_table(pd.DataFrame(metrics["by_season"]))
    return 0


def cmd_sweep(args) -> int:
    from .backtest import BacktestConfig, run_sweep
    from .models.weighting import Sweep

    seasons = _parse_seasons(args.seasons)
    config = BacktestConfig(
        model=args.model,
        params=_model_params(args),
        first_season=min(seasons) if seasons else None,
        last_season=max(seasons) if seasons else None,
        provider=args.provider,
        edge_threshold=args.threshold,
        price=args.price,
        bet_line=args.bet_line,
        min_train_games=args.min_train_games,
    )
    half_lives = (
        tuple(float(x) for x in args.half_lives.split(","))
        if args.half_lives
        else Sweep().half_lives
    )
    sweep = Sweep(half_lives=half_lives, include_windows=not args.no_windows)
    frame = run_sweep(config, sweep, persist=args.persist)
    _print_table(frame)
    print(
        "\nRead the shape, not the maximum: a real effect is a smooth hill "
        "across neighbouring half-lives.\n"
        "A single spike surrounded by flat ground is overfitting to this sample."
    )
    return 0


def cmd_edges(args) -> int:
    from .edges import EdgeConfig, best_price_per_game, compute_edges

    config = EdgeConfig(
        model=args.model or "",
        params=_model_params(args) or None,
        threshold=args.threshold or 0.0,
        bankroll=args.bankroll,
        days_ahead=args.days,
    )
    edges = compute_edges(config)
    if edges.empty:
        print("No upcoming games with prices. Run `gridiron odds` first.")
        return 0

    best = best_price_per_game(edges)
    if best.empty:
        print(
            f"No game clears the {config.threshold or settings().default_edge_threshold} "
            "point threshold. That is the normal outcome most weeks."
        )
        return 0

    columns = [
        "kickoff", "away_team", "home_team", "bet_team", "book", "bet_spread",
        "price", "model_margin", "market_margin", "edge", "model_prob",
        "ev_per_unit", "stake", "shop_gain_pts",
    ]
    _print_table(best[[c for c in columns if c in best.columns]])
    print(f"\nsigma {edges['sigma'].iloc[0]:.2f} pts; stakes are quarter-Kelly on a "
          f"{config.bankroll:,.0f} bankroll.")
    return 0


def cmd_research(args) -> int:
    from pathlib import Path

    from .db import cursor
    from .research import evaluate as ev
    from .research import registry

    if args.action == "status":
        state = registry.summary()
        print(f"search seasons   {state['search_seasons']}")
        print(f"holdout seasons  {state['holdout_seasons']}  (locked)")
        print(f"backtest trials  {state['backtest_trials']}")
        print(
            f"significance bar |t| >= {state['required_t_now']} "
            f"(noise alone reaches {state['expected_max_t_under_null']} "
            f"over this many looks)"
        )
        print(f"hypotheses       {state['by_status'] or 'none yet'}")
        for stage, counts in state["by_stage"].items():
            print(f"  {stage:<12} {counts['trials']:>4} trials, {counts['passed']:>3} passed")
        return 0

    if args.action == "list":
        with cursor() as conn:
            frame = conn.execute(
                "SELECT hypothesis_id, status, name, source, created_at "
                "FROM research_hypotheses ORDER BY created_at DESC LIMIT 50"
            ).df()
        _print_table(frame)
        return 0

    if args.action == "add":
        if not (args.name and args.mechanism and args.sql_file):
            print("add needs --name, --mechanism and --sql-file", file=sys.stderr)
            return 2
        hypothesis = registry.Hypothesis(
            name=args.name,
            mechanism=args.mechanism,
            sql=Path(args.sql_file).read_text(),
        )
        registry.record_hypothesis(hypothesis)
        print(f"recorded {hypothesis.hypothesis_id}  {hypothesis.name}")
        return 0

    if args.action == "holdout":
        if not args.hypothesis_id:
            print("holdout needs --id", file=sys.stderr)
            return 2
        result = ev.holdout(
            args.hypothesis_id, model=args.model, edge_threshold=args.edge
        )
        print(f"{result['name']}  seasons {result['seasons']}")
        print(f"  bets {result['bets']:,}  roi {result['roi']:+.2%}  t {result['t']:+.2f}")
        print(f"  95% CI {result['roi_ci'][0]:+.2%} .. {result['roi_ci'][1]:+.2%}")
        print(f"  {result['outcome']}")
        print("  the holdout for this hypothesis is now spent")
        return 0

    if args.action == "evaluate":
        if not args.hypothesis_id:
            print("evaluate needs --id", file=sys.stderr)
            return 2
        with cursor() as conn:
            row = conn.execute(
                "SELECT hypothesis_id, name, mechanism, expected_sign, sql, source "
                "FROM research_hypotheses WHERE hypothesis_id = ?",
                [args.hypothesis_id],
            ).fetchone()
        if row is None:
            print(f"no hypothesis {args.hypothesis_id}", file=sys.stderr)
            return 1
        hypothesis = registry.Hypothesis(
            hypothesis_id=row[0], name=row[1], mechanism=row[2],
            expected_sign=row[3], sql=row[4], source=row[5],
        )
        _print_evaluation(ev.evaluate(
            hypothesis, stage_limit=args.stop_at, model=args.model,
            edge_threshold=args.edge,
        ))
        return 0

    # propose
    from .research import propose as pr

    for i in range(max(1, args.count)):
        try:
            hypothesis = pr.propose(hint=args.hint)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        registry.record_hypothesis(hypothesis)
        print(f"\n=== {hypothesis.name}  [{hypothesis.hypothesis_id}] ===")
        print(f"mechanism: {hypothesis.mechanism}")
        print(f"\n{hypothesis.sql.strip()}\n")
        try:
            _print_evaluation(ev.evaluate(
                hypothesis, stage_limit=args.stop_at, model=args.model,
                edge_threshold=args.edge,
            ))
        except Exception as exc:  # noqa: BLE001 — one bad idea must not stop the run
            registry.set_status(hypothesis.hypothesis_id, "rejected")
            registry.record_trial(
                hypothesis.hypothesis_id, "persistence", seasons=[],
                passed=False, statistic=None, note=str(exc)[:400],
            )
            print(f"  rejected: {exc}")
    return 0


def _print_evaluation(report: dict) -> None:
    for stage, data in report.get("stages", {}).items():
        mark = "pass" if data.get("passed") else "FAIL"
        if stage == "persistence":
            print(f"  [{mark}] persistence   split-half r={data['split_half_r']:+.3f} "
                  f"over {data['team_seasons']} team-seasons")
        elif stage == "market":
            print(f"  [{mark}] market test   t={data.get('t', 0):+.2f} "
                  f"on {data.get('games', 0):,} games")
        elif stage == "backtest":
            c = data["correction"]
            print(f"  [{mark}] backtest      {data['bets']:,} bets, roi "
                  f"{data['roi']:+.2%}, t={data['t']:+.2f}")
            print(f"         bar |t| >= {c['required_t']} after {c['trials_so_far']} looks")
    print(f"  -> {report.get('outcome', '')}")


def cmd_analyze(args) -> int:
    import pandas as pd

    from . import analysis

    if args.topic == "brand-premium":
        result = analysis.brand_premium(args.provider)
        if "error" in result:
            print(result["error"])
            return 1
        print(
            f"brand premium: {result['premium_pts_per_sd']:+.3f} points per SD of "
            f"prestige (se {result['se']:.3f}, t {result['t']:+.2f}, "
            f"n {result['games']:,})"
        )
        print(f"\n{result['interpretation']}\n")
        print("by season (the NIL drift):")
        _print_table(pd.DataFrame(result["by_season"]))
        print("\nby prestige matchup:")
        _print_table(pd.DataFrame(result["by_prestige_bucket"]))

    elif args.topic == "persistence":
        result = analysis.hidden_yardage_persistence()
        if "error" in result:
            print(result["error"])
            return 1
        _print_table(pd.DataFrame(result["metrics"]))
        print(f"\n{result['interpretation']}")

    elif args.topic == "market-test":
        result = analysis.hidden_yardage_market_test(args.provider)
        if "error" in result:
            print(result["error"])
            return 1
        print(
            f"coefficient {result['coefficient']:+.4f} "
            f"(se {result['se']:.4f}, t {result['t']:+.2f}, n {result['games']:,})"
        )
        print(f"\n{result['interpretation']}")

    elif args.topic == "portal":
        _print_table(analysis.portal_and_prestige())

    elif args.topic == "fp-curve":
        _print_table(analysis.fp_curve_table(), max_rows=25)

    return 0


def cmd_models(args) -> int:
    from .features import registered_blocks
    from .models import registered_models

    print("models:")
    for name, description in sorted(registered_models().items()):
        print(f"  {name:<16} {description}")
    print("\nfeature blocks:")
    for name, block in sorted(registered_blocks().items()):
        print(f"  {name:<16} {block.description}")
        print(f"  {'':<16} -> {', '.join(block.columns)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from . import scheduler
    from .web.app import create_app

    cfg = settings()
    if not args.no_refresh:
        scheduler.start(
            odds_interval_minutes=args.odds_interval,
            data_interval_hours=args.data_interval,
        )
    uvicorn.run(
        create_app(),
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        access_log=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridiron", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="row counts and configuration").set_defaults(
        func=cmd_status
    )

    ingest = subparsers.add_parser("ingest", help="pull data from CFBD")
    ingest.add_argument("--seasons", help='e.g. "2015-2026" or "2023,2024"')
    ingest.add_argument("--datasets", help="comma-separated subset")
    ingest.add_argument(
        "--force-plays",
        action="store_true",
        help="re-download play-by-play for seasons already complete",
    )
    ingest.set_defaults(func=cmd_ingest)

    subparsers.add_parser("odds", help="poll live prices").set_defaults(func=cmd_odds)

    features = subparsers.add_parser("features", help="rebuild derived features")
    features.add_argument("action", choices=["build"])
    features.add_argument("--seasons")
    features.add_argument(
        "--refit-curve",
        action="store_true",
        help="refit the field-position expected-points curve first",
    )
    features.set_defaults(func=cmd_features)

    def add_model_args(sub):
        sub.add_argument("--model", default=settings().default_model)
        sub.add_argument("--half-life", type=float)
        sub.add_argument("--ridge-lambda", type=float)
        sub.add_argument("--fp-half-life", type=float)
        sub.add_argument("--fp-lambda", type=float)
        sub.add_argument(
            "--param", action="append", metavar="KEY=VALUE", help="any other model param"
        )
        sub.add_argument(
            "--min-train-games",
            type=int,
            default=MIN_TRAIN_GAMES,
            metavar="N",
            help=(
                "skip weeks with fewer than N finished games to learn from "
                f"(default {MIN_TRAIN_GAMES}). Lower it only when testing on a "
                "deliberately small slice — a fit under a couple of hundred "
                "games is confident nonsense"
            ),
        )

    backtest = subparsers.add_parser("backtest", help="walk-forward backtest")
    add_model_args(backtest)
    backtest.add_argument("--seasons")
    backtest.add_argument("--provider", default="consensus")
    backtest.add_argument("--threshold", type=float, default=settings().default_edge_threshold)
    backtest.add_argument("--price", type=int, default=-110)
    backtest.add_argument("--bet-line", choices=["close", "open"], default="close")
    backtest.add_argument("--label")
    backtest.add_argument("--detail", action="store_true")
    backtest.add_argument("--no-persist", action="store_true")
    backtest.set_defaults(func=cmd_backtest)

    sweep = subparsers.add_parser(
        "sweep", help="backtest across a ladder of recency settings"
    )
    add_model_args(sweep)
    sweep.add_argument("--seasons")
    sweep.add_argument("--provider", default="consensus")
    sweep.add_argument("--threshold", type=float, default=settings().default_edge_threshold)
    sweep.add_argument("--price", type=int, default=-110)
    sweep.add_argument("--bet-line", choices=["close", "open"], default="close")
    sweep.add_argument("--half-lives", help="comma-separated days, e.g. 90,180,365")
    sweep.add_argument("--no-windows", action="store_true")
    sweep.add_argument("--persist", action="store_true")
    sweep.set_defaults(func=cmd_sweep)

    edges = subparsers.add_parser("edges", help="model vs the books, right now")
    add_model_args(edges)
    edges.add_argument("--threshold", type=float)
    edges.add_argument("--bankroll", type=float, default=1000.0)
    edges.add_argument("--days", type=int, default=10)
    edges.set_defaults(func=cmd_edges)

    analyze = subparsers.add_parser("analyze", help="test a theory's mechanism")
    analyze.add_argument(
        "topic",
        choices=["brand-premium", "persistence", "market-test", "portal", "fp-curve"],
    )
    analyze.add_argument("--provider", default="consensus")
    analyze.set_defaults(func=cmd_analyze)

    subparsers.add_parser(
        "models", help="list registered models and feature blocks"
    ).set_defaults(func=cmd_models)

    research = subparsers.add_parser(
        "research", help="automated hypothesis search, with multiple-testing control"
    )
    research.add_argument(
        "action",
        choices=["status", "propose", "add", "list", "evaluate", "holdout"],
    )
    research.add_argument("--hint", default="", help="steer the proposer")
    research.add_argument("--name", help="for `add`")
    research.add_argument("--mechanism", help="for `add`: why this should be true")
    research.add_argument("--sql-file", help="for `add`: file holding the SELECT")
    research.add_argument("--id", dest="hypothesis_id", help="for evaluate/holdout")
    research.add_argument("--model", default="decomposed")
    research.add_argument("--threshold", dest="edge", type=float, default=2.0)
    research.add_argument(
        "--count", type=int, default=1, help="propose and evaluate N in a row"
    )
    research.add_argument(
        "--stop-at", choices=["market", "backtest"], default="backtest",
        help="stop before the stage that consumes the sample",
    )
    research.set_defaults(func=cmd_research)

    serve = subparsers.add_parser("serve", help="run the web UI")
    serve.add_argument("--no-refresh", action="store_true")
    serve.add_argument("--odds-interval", type=int, default=30, metavar="MINUTES")
    serve.add_argument("--data-interval", type=int, default=6, metavar="HOURS")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Third-party chatter at INFO drowns out the progress that matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
