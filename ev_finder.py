#!/usr/bin/env python3
"""ev_finder.py -- find positive-expected-value sports bets.

Uses The Odds API (https://the-odds-api.com, v4) to pull moneyline (h2h),
totals, and/or spreads odds for a sport, treats Pinnacle as the sharp
reference book, devigs its lines to estimate fair probabilities, and flags
bets at other ("soft") books that carry positive expected value against
that fair line.

Usage examples
--------------
    # Run the built-in demo (no network call, no API key needed):
    $ python3 ev_finder.py --demo

    # Live run against baseball_mlb (the default sport), multiplicative devig:
    $ export ODDS_API_KEY=your_key_here
    $ python3 ev_finder.py

    # Live run against the NBA, power-method devig:
    $ python3 ev_finder.py basketball_nba --devig power

    # Scan totals (Over/Under) instead of moneyline:
    $ python3 ev_finder.py baseball_mlb --markets totals

    # Scan spreads (point handicap) instead of moneyline:
    $ python3 ev_finder.py baseball_mlb --markets spreads

    # Scan all three markets in one call:
    $ python3 ev_finder.py baseball_mlb --markets h2h,totals,spreads

    # Scan player props (batter home runs) alongside moneyline:
    $ python3 ev_finder.py baseball_mlb --markets h2h,batter_home_runs

    # --props adds this sport's default prop markets after the moneyline
    # scan (baseball_mlb defaults to batter_hits,batter_home_runs,
    # batter_total_bases,pitcher_strikeouts -- see PROP_MARKET_DEFAULTS
    # for every sport's list):
    $ python3 ev_finder.py baseball_mlb --props

    # --props-only skips the moneyline scan entirely:
    $ python3 ev_finder.py basketball_nba --props-only

    # Prop-only scan, explicit markets, capped at the first 3 events:
    $ python3 ev_finder.py baseball_mlb --props-only \
        --markets batter_hits,batter_total_bases --max-events 3

    # Pinnacle barely covers some prop markets -- fall back to a
    # multi-book consensus fair line instead (mean of every book's
    # devigged price, requires >=3 books quoting both sides of the
    # exact line):
    $ python3 ev_finder.py baseball_mlb --props-only --reference consensus

    # See other supported sport keys:
    # https://the-odds-api.com/sports-odds-data/sports-apis.html

    # See the full list of market keys (game markets and player props):
    # https://the-odds-api.com/sports-odds-data/betting-markets.html

Totals/spreads/props note
--------------------------
For the totals (Over/Under) and spreads (point handicap) markets, a soft
book's line is only compared against the fair reference when the point
(e.g. 8.5, or -1.5) matches exactly -- quoting different lines isn't a
valid apples-to-apples devig comparison, so mismatched points are
silently skipped. Player-prop outcomes (e.g. "Aaron Judge Over 0.5") are
matched the same way, but keyed on market + player + side + point, so a
soft book's line on a given player only compares against the fair line
for that same player at that same point.

Any market key that isn't h2h/totals/spreads is treated as a player prop
(batter_hits, batter_home_runs, pitcher_strikeouts, player_points,
player_pass_yds, etc. -- see the market-keys link above, or
PROP_MARKET_DEFAULTS for a curated default per sport). Game markets and
prop markets can be mixed freely in one --markets list; --props/
--props-only are just a convenience for not having to type the defaults.

Two request patterns, one script
---------------------------------
Game markets (h2h/totals/spreads) come back for the whole day's slate in
one call to /v4/sports/{sport}/odds. Player props are not available on
that endpoint -- The Odds API only serves them one event at a time from
/v4/sports/{sport}/events/{eventId}/odds. So when a prop market is
requested, this script first calls the free /events endpoint to get the
day's event IDs, then makes one odds call per event for the requested
prop markets, printing the estimated request cost before spending it.
Requesting props for a sport with more games running naturally costs
more requests than a moneyline scan.

Devig grouping note
--------------------
A market's outcomes are devigged in groups, not all together: h2h/
totals/spreads have exactly one group (the whole market), but a prop
market like batter_home_runs carries many players' Over/Under pairs in
one response -- each player's pair is its own group, devigged
independently of every other player's. Grouping the whole outcomes list
together (as if 20 players' Overs and Unders were 40 sides of one bet)
would silently produce nonsense fair values, so this is not optional.

Credits note
------------
Each odds call (whole-slate /odds or a single-event /events/{id}/odds)
costs `markets x regions` requests against your quota. --markets
h2h,totals,spreads costs 3 requests for the whole slate in one call.
Player props cost that same `markets x regions` rate *per event* -- e.g.
scanning batter_hits,batter_home_runs (2 markets) across a 15-game MLB
slate is 15 x 2 x 1 region = 30 requests. --max-events (default 5) caps
that while testing, and the estimated cost is printed to stderr before
the per-event calls run. The Odds API's free tier includes 500 requests
per month -- budget your polling frequency accordingly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

API_BASE = "https://api.the-odds-api.com/v4"
SHARP_BOOK = "pinnacle"
SOFT_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers"]
ALL_BOOKS = [SHARP_BOOK] + SOFT_BOOKS
SUPPORTED_MARKETS = ["h2h", "totals", "spreads"]
REGIONS = "us"
KELLY_FRACTION = 0.25
MIN_EV = 0.01
MAX_EV = 0.15

# Default player-prop market keys per sport for --props/--props-only when
# --markets doesn't already name explicit prop markets. See
# https://the-odds-api.com/sports-odds-data/betting-markets.html for the
# full catalog -- these are just a sane starting set per sport.
PROP_MARKET_DEFAULTS: Dict[str, List[str]] = {
    "baseball_mlb": [
        "batter_hits",
        "batter_home_runs",
        "batter_total_bases",
        "pitcher_strikeouts",
    ],
    "basketball_nba": ["player_points", "player_rebounds", "player_assists"],
    "americanfootball_nfl": [
        "player_pass_yds",
        "player_rush_yds",
        "player_reception_yds",
    ],
    "icehockey_nhl": ["player_shots_on_goal", "player_goals"],
}

# --reference consensus requires at least this many books quoting both
# sides of the exact same line before it'll produce a fair probability for
# it -- below this, one or two books' vig is too noisy to trust as "fair".
CONSENSUS_MIN_BOOKS = 3


# --------------------------------------------------------------------------
# Odds math helpers
# --------------------------------------------------------------------------


def american_to_decimal(odds: float) -> float:
    """Convert American odds (e.g. +125, -150) to decimal odds (e.g. 2.25, 1.667)."""
    if odds > 0:
        return 1.0 + odds / 100.0
    elif odds < 0:
        return 1.0 + 100.0 / abs(odds)
    else:
        raise ValueError("American odds cannot be 0")


def decimal_to_american(decimal_odds: float) -> int:
    """Convert decimal odds (e.g. 2.25) to American odds (e.g. +125), rounded to an int."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if decimal_odds >= 2.0:
        american = (decimal_odds - 1.0) * 100.0
    else:
        american = -100.0 / (decimal_odds - 1.0)
    return round(american)


