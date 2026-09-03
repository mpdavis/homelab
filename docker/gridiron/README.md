# gridiron

College football betting research: test a theory, backtest it honestly, and
compare a model's expected spread against what Fanatics and DraftKings are
actually offering.

The service is built around one claim — that a betting edge has to survive three
separate questions, and most ideas die at the second:

1. **Is the effect real?** Does the metric measure a property of a team, or
   just what happened to it? (`gridiron analyze persistence`)
2. **Does the market already know?** A real football insight that is already in
   the line is worth nothing. (`gridiron analyze market-test`)
3. **Does it survive out of sample?** Walk-forward, with a confidence interval
   rather than a win rate. (`gridiron backtest`, `gridiron sweep`)

Only then does it become a bet (`gridiron edges`).

## The sign convention

Everything in this package is a **home margin in points**. There is exactly one
conversion from book convention, and it lives in `backtest.load_frame`:

```
actual_margin  = home_points - away_points
model_margin   = the model's prediction of that same quantity
market_margin  = -spread          # a book quotes the favourite negative
edge           = model_margin - market_margin
```

A positive edge means the model likes the home side. Getting this backwards is
the single most expensive bug available in this domain, so it is stated once,
here and in `gridiron/__init__.py`, and never re-derived.

## Point-in-time discipline

Leakage is the default failure mode of a backtest, so it is prevented
structurally rather than by care. Every model's `fit()` takes an `as_of`
timestamp and considers only games that had **finished** before it; the
backtester refits at every week boundary using `as_of = min(kickoff)` of that
week's fixtures.

`tests/test_ratings.py` and `tests/test_backtest.py` enforce this two ways: by
counting training rows against the cutoff, and by rewriting every future result
to an absurd scoreline and asserting that earlier predictions do not move by so
much as a floating-point bit.

The one documented exception is the expected-points-by-field-position curve,
which is fitted globally. It is close to a structural constant of the sport
(the value of a drive from your own 25 has moved by a fraction of a point in a
decade), and `fit_fp_curve(as_of=...)` will produce a strict point-in-time
vintage if you would rather not accept even that.

## Recency is a swept parameter, not a constant

The usual approach is a fixed window — "last 20 games" — which has an ugly
property: a game counts fully on Friday and not at all on Saturday, so the
model lurches every week as good and bad results fall off the back.

Instead, `models/weighting.py` weights each training game by an exponential
half-life in **days**, plus a `season_carryover` multiplier per offseason
boundary crossed (a summer is not ordinary time — coordinators, quarterbacks
and portal classes all turn over). Both are swept:

```
gridiron sweep --model decomposed --seasons 2019-2025
```

Read the output as a **shape, not a maximum**. A real effect is a smooth hill
across neighbouring half-lives. A single spike surrounded by flat ground is
overfitting to the sample, and picking it will lose money.

Windows and uniform weighting are still in the sweep, because showing that the
smooth version beats the obvious version is worth more than asserting it.

## The two theories that ship

### Hidden yardage

Yardage that never reaches a stat sheet, in two forms:

- **Field position.** Every drive's starting spot is converted to expected
  points through a fitted curve, so "we start on our 32 and they start on their
  21" becomes a number of points per game (`fp_margin_pts`). A team at +4 was
  handed most of a touchdown before running a play.
- **Negative-play salvage.** `salvage_yards_per_rush` is stuff rate times how
  much *less* ground a team gives up on a stuffed run than the league average.
  This is the back who breaks the first hit and turns an eight-yard loss into a
  one-yard loss — success rate calls both plays a failure, and yards-per-carry
  buries the difference in the average.

The `decomposed` model is this thesis as a model: it splits the scoreboard into
`efficiency_margin + fp_margin_pts`, rates each half separately, and shrinks
field position several times harder because it regresses to the mean faster.
The two halves sum exactly to the margin, so there is no double counting and no
missing term.

### Blue-blood bias

The thesis, stated so it can be wrong: a blue blood's advantage came from better
starters *and* far better depth — the four-star sitting third on Alabama's
bench. NIL and a frictionless portal price that bench seat honestly for the
first time, so the depth advantage leaks out to schools that will start him. If
the market still pays for the brand as though the depth were intact, blue bloods
are overvalued and the teams absorbing that talent are undervalued.

Note that the right move is **not** to add prestige as a model covariate. The
test is to regress *the market's own error* on the prestige gap:

```
gridiron analyze brand-premium
```

That yields points of brand premium per standard deviation of prestige, plus a
per-season series — which is the part that speaks to NIL specifically. If the
premium was substantial through the late 2010s and has shrunk since 2021, that
is the depth advantage being arbitraged away, showing up in the one place it
has to. `gridiron analyze portal` is the mechanism check: are the blue bloods
actually net exporters of transfer talent?

The `market_debias` model turns that measurement into a bettable line: take the
market's number and remove the premium it is measured to carry, re-estimated at
every point in time so a shrinking premium is tracked rather than assumed.

## Commands

```
gridiron status                          # what is loaded
gridiron ingest --seasons 2015-2026      # CFBD history
gridiron features build                  # rebuild team_game
gridiron odds                            # live Fanatics/DraftKings prices
gridiron backtest --model decomposed --seasons 2019-2025 --detail
gridiron sweep    --model decomposed --seasons 2019-2025
gridiron analyze {brand-premium,persistence,market-test,portal,fp-curve}
gridiron edges --threshold 2.5 --bankroll 1000
gridiron models                          # registered models and feature blocks
gridiron serve                           # web UI on :8080
```

