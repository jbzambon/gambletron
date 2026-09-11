# gambletron

A command-line tool that finds positive-expected-value (+EV) sports bets.

It pulls moneyline (h2h), totals, spreads, and/or player-prop odds from
[The Odds API](https://the-odds-api.com) (v4), treats **Pinnacle** as the
sharp reference book, devigs its lines to estimate fair probabilities, and
flags bets at other ("soft") books — DraftKings, FanDuel, BetMGM, Caesars
(williamhill_us), BetRivers — that carry positive EV against that fair line.

## Requirements

- Python 3.9+ (uses only the standard library — no dependencies to install)
- A free [The Odds API](https://the-odds-api.com) key for live runs (not
  needed for `--demo`)

## Quick start

```bash
# Run the built-in demo (no network call, no API key needed)
python3 ev_finder.py --demo

# Live run against baseball_mlb (the default sport), multiplicative devig
export ODDS_API_KEY=your_key_here
python3 ev_finder.py
```

Sample output:

```
EVENT                               BOOK        MARKET  LINE            ODDS  FV    EV%    QK
Boston Red Sox @ New York Yankees   draftkings  h2h     Boston Red Sox  +125  +114  +5.2%  1.03u
Boston Red Sox @ New York Yankees   fanduel     h2h     Boston Red Sox  +120  +114  +2.8%  0.59u
```

`FV` is Pinnacle's devigged fair value, `EV%` is expected value per unit
staked, and `QK` is the recommended quarter-Kelly stake in units (100 units
= full bankroll).

## Usage

```bash
# Live run against the NBA, power-method devig
python3 ev_finder.py basketball_nba --devig power

# Scan totals (Over/Under) instead of moneyline
python3 ev_finder.py baseball_mlb --markets totals

# Scan spreads (point handicap) instead of moneyline
python3 ev_finder.py baseball_mlb --markets spreads

# Scan all three game markets in one call
python3 ev_finder.py baseball_mlb --markets h2h,totals,spreads

# Scan player props (batter home runs) alongside moneyline
python3 ev_finder.py baseball_mlb --markets h2h,batter_home_runs

# --props adds this sport's default prop markets after the moneyline scan
python3 ev_finder.py baseball_mlb --props

# --props-only skips the moneyline scan entirely
python3 ev_finder.py basketball_nba --props-only

# Prop-only scan, explicit markets, capped at the first 3 events
python3 ev_finder.py baseball_mlb --props-only \
    --markets batter_hits,batter_total_bases --max-events 3

# Pinnacle barely covers some prop markets -- fall back to a multi-book
# consensus fair line instead (mean of every book's devigged price,
# requires >=3 books quoting both sides of the exact line)
python3 ev_finder.py baseball_mlb --props-only --reference consensus
```

Run `python3 ev_finder.py --help` for the full flag reference (`--boost`,
`--book`, `--events`, `--max-events`, `--reference`, etc.).

See [other supported sport keys](https://the-odds-api.com/sports-odds-data/sports-apis.html)
and the [full list of market keys](https://the-odds-api.com/sports-odds-data/betting-markets.html).

## How it works

- **Devig**: strips the bookmaker's vig from a market's implied
  probabilities to estimate the "true" probability of each outcome.
  Two methods are supported: `multiplicative` (normalize probabilities to
  sum to 1) and `power` (solve for an exponent so `sum(p_i ** k) == 1`,
  which shrinks longshot probabilities more than favorites — a better fit
  for some markets' favorite-longshot bias).
- **Reference**: `pinnacle` (default) devigs just Pinnacle's line, on the
  premise that it's the sharpest book in the market. `consensus` instead
  averages every qualifying book's devigged price for the exact same line
  (requiring at least 3 books quoting both sides) — useful for player-prop
  markets where Pinnacle's coverage is thin.
- **EV / Kelly**: expected value per unit staked is computed from the fair
  probability and the soft book's price; stake sizing uses quarter-Kelly by
  default, scaled by any profit boost.

### Totals / spreads / props matching

For totals and spreads, a soft book's line is only compared against the
fair reference when the point (e.g. `8.5`, or `-1.5`) matches exactly —
mismatched points are silently skipped. Player-prop outcomes (e.g. "Aaron
Judge Over 0.5") are matched the same way, keyed on market + player + side
+ point.

### Two request patterns

Game markets (h2h/totals/spreads) come back for the whole day's slate in
one call to `/v4/sports/{sport}/odds`. Player props aren't available on
that endpoint — The Odds API only serves them one event at a time from
`/v4/sports/{sport}/events/{eventId}/odds`. When a prop market is
requested, this script first calls the free `/events` endpoint to get the
day's event IDs, then makes one odds call per event, printing the
estimated request cost before spending it.

### API credit costs

Each odds call costs `markets x regions` requests against your quota.
`--markets h2h,totals,spreads` costs 3 requests for the whole slate in one
call. Player props cost that same rate *per event* — e.g. scanning
`batter_hits,batter_home_runs` (2 markets) across a 15-game MLB slate is
15 × 2 × 1 region = 30 requests. `--max-events` (default 5) caps that while
testing. The Odds API's free tier includes 500 requests/month — budget
your polling frequency accordingly.

## Disclaimer

This tool estimates expected value from publicly posted odds; it does not
guarantee winning bets, and past +EV does not guarantee future returns.
Sports betting is illegal or restricted in some jurisdictions — check your
local laws, and bet only what you can afford to lose.

## License

[MIT](LICENSE)