def implied_prob(decimal_odds: float) -> float:
    """Implied (vig-included) probability from decimal odds: 1 / decimal_odds."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    return 1.0 / decimal_odds


def devig(probs: List[float], method: str = "multiplicative") -> List[float]:
    """Remove the vig from a list of implied probabilities for one market.

    method="multiplicative": normalize probabilities so they sum to 1.
    method="power": find exponent k such that sum(p_i ** k) == 1 (bisection),
        then return p_i ** k for each outcome. This method shrinks longshot
        probabilities relatively more than favorites, matching the
        empirically observed favorite-longshot bias better than the
        multiplicative method for some markets.
    """
    if not probs:
        raise ValueError("probs must be non-empty")
    if any(p <= 0.0 for p in probs):
        raise ValueError("all probabilities must be > 0")

    if method == "multiplicative":
        total = sum(probs)
        return [p / total for p in probs]

    elif method == "power":
        def total_at_k(k: float) -> float:
            return sum(p ** k for p in probs)

        lo, hi = 0.0, 20.0
        # total_at_k is monotonically decreasing in k for k >= 0 when
        # sum(probs) > 1 (the normal vigged case). Bisect for the root.
        if total_at_k(lo) < 1.0:
            # Book is already under 100% (or exactly), nothing to remove.
            return list(probs)
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if total_at_k(mid) > 1.0:
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2.0
        return [p ** k for p in probs]

    else:
        raise ValueError(f"unknown devig method: {method!r}")


def expected_value(p_fair: float, decimal_odds: float, boost: float = 0.0) -> float:
    """Per-unit EV of a bet: p_fair*(profit) - (1-p_fair), where profit is
    (decimal-1) scaled up by a profit-boost token if any (`boost=0.6` for a
    60%-boosted bet: winnings, not stake, get multiplied by 1.6). Profit
    boosts don't change the fair line, only what you're paid when you win,
    so p_fair should still come from devigging the *unboosted* sharp line."""
    boosted_profit = (decimal_odds - 1.0) * (1.0 + boost)
    return p_fair * boosted_profit - (1.0 - p_fair)


def kelly(
    p_fair: float, decimal_odds: float, boost: float = 0.0, fraction: float = KELLY_FRACTION
) -> float:
    """Fractional Kelly stake as a fraction of bankroll (e.g. 0.0103 = 1.03%).

    f = (b*p - q) / b, where b = (decimal_odds - 1) scaled by any profit
    boost (see expected_value), p = p_fair, q = 1 - p_fair. The result is
    scaled by `fraction` (default quarter-Kelly) and clamped at 0 (never
    recommends staking on a -EV bet).
    """
    b = (decimal_odds - 1.0) * (1.0 + boost)
    p = p_fair
    q = 1.0 - p
    f_full = (b * p - q) / b
    f = fraction * f_full
    return max(0.0, f)


# --------------------------------------------------------------------------
# API fetch
# --------------------------------------------------------------------------


def fetch_odds(
    sport: str, api_key: str, markets: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Fetch odds for `sport` from The Odds API for the given market keys
    (e.g. ["h2h"], ["totals"], or ["h2h", "totals"]). Prints
    remaining-requests header to stderr. Raises on HTTP/network errors."""
    if markets is None:
        markets = ["h2h"]
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
        "bookmakers": ",".join(ALL_BOOKS),
    }
    url = f"{API_BASE}/sports/{sport}/odds?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            remaining = resp.headers.get("x-requests-remaining")
            print(f"x-requests-remaining: {remaining}", file=sys.stderr)
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP error {e.code} from The Odds API: {body}", file=sys.stderr)
        raise
    except urllib.error.URLError as e:
        print(f"Network error contacting The Odds API: {e}", file=sys.stderr)
        raise
    return data