`--min-train-games` exists on `backtest` and `sweep` for testing on a
deliberately small slice. Lowering it below a couple of hundred games buys
confident nonsense from the opening Saturday of a sample.

## Adding a theory

Two extension points, both one decorator.

A **feature block** adds columns to `team_game`:

```python
@feature_block("my_idea", columns=["my_metric"], description="...")
def my_idea(conn, seasons=None, **_):
    return conn.execute("SELECT game_id, team, ... AS my_metric FROM ...").df()
```

A **model** turns history into predicted home margins:

```python
@register_model("my_theory", "what it claims")
class MyTheory:
    def fit(self, history, as_of): ...      # respect as_of
    def predict(self, fixtures): ...        # -> np.ndarray of home margins
```

Either is immediately available to `backtest`, `sweep`, `edges` and the web UI.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GRIDIRON_DATA_DIR` | `/data` | DuckDB file lives here |
| `GRIDIRON_CFBD_API_KEY` | — | CollegeFootballData, for all history |
| `GRIDIRON_ODDS_API_KEY` | — | The Odds API, for live prices |
| `GRIDIRON_ODDS_BOOKS` | `fanatics,draftkings` | Odds API book keys |
| `GRIDIRON_FIRST_SEASON` | `2015` | Drive start position is patchy before this |
| `GRIDIRON_DEFAULT_MODEL` | `decomposed` | |
| `GRIDIRON_HALF_LIFE_DAYS` | `240` | Two seasons ago counts a quarter as much |
| `GRIDIRON_RIDGE_LAMBDA` | `12` | |
| `GRIDIRON_EDGE_THRESHOLD` | `2.5` | Points before a game is called a bet |
| `GRIDIRON_PORT` | `8080` | |

Without an Odds API key the edge report falls back to CFBD's stored lines,
which are often stale and never carry Fanatics. It is a consolation, not a
substitute.

## Seeding, and why you cannot just shell in and run the CLI

DuckDB's lock is exclusive. While the server is up it holds the database file,
and a second process cannot open it — **not even with `read_only=True`**. So
`kubectl exec ... gridiron ingest` does not work against a running server, and
the CLI says so rather than surfacing a raw `IOException` naming a pid.

Everything that writes therefore has to be asked of the process that owns the
file:

```
# start an ingest now (backgrounded; poll /api/status for the result)
curl -X POST 'https://gridiron.mpdavis.com/api/refresh?seasons=2015-2026'
curl -s   'https://gridiron.mpdavis.com/api/status' | jq .refresh
```

Usually you do not need to. **An empty database backfills itself**: the first
scheduled data tick (about five minutes after the pod starts) sees no games and
ingests the whole `GRIDIRON_FIRST_SEASON..GRIDIRON_LAST_SEASON` range rather
than the current season. After that, refreshes are incremental — current season
only, every six hours — because that is all a running service needs.

The CLI is for a machine where no server is holding the file: your laptop, or a
pod scaled to zero replicas.

### Knowing it worked

Three signals, in increasing order of how much they actually prove:

| Signal | What it tells you |
|---|---|
| `/api/status` → `refresh.last_ingest_at` and `last_error` | The refresh ran, and whether it ended clean. This is the only place a wedged run shows, since there is no CronJob to inspect. |
| `/status` → coverage by season | Per-season row counts. **A season with games but zero plays is the failure that looks like success**: play-by-play never landed, every hidden-yardage feature is null for it, and it contributes nothing to a backtest while looking present. The page flags it. |
| `gridiron backtest --seasons 2023` returning bets with a sane `mae_market` | The whole pipeline — ingest, features, models — demonstrably worked end to end. Counts can be nonzero and still wrong; this cannot. |

What tells you nothing about the data: the Gatus probe going green. It checks
that the web server returns a 302 from the auth middleware, which it does
perfectly well with an empty database.

## Why one process owns the database

DuckDB is a single-writer engine. That is the right trade here — every query
this service runs is an analytical scan over millions of plays, which a columnar
embedded engine does in the time a round trip to Postgres would take, with no
second pod and no backup story beyond copying one file.

The cost is the deployment shape: ingest runs on a daemon thread *inside* the
web process rather than as a separate CronJob, and the PVC is ReadWriteOnce
local-path rather than NFS (DuckDB's file locking over NFS is not something to
bet a dataset on). The visible downside is that a wedged refresh does not show
up in `kubectl get jobs`, so the `/status` page reports when each refresh last
succeeded and what it last failed with. Check there first.

## Development

**Use Python 3.13** — the version the image ships and CI runs:

```
mise use python@3.13          # or any 3.13 interpreter
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The version matters more than it looks. Warnings are errors here, so a
dependency resolving differently between your interpreter and CI's turns into a
red build rather than a shrug — numpy 2.5 publishes no wheel for 3.11, so a
3.11 venv silently pins an older numpy and passes a suite that fails in CI.
Matching the interpreter is what makes a green run locally mean anything.

Tests run against a synthetic league generated from known parameters
(`tests/synth.py`), which is what lets them assert that a fit *recovers the
right answer* rather than merely that it returns one.