def fetch_events(sport: str, api_key: str) -> List[Dict[str, Any]]:
    """Fetch the list of upcoming/live events (id, teams, commence_time) for
    `sport`, with no odds attached. This is the free (0-quota-cost) first
    step for scanning player-prop markets, which The Odds API only serves
    one event at a time -- see fetch_event_odds."""
    params = {"apiKey": api_key}
    url = f"{API_BASE}/sports/{sport}/events?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP error {e.code} from The Odds API (events): {body}", file=sys.stderr)
        raise
    except urllib.error.URLError as e:
        print(f"Network error contacting The Odds API (events): {e}", file=sys.stderr)
        raise
    return data


def fetch_event_odds(
    sport: str, event_id: str, api_key: str, markets: List[str]
) -> Optional[Dict[str, Any]]:
    """Fetch odds (typically player-prop markets) for a single event.
    Costs `markets x regions` requests against your quota, same rate as
    fetch_odds but charged per event instead of per whole-slate call.

    Returns None (after printing a warning to stderr) instead of raising on
    an HTTP/network error, so one problem event (e.g. a game with no props
    posted yet) doesn't abort the rest of the scan."""
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
        "bookmakers": ",".join(ALL_BOOKS),
    }
    url = (
        f"{API_BASE}/sports/{sport}/events/{event_id}/odds"
        f"?{urllib.parse.urlencode(params)}"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            remaining = resp.headers.get("x-requests-remaining")
            print(f"x-requests-remaining: {remaining}", file=sys.stderr)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(
            f"HTTP error {e.code} from The Odds API (event {event_id}): {body}",
            file=sys.stderr,
        )
        return None
    except urllib.error.URLError as e:
        print(
            f"Network error contacting The Odds API (event {event_id}): {e}",
            file=sys.stderr,
        )
        return None


# --------------------------------------------------------------------------
# Demo payload
# --------------------------------------------------------------------------


def demo_payload() -> List[Dict[str, Any]]:
    """One hardcoded event, three bookmakers (Pinnacle + DraftKings +
    FanDuel -- the third book exists so --reference consensus has the
    CONSENSUS_MIN_BOOKS=3 it needs to produce a fair line at all).

    Pinnacle prices the away team (Red Sox) at a fair value of about +114
    (after devig) with ~2% vig per side; DraftKings posts +125 on the same
    side, a +5.2% EV / 1.03u quarter-Kelly bet under --reference pinnacle
    (the default), or a smaller but still positive ~+2.0% EV under
    --reference consensus (averaging in FanDuel's line too).

    A totals (Over/Under 8.5) market is also included for all three books,
    priced close to fair in both modes, to demonstrate --markets totals
    without producing a +EV totals bet (real books rarely leave that much
    on the table on totals the way the demo moneyline does).

    A spreads (run line, +/-1.5) market is also included: DraftKings posts
    +140 on Red Sox +1.5 against a Pinnacle fair value of +126, a +6.0% EV
    /1.07u quarter-Kelly bet (still +4.5% under consensus).

    A batter_home_runs (player-prop) market is also included, with TWO
    players -- Aaron Judge and Juan Soto -- specifically so the grouped
    devig can be checked: each player's Over/Under pair must be devigged
    against only that player's own two prices, never pooled with the
    other player's, or both players' fair values come out wrong. Pinnacle
    prices Judge Over 0.5 HR at a fair value of about +166; DraftKings
    posts +180 on the same side, a +5.4% EV / 0.75u quarter-Kelly bet
    under --reference pinnacle (~+1.7% under consensus). Soto's line is
    priced with no edge anywhere (a distractor -- if grouping were broken,
    Judge's fair value would shift depending on Soto's prices, or vice
    versa, and it should not)."""
    return [
        {
            "id": "demo1",
            "sport_key": "baseball_mlb",
            "sport_title": "MLB",
            "commence_time": "2026-09-10T23:05:00Z",
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Boston Red Sox", "price": 2.097639},
                                {"name": "New York Yankees", "price": 1.840660},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 8.5, "price": 1.952},
                                {"name": "Under", "point": 8.5, "price": 1.935},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Boston Red Sox", "point": 1.5, "price": 2.20},
                                {"name": "New York Yankees", "point": -1.5, "price": 1.74},
                            ],
                        },
                        {
                            "key": "batter_home_runs",
                            "outcomes": [
                                {
                                    "name": "Over",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 2.60,
                                },
                                {
                                    "name": "Under",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 1.57,
                                },
                                {
                                    "name": "Over",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 3.40,
                                },
                                {
                                    "name": "Under",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 1.36,
                                },
                            ],
                        },
                    ],
                },
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Boston Red Sox", "price": 2.25},
                                {"name": "New York Yankees", "price": 1.80},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 8.5, "price": 1.935},
                                {"name": "Under", "point": 8.5, "price": 1.893},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Boston Red Sox", "point": 1.5, "price": 2.40},
                                {"name": "New York Yankees", "point": -1.5, "price": 1.74},
                            ],
                        },
                        {
                            "key": "batter_home_runs",
                            "outcomes": [
                                {
                                    "name": "Over",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 2.80,
                                },
                                {
                                    "name": "Under",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 1.50,
                                },
                                {
                                    "name": "Over",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 3.20,
                                },
                                {
                                    "name": "Under",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 1.40,
                                },
                            ],
                        },
                    ],
                },
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Boston Red Sox", "price": 2.20},
                                {"name": "New York Yankees", "price": 1.79},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 8.5, "price": 1.94},
                                {"name": "Under", "point": 8.5, "price": 1.90},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Boston Red Sox", "point": 1.5, "price": 2.15},
                                {"name": "New York Yankees", "point": -1.5, "price": 1.72},
                            ],
                        },
                        {
                            "key": "batter_home_runs",
                            "outcomes": [
                                {
                                    "name": "Over",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 2.70,
                                },
                                {
                                    "name": "Under",
                                    "description": "Aaron Judge",
                                    "point": 0.5,
                                    "price": 1.55,
                                },
                                {
                                    "name": "Over",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 3.30,
                                },
                                {
                                    "name": "Under",
                                    "description": "Juan Soto",
                                    "point": 0.5,
                                    "price": 1.38,
                                },
                            ],
                        },
                    ],
                },
            ],
        }
    ]


# --------------------------------------------------------------------------
# Core scan logic
# --------------------------------------------------------------------------


def find_market(bookmaker: Dict[str, Any], market_key: str) -> Optional[Dict[str, Any]]:
    for market in bookmaker.get("markets", []):
        if market.get("key") == market_key:
            return market
    return None


def outcome_key(outcome: Dict[str, Any]) -> Tuple[Any, ...]:
    """Identity of an outcome within a market: name alone for h2h (e.g.
    "New York Yankees"), name+point for totals/spreads (e.g. ("Over", 8.5)),
    and description+name+point for player props (e.g. ("Aaron Judge",
    "Over", 0.5)) -- The Odds API puts the player's name in `description`
    on prop outcomes. A soft book's line is only comparable to Pinnacle's
    when the player, side, and point all match exactly."""
    key: List[Any] = []
    description = outcome.get("description")
    if description is not None:
        key.append(description)
    key.append(outcome["name"])
    point = outcome.get("point")
    if point is not None:
        key.append(point)
    return tuple(key)


def outcome_player(outcome: Dict[str, Any]) -> str:
    """The player a prop outcome belongs to (The Odds API's `description`
    field), or "" for a market with no per-entity breakdown (h2h/totals/
    spreads)."""
    return outcome.get("description") or ""


def outcome_line(outcome: Dict[str, Any]) -> str:
    """The side of the bet, independent of player: "Over 0.5", "Under 8.5",
    or just a team name like "Boston Red Sox" / "Boston Red Sox 1.5" for
    h2h/spreads, which have no `description`."""
    parts = [outcome["name"]]
    point = outcome.get("point")
    if point is not None:
        parts.append(str(point))
    return " ".join(parts)


def devig_group_key(outcome: Dict[str, Any]) -> Any:
    """Which outcomes must be devigged together as one bet's full set of
    sides. `description` is the only thing that distinguishes independent
    bets sharing a single market response: h2h/totals/spreads have no
    `description`, so every outcome shares one group (the whole market, as
    a single two- or three-way bet). A prop market like batter_home_runs
    packs many players' Over/Under pairs into one response -- each
    player's pair is `description`-keyed to its own group, devigged
    independently of every other player's, since a 20-player market isn't
    one 40-way bet."""
    return outcome.get("description")


def devig_market_outcomes(
    outcomes: List[Dict[str, Any]], devig_method: str
) -> Dict[Tuple[Any, ...], float]:
    """Devig a market's outcomes group-by-group (see devig_group_key) and
    return a flat {outcome_key: fair_probability} map across every group.
    A group of fewer than 2 outcomes can't be devigged (there's no vig to
    remove from a single side) and is skipped."""
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for outcome in outcomes:
        groups.setdefault(devig_group_key(outcome), []).append(outcome)

    fair_by_key: Dict[Tuple[Any, ...], float] = {}
    for group_outcomes in groups.values():
        if len(group_outcomes) < 2:
            continue
        vigged_probs = [implied_prob(o["price"]) for o in group_outcomes]
        fair_probs = devig(vigged_probs, method=devig_method)
        for outcome, p in zip(group_outcomes, fair_probs):
            fair_by_key[outcome_key(outcome)] = p
    return fair_by_key


def consensus_fair_probs(
    bookmakers: Dict[str, Dict[str, Any]], market_key: str, devig_method: str
) -> Dict[Tuple[Any, ...], float]:
    """Fair probability per outcome as the mean of every book's devigged
    price for that exact line, requiring at least CONSENSUS_MIN_BOOKS books
    that each quote the *complete* set of sides for that line -- a book
    offering only "Over 0.5" without "Under 0.5" for a player doesn't get a
    vote on that player's line. Existing purely to cope with Pinnacle's
    thin prop coverage; for h2h/totals/spreads (one group, the whole
    market) this just averages whichever books quote the full market."""
    # Canonical side-set per group: which outcome_keys *any* book has
    # attached to this group, so we can tell a book offering the complete
    # line apart from one offering only part of it.
    canonical_keys: Dict[Any, set] = {}
    book_outcomes: List[Tuple[str, List[Dict[str, Any]]]] = []
    for book_key in ALL_BOOKS:
        book = bookmakers.get(book_key)
        if book is None:
            continue
        market = find_market(book, market_key)
        if market is None:
            continue
        outcomes = market.get("outcomes", [])
        book_outcomes.append((book_key, outcomes))
        for outcome in outcomes:
            canonical_keys.setdefault(devig_group_key(outcome), set()).add(
                outcome_key(outcome)
            )

    # Per outcome_key, the list of fair probabilities contributed by books
    # that quoted that outcome's entire group.
    contributions: Dict[Tuple[Any, ...], List[float]] = {}
    for _book_key, outcomes in book_outcomes:
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for outcome in outcomes:
            groups.setdefault(devig_group_key(outcome), []).append(outcome)
        for gkey, group_outcomes in groups.items():
            if {outcome_key(o) for o in group_outcomes} != canonical_keys.get(gkey):
                continue  # partial line -- doesn't get a vote
            if len(group_outcomes) < 2:
                continue
            vigged_probs = [implied_prob(o["price"]) for o in group_outcomes]
            fair_probs = devig(vigged_probs, method=devig_method)
            for outcome, p in zip(group_outcomes, fair_probs):
                contributions.setdefault(outcome_key(outcome), []).append(p)

    fair_by_key: Dict[Tuple[Any, ...], float] = {}
    for key, probs in contributions.items():
        if len(probs) >= CONSENSUS_MIN_BOOKS:
            fair_by_key[key] = sum(probs) / len(probs)
    return fair_by_key


def fair_probabilities(
    bookmakers: Dict[str, Dict[str, Any]],
    market_key: str,
    devig_method: str,
    reference: str = "pinnacle",
) -> Dict[Tuple[Any, ...], float]:
    """Fair probability per outcome_key for one market, per `reference`:
    "pinnacle" devigs just Pinnacle's line (group-by-group -- see
    devig_market_outcomes); "consensus" averages every qualifying book's
    devigged price instead (see consensus_fair_probs), for markets where
    Pinnacle's coverage is too thin to trust alone."""
    if reference == "pinnacle":
        sharp = bookmakers.get(SHARP_BOOK)
        if sharp is None:
            return {}
        sharp_market = find_market(sharp, market_key)
        if sharp_market is None:
            return {}
        return devig_market_outcomes(sharp_market.get("outcomes", []), devig_method)
    elif reference == "consensus":
        return consensus_fair_probs(bookmakers, market_key, devig_method)
    else:
        raise ValueError(f"unknown reference: {reference!r}")


def scan_market(
    bookmakers: Dict[str, Dict[str, Any]],
    market_key: str,
    devig_method: str,
    event_label: str,
    boost: float = 0.0,
    books: Optional[List[str]] = None,
    reference: str = "pinnacle",
) -> List[Dict[str, Any]]:
    """Return a list of +EV bet dicts for one market (e.g. "h2h" or
    "batter_home_runs") within one event, devigged against `reference`
    ("pinnacle" or "consensus" -- see fair_probabilities). `boost` is a
    profit-boost fraction (0.6 = 60%) applied only to the soft books in
    `books` (default: all SOFT_BOOKS) -- pass e.g. books=["draftkings"]
    when the boost token is book-specific. `MAX_EV` is a sanity ceiling
    meant to catch bad data on an unboosted scan; a real boost can
    legitimately push EV past it, so it's skipped whenever boost > 0.

    This is the one code path for every market type -- h2h, totals,
    spreads, and every player prop all devig/score/Kelly-size through the
    same fair_probabilities/expected_value/kelly calls, so there's nothing
    market-specific to keep in sync between an "h2h path" and a "prop
    path"."""
    bets: List[Dict[str, Any]] = []
    if books is None:
        books = SOFT_BOOKS

    fair_by_key = fair_probabilities(bookmakers, market_key, devig_method, reference)
    if not fair_by_key:
        return bets

    for book_key in books:
        book = bookmakers.get(book_key)
        if book is None:
            continue
        market = find_market(book, market_key)
        if market is None:
            continue

        for outcome in market.get("outcomes", []):
            # For totals/spreads, a mismatched point (soft book quoting a
            # different line than Pinnacle) means no key match -- skip it,
            # since it isn't a valid apples-to-apples devig comparison.
            p_fair = fair_by_key.get(outcome_key(outcome))
            if p_fair is None:
                continue
            decimal_odds = outcome["price"]
            ev = expected_value(p_fair, decimal_odds, boost=boost)

            if ev >= MIN_EV and (boost > 0.0 or ev <= MAX_EV):
                stake = kelly(p_fair, decimal_odds, boost=boost)
                bets.append(
                    {
                        "event": event_label,
                        "book": book_key,
                        "market": market_key,
                        "player": outcome_player(outcome),
                        "line": outcome_line(outcome),
                        "odds_american": decimal_to_american(decimal_odds),
                        "fv_american": decimal_to_american(1.0 / p_fair),
                        "ev": ev,
                        "kelly_units": stake * 100.0,
                    }
                )

    return bets


def scan_event(
    event: Dict[str, Any],
    devig_method: str,
    markets: List[str],
    boost: float = 0.0,
    books: Optional[List[str]] = None,
    reference: str = "pinnacle",
) -> List[Dict[str, Any]]:
    """Return a list of +EV bet dicts found in one event, across all
    requested market keys."""
    bookmakers = {bm["key"]: bm for bm in event.get("bookmakers", [])}
    event_label = f"{event.get('away_team')} @ {event.get('home_team')}"

    bets: List[Dict[str, Any]] = []
    for market_key in markets:
        bets.extend(
            scan_market(
                bookmakers,
                market_key,
                devig_method,
                event_label,
                boost=boost,
                books=books,
                reference=reference,
            )
        )
    return bets


def format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def print_table(bets: List[Dict[str, Any]]) -> None:
    if not bets:
        print("No +EV bets found.")
        return

    bets_sorted = sorted(bets, key=lambda b: b["ev"], reverse=True)

    headers = ["EVENT", "BOOK", "MARKET", "PLAYER", "LINE", "ODDS", "FV", "EV%", "QK"]
    rows = []
    for b in bets_sorted:
        rows.append(
            [
                b["event"],
                b["book"],
                b["market"],
                b["player"] or "-",
                b["line"],
                format_american(b["odds_american"]),
                format_american(b["fv_american"]),
                f"+{b['ev'] * 100:.1f}%",
                f"{b['kelly_units']:.2f}u",
            ]
        )

    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    line_fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(line_fmt.format(*headers))
    for r in rows:
        print(line_fmt.format(*r))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find positive-EV sports bets by devigging Pinnacle's line."
    )
    parser.add_argument(
        "sport",
        nargs="?",
        default="baseball_mlb",
        help="The Odds API sport key (default: baseball_mlb)",
    )
    parser.add_argument(
        "--devig",
        choices=["multiplicative", "power"],
        default="multiplicative",
        help="Devig method used to strip vig from Pinnacle's line (default: multiplicative)",
    )
    parser.add_argument(
        "--markets",
        default="h2h",
        help=(
            "Comma-separated market keys to scan (default: h2h). Game "
            f"markets ({','.join(SUPPORTED_MARKETS)}) come back for the "
            "whole slate in one call. Any other key is treated as a "
            "player-prop market (e.g. batter_hits, batter_home_runs, "
            "pitcher_strikeouts, player_points, player_pass_yds) and is "
            "fetched one event at a time -- see --max-events and the "
            "credits note in --help. If --props/--props-only is set and "
            "this is left at its default, the sport's PROP_MARKET_DEFAULTS "
            "list is used instead. Full list: "
            "https://the-odds-api.com/sports-odds-data/betting-markets.html"
        ),
    )
    parser.add_argument(
        "--props",
        action="store_true",
        help=(
            "After the moneyline/game-market scan, also scan player props "
            "-- this sport's PROP_MARKET_DEFAULTS unless --markets already "
            "names explicit prop markets."
        ),
    )
    parser.add_argument(
        "--props-only",
        action="store_true",
        help="Like --props, but skips the game-market scan entirely.",
    )
    parser.add_argument(
        "--reference",
        choices=["pinnacle", "consensus"],
        default="pinnacle",
        help=(
            "Fair-value source to devig against (default: pinnacle). "
            "\"consensus\" averages every book's devigged price for the "
            f"exact same line instead, requiring >= {CONSENSUS_MIN_BOOKS} "
            "books that quote both sides -- useful for prop markets where "
            "Pinnacle's coverage is thin."
        ),
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=5,
        metavar="N",
        help=(
            "Only scan player-prop markets for the first N events returned "
            "by /events (default: 5). Each event costs `markets x regions` "
            "requests, so this caps spend -- the estimated total is "
            "printed to stderr before the calls run."
        ),
    )
    parser.add_argument(
        "--events",
        default=None,
        metavar="EVENT_ID[,EVENT_ID...]",
        help=(
            "Restrict the scan to specific event IDs (from a prior run's "
            "output, or fetch_events()) instead of the whole slate -- "
            "cheaper and avoids the --max-events guesswork when you "
            "already know which game(s) you care about."
        ),
    )
    parser.add_argument(
        "--book",
        default=None,
        metavar="BOOKMAKER_KEY",
        help=(
            "Restrict soft-book scanning to one bookmaker (e.g. draftkings) "
            "instead of all of SOFT_BOOKS -- use with --boost for a "
            "book-specific promo that only applies at that book."
        ),
    )
    parser.add_argument(
        "--boost",
        type=float,
        default=0.0,
        metavar="FRACTION",
        help=(
            "Profit-boost fraction to apply to soft-book payouts before "
            "computing EV (0.6 = a 60%%-boosted bet: winnings, not stake, "
            "are multiplied by 1.6). Fair value still comes from the "
            "unboosted reference line -- a boost changes what you're paid, "
            "not the true probability. Disables the MAX_EV sanity ceiling, "
            "since a real boost can legitimately exceed it."
        ),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Skip the network call and run against a hardcoded demo event",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    raw_markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    if not raw_markets:
        parser.error("--markets must name at least one market key")
    # Game markets (h2h/totals/spreads) are fetched for the whole slate in
    # one call; anything else is treated as a player-prop market key and
    # fetched one event at a time (see fetch_events/fetch_event_odds).
    game_markets = [m for m in raw_markets if m in SUPPORTED_MARKETS]
    prop_markets = [m for m in raw_markets if m not in SUPPORTED_MARKETS]

    if args.props or args.props_only:
        if not prop_markets:
            # --markets was left at its default (or otherwise named no
            # prop keys) -- fall back to this sport's default prop list.
            prop_markets = list(PROP_MARKET_DEFAULTS.get(args.sport, []))
            if not prop_markets:
                parser.error(
                    f"--props/--props-only given but no default prop markets "
                    f"for sport {args.sport!r}; pass --markets explicitly "
                    "(see PROP_MARKET_DEFAULTS for the sports with defaults)"
                )

    if args.props_only:
        game_markets = []

    markets = game_markets + prop_markets
    if not markets:
        parser.error("nothing to scan: no game markets and no prop markets resolved")

    target_event_ids = None
    if args.events:
        target_event_ids = {e.strip() for e in args.events.split(",") if e.strip()}

    books = [args.book] if args.book else None

    events: List[Dict[str, Any]] = []

    if args.demo:
        events = demo_payload()
    else:
        api_key = os.environ.get("ODDS_API_KEY")
        if not api_key:
            print("Error: ODDS_API_KEY environment variable is not set.", file=sys.stderr)
            return 1

        if game_markets:
            try:
                slate = fetch_odds(args.sport, api_key, game_markets)
            except (urllib.error.HTTPError, urllib.error.URLError):
                return 1
            if target_event_ids is not None:
                slate = [e for e in slate if e.get("id") in target_event_ids]
            events.extend(slate)

        if prop_markets:
            if target_event_ids is not None:
                # Skip the free /events call entirely -- we already know
                # which event IDs to hit.
                event_ids = sorted(target_event_ids)
            else:
                try:
                    event_stubs = fetch_events(args.sport, api_key)
                except (urllib.error.HTTPError, urllib.error.URLError):
                    return 1
                event_ids = [stub["id"] for stub in event_stubs][: args.max_events]

            n_regions = REGIONS.count(",") + 1
            estimated_cost = len(event_ids) * len(prop_markets) * n_regions
            print(
                f"Estimated prop-scan cost: {len(event_ids)} events x "
                f"{len(prop_markets)} markets x {n_regions} region(s) = "
                f"{estimated_cost} requests",
                file=sys.stderr,
            )
            for event_id in event_ids:
                event_odds = fetch_event_odds(args.sport, event_id, api_key, prop_markets)
                if event_odds is not None:
                    events.append(event_odds)

    if args.boost:
        print(f"Boost applied: +{args.boost * 100:.0f}% profit boost", file=sys.stderr)
    if args.reference == "consensus":
        print(
            f"Reference: consensus (>= {CONSENSUS_MIN_BOOKS} books per line)",
            file=sys.stderr,
        )

    all_bets: List[Dict[str, Any]] = []
    for event in events:
        all_bets.extend(
            scan_event(
                event,
                args.devig,
                markets,
                boost=args.boost,
                books=books,
                reference=args.reference,
            )
        )

    print_table(all_bets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
