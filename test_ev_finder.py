#!/usr/bin/env python3
"""Unit tests for ev_finder.py.

Uses only the standard library (unittest) to match the project's
zero-dependency philosophy -- run with:

    python3 -m unittest test_ev_finder.py -v

No network access is required or performed; everything here exercises pure
functions and the hardcoded --demo payload.
"""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

import ev_finder as ef


# --------------------------------------------------------------------------
# Odds conversion helpers
# --------------------------------------------------------------------------


class TestOddsConversion(unittest.TestCase):
    def test_american_to_decimal_positive(self):
        self.assertAlmostEqual(ef.american_to_decimal(125), 2.25)
        self.assertAlmostEqual(ef.american_to_decimal(100), 2.0)

    def test_american_to_decimal_negative(self):
        self.assertAlmostEqual(ef.american_to_decimal(-150), 1.0 + 100.0 / 150.0)
        self.assertAlmostEqual(ef.american_to_decimal(-100), 2.0)

    def test_american_to_decimal_zero_raises(self):
        with self.assertRaises(ValueError):
            ef.american_to_decimal(0)

    def test_decimal_to_american_round_trip(self):
        # -100 is excluded: decimal 2.0 is the break-even boundary where
        # +100 and -100 are the same price, and decimal_to_american's
        # >= 2.0 branch always returns +100 there -- not a round-trip bug,
        # just an inherent ambiguity at that single point.
        for american in (125, 150, -150, -110, 100):
            decimal = ef.american_to_decimal(american)
            self.assertEqual(ef.decimal_to_american(decimal), american)

    def test_decimal_to_american_invalid_raises(self):
        with self.assertRaises(ValueError):
            ef.decimal_to_american(1.0)
        with self.assertRaises(ValueError):
            ef.decimal_to_american(0.5)

    def test_implied_prob(self):
        self.assertAlmostEqual(ef.implied_prob(2.0), 0.5)
        self.assertAlmostEqual(ef.implied_prob(1.5), 2.0 / 3.0)

    def test_implied_prob_invalid_raises(self):
        with self.assertRaises(ValueError):
            ef.implied_prob(1.0)


# --------------------------------------------------------------------------
# Devig
# --------------------------------------------------------------------------


class TestDevig(unittest.TestCase):
    def test_multiplicative_sums_to_one(self):
        probs = [0.55, 0.52]  # a vigged two-way market
        fair = ef.devig(probs, method="multiplicative")
        self.assertAlmostEqual(sum(fair), 1.0)
        # multiplicative preserves the ratio between outcomes
        self.assertAlmostEqual(fair[0] / fair[1], probs[0] / probs[1])

    def test_power_sums_to_one(self):
        probs = [0.55, 0.30, 0.20]  # a vigged three-way market
        fair = ef.devig(probs, method="power")
        self.assertAlmostEqual(sum(fair), 1.0, places=6)

    def test_power_matches_multiplicative_when_no_vig(self):
        probs = [0.6, 0.4]  # already sums to 1
        fair = ef.devig(probs, method="power")
        # The early-exit in devig() only fires for a single outcome; with
        # 2+ already-fair outcomes it still bisects for k and lands within
        # float tolerance of the input rather than returning it exactly.
        for got, want in zip(fair, probs):
            self.assertAlmostEqual(got, want, places=6)

    def test_power_shrinks_longshots_more_than_multiplicative(self):
        # Favorite-longshot bias: the power method should discount the
        # longshot's fair probability more than straight normalization does.
        probs = [0.80, 0.30]  # heavy favorite + longshot, ~10% vig
        mult = ef.devig(probs, method="multiplicative")
        power = ef.devig(probs, method="power")
        self.assertLess(power[1], mult[1])  # longshot shrinks more
        self.assertGreater(power[0], mult[0])  # favorite shrinks less

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ef.devig([])

    def test_nonpositive_prob_raises(self):
        with self.assertRaises(ValueError):
            ef.devig([0.5, 0.0])
        with self.assertRaises(ValueError):
            ef.devig([0.5, -0.1])

    def test_unknown_method_raises(self):
        with self.assertRaises(ValueError):
            ef.devig([0.5, 0.5], method="bogus")


# --------------------------------------------------------------------------
# EV and Kelly
# --------------------------------------------------------------------------


class TestExpectedValueAndKelly(unittest.TestCase):
    def test_expected_value_matches_demo(self):
        # Pinnacle fair value ~+114 (p ~= 0.4675), DraftKings posts +125.
        p_fair = ef.implied_prob(ef.american_to_decimal(114))
        ev = ef.expected_value(p_fair, ef.american_to_decimal(125))
        self.assertAlmostEqual(ev, 0.0516, places=3)

    def test_expected_value_boost_increases_ev(self):
        p_fair = 0.5
        decimal_odds = 2.0
        unboosted = ef.expected_value(p_fair, decimal_odds)
        boosted = ef.expected_value(p_fair, decimal_odds, boost=0.6)
        self.assertGreater(boosted, unboosted)

    def test_kelly_negative_ev_clamped_to_zero(self):
        # p_fair well below breakeven for these odds -> full Kelly would be
        # negative, but the function must clamp at 0.
        stake = ef.kelly(p_fair=0.3, decimal_odds=1.5)
        self.assertEqual(stake, 0.0)

    def test_kelly_positive_ev_scaled_by_fraction(self):
        p_fair, decimal_odds = 0.55, 2.0
        full = ef.kelly(p_fair, decimal_odds, fraction=1.0)
        quarter = ef.kelly(p_fair, decimal_odds, fraction=0.25)
        self.assertGreater(full, 0.0)
        self.assertAlmostEqual(quarter, full * 0.25)

    def test_kelly_boost_increases_stake(self):
        p_fair, decimal_odds = 0.5, 2.0
        unboosted = ef.kelly(p_fair, decimal_odds)
        boosted = ef.kelly(p_fair, decimal_odds, boost=0.6)
        self.assertGreater(boosted, unboosted)


# --------------------------------------------------------------------------
# Outcome keying / grouping
# --------------------------------------------------------------------------


class TestOutcomeKeying(unittest.TestCase):
    def test_outcome_key_h2h(self):
        outcome = {"name": "Boston Red Sox", "price": 2.1}
        self.assertEqual(ef.outcome_key(outcome), ("Boston Red Sox",))

    def test_outcome_key_totals(self):
        outcome = {"name": "Over", "point": 8.5, "price": 1.9}
        self.assertEqual(ef.outcome_key(outcome), ("Over", 8.5))

    def test_outcome_key_prop(self):
        outcome = {"name": "Over", "description": "Aaron Judge", "point": 0.5, "price": 2.6}
        self.assertEqual(ef.outcome_key(outcome), ("Aaron Judge", "Over", 0.5))

    def test_outcome_player(self):
        self.assertEqual(ef.outcome_player({"name": "Over", "description": "Aaron Judge"}), "Aaron Judge")
        self.assertEqual(ef.outcome_player({"name": "Boston Red Sox"}), "")

    def test_outcome_line(self):
        self.assertEqual(ef.outcome_line({"name": "Boston Red Sox"}), "Boston Red Sox")
        self.assertEqual(ef.outcome_line({"name": "Over", "point": 8.5}), "Over 8.5")

    def test_devig_group_key_separates_players(self):
        judge = {"name": "Over", "description": "Aaron Judge", "point": 0.5}
        soto = {"name": "Over", "description": "Juan Soto", "point": 0.5}
        self.assertNotEqual(ef.devig_group_key(judge), ef.devig_group_key(soto))

    def test_devig_group_key_shared_for_game_markets(self):
        # h2h/totals/spreads outcomes have no `description`, so they share
        # one group (the whole market) regardless of side.
        home = {"name": "New York Yankees"}
        away = {"name": "Boston Red Sox"}
        self.assertEqual(ef.devig_group_key(home), ef.devig_group_key(away))


class TestDevigMarketOutcomesGrouping(unittest.TestCase):
    """Regression test for the grouping bug called out in the source: two
    players' Over/Under pairs in one prop market must devig independently,
    never pooled together."""

    def test_two_players_devig_independently(self):
        outcomes = [
            {"name": "Over", "description": "Aaron Judge", "point": 0.5, "price": 2.60},
            {"name": "Under", "description": "Aaron Judge", "point": 0.5, "price": 1.57},
            {"name": "Over", "description": "Juan Soto", "point": 0.5, "price": 3.40},
            {"name": "Under", "description": "Juan Soto", "point": 0.5, "price": 1.36},
        ]
        fair = ef.devig_market_outcomes(outcomes, "multiplicative")

        judge_over = fair[("Aaron Judge", "Over", 0.5)]
        judge_under = fair[("Aaron Judge", "Under", 0.5)]
        soto_over = fair[("Juan Soto", "Over", 0.5)]
        soto_under = fair[("Juan Soto", "Under", 0.5)]

        # Each player's own pair must sum to 1 -- devigged independently.
        self.assertAlmostEqual(judge_over + judge_under, 1.0)
        self.assertAlmostEqual(soto_over + soto_under, 1.0)

        # Cross-check against devigging Judge's pair in isolation: the
        # result must be identical whether or not Soto's prices are present
        # in the same market response.
        judge_only = ef.devig_market_outcomes(outcomes[:2], "multiplicative")
        self.assertAlmostEqual(judge_only[("Aaron Judge", "Over", 0.5)], judge_over)
        self.assertAlmostEqual(judge_only[("Aaron Judge", "Under", 0.5)], judge_under)

    def test_single_outcome_group_skipped(self):
        # A group with fewer than 2 outcomes can't be devigged.
        outcomes = [{"name": "Over", "description": "Solo Player", "point": 0.5, "price": 2.0}]
        fair = ef.devig_market_outcomes(outcomes, "multiplicative")
        self.assertEqual(fair, {})


# --------------------------------------------------------------------------
# Consensus reference
# --------------------------------------------------------------------------


class TestConsensus(unittest.TestCase):
    def test_requires_min_books(self):
        # Only 2 books quote the full h2h line -- below CONSENSUS_MIN_BOOKS
        # (3), so no fair value should be produced for either outcome.
        bookmakers = {
            "pinnacle": {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "A", "price": 2.0},
                            {"name": "B", "price": 2.0},
                        ],
                    }
                ],
            },
            "draftkings": {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "A", "price": 1.9},
                            {"name": "B", "price": 2.1},
                        ],
                    }
                ],
            },
        }
        fair = ef.consensus_fair_probs(bookmakers, "h2h", "multiplicative")
        self.assertEqual(fair, {})

    def test_partial_line_book_excluded(self):
        # A book quoting only one side of a player's prop line shouldn't
        # get a vote on that line's consensus fair value.
        bookmakers = {
            "pinnacle": {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "batter_home_runs",
                        "outcomes": [
                            {"name": "Over", "description": "Player", "point": 0.5, "price": 2.6},
                            {"name": "Under", "description": "Player", "point": 0.5, "price": 1.57},
                        ],
                    }
                ],
            },
            "draftkings": {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "batter_home_runs",
                        "outcomes": [
                            {"name": "Over", "description": "Player", "point": 0.5, "price": 2.8},
                            {"name": "Under", "description": "Player", "point": 0.5, "price": 1.5},
                        ],
                    }
                ],
            },
            "fanduel": {
                "key": "fanduel",
                "markets": [
                    {
                        "key": "batter_home_runs",
                        "outcomes": [
                            # Only the Over side -- a partial line, must not vote.
                            {"name": "Over", "description": "Player", "point": 0.5, "price": 2.7},
                        ],
                    }
                ],
            },
        }
        fair = ef.consensus_fair_probs(bookmakers, "batter_home_runs", "multiplicative")
        # Only pinnacle + draftkings quoted the complete line -- fanduel's
        # partial quote doesn't push the count to CONSENSUS_MIN_BOOKS (3).
        self.assertEqual(fair, {})


# --------------------------------------------------------------------------
# End-to-end against the --demo payload
# --------------------------------------------------------------------------


class TestDemoPayloadScan(unittest.TestCase):
    def setUp(self):
        self.event = ef.demo_payload()[0]
        self.bookmakers = {bm["key"]: bm for bm in self.event["bookmakers"]}

    def test_h2h_pinnacle_reference(self):
        bets = ef.scan_market(self.bookmakers, "h2h", "multiplicative", "TEST")
        by_book = {b["book"]: b for b in bets}
        self.assertEqual(set(by_book), {"draftkings", "fanduel"})
        self.assertAlmostEqual(by_book["draftkings"]["ev"], 0.0516, places=3)
        self.assertAlmostEqual(by_book["fanduel"]["ev"], 0.0282, places=3)
        self.assertEqual(by_book["draftkings"]["fv_american"], 114)

    def test_h2h_consensus_reference(self):
        bets = ef.scan_market(
            self.bookmakers, "h2h", "multiplicative", "TEST", reference="consensus"
        )
        by_book = {b["book"]: b for b in bets}
        # FanDuel's line is folded into the fair value under consensus, so
        # only DraftKings clears MIN_EV.
        self.assertEqual(set(by_book), {"draftkings"})
        self.assertAlmostEqual(by_book["draftkings"]["ev"], 0.0203, places=3)

    def test_totals_produces_no_bets(self):
        # The demo's totals market is deliberately priced close to fair.
        bets = ef.scan_market(self.bookmakers, "totals", "multiplicative", "TEST")
        self.assertEqual(bets, [])

    def test_spreads_pinnacle_reference(self):
        bets = ef.scan_market(self.bookmakers, "spreads", "multiplicative", "TEST")
        self.assertEqual(len(bets), 1)
        bet = bets[0]
        self.assertEqual(bet["book"], "draftkings")
        self.assertEqual(bet["line"], "Boston Red Sox 1.5")
        self.assertAlmostEqual(bet["ev"], 0.0599, places=3)

    def test_batter_home_runs_grouping_and_ev(self):
        bets = ef.scan_market(self.bookmakers, "batter_home_runs", "multiplicative", "TEST")
        by_book = {b["book"]: b for b in bets}
        self.assertEqual(set(by_book), {"draftkings", "fanduel"})
        judge_dk = by_book["draftkings"]
        self.assertEqual(judge_dk["player"], "Aaron Judge")
        self.assertEqual(judge_dk["line"], "Over 0.5")
        self.assertAlmostEqual(judge_dk["ev"], 0.0542, places=3)
        # Soto is a distractor with no edge -- must never show up as a bet.
        self.assertTrue(all(b["player"] == "Aaron Judge" for b in bets))

    def test_scan_event_all_markets(self):
        bets = ef.scan_event(
            self.event,
            "multiplicative",
            ["h2h", "totals", "spreads", "batter_home_runs"],
        )
        # 2 h2h + 0 totals + 1 spread + 2 prop = 5 total bets.
        self.assertEqual(len(bets), 5)
        markets_found = {b["market"] for b in bets}
        self.assertEqual(markets_found, {"h2h", "spreads", "batter_home_runs"})


# --------------------------------------------------------------------------
# Table formatting (smoke tests)
# --------------------------------------------------------------------------


class TestPrintTable(unittest.TestCase):
    def test_empty_bets(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ef.print_table([])
        self.assertIn("No +EV bets found.", buf.getvalue())

    def test_nonempty_bets_sorted_by_ev_desc(self):
        bets = [
            {
                "event": "A @ B", "book": "fanduel", "market": "h2h", "player": "",
                "line": "A", "odds_american": 120, "fv_american": 114,
                "ev": 0.028, "kelly_units": 0.59,
            },
            {
                "event": "A @ B", "book": "draftkings", "market": "h2h", "player": "",
                "line": "A", "odds_american": 125, "fv_american": 114,
                "ev": 0.052, "kelly_units": 1.03,
            },
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            ef.print_table(bets)
        output = buf.getvalue()
        # DraftKings (higher EV) must be printed before FanDuel.
        self.assertLess(output.index("draftkings"), output.index("fanduel"))

    def test_format_american(self):
        self.assertEqual(ef.format_american(125), "+125")
        self.assertEqual(ef.format_american(-150), "-150")


# --------------------------------------------------------------------------
# CLI (argument parsing + full --demo run via subprocess)
# --------------------------------------------------------------------------


class TestArgParser(unittest.TestCase):
    def test_defaults(self):
        parser = ef.build_arg_parser()
        args = parser.parse_args([])
        self.assertEqual(args.sport, "baseball_mlb")
        self.assertEqual(args.devig, "multiplicative")
        self.assertEqual(args.markets, "h2h")
        self.assertEqual(args.reference, "pinnacle")
        self.assertFalse(args.demo)

    def test_props_only_unsupported_sport_errors(self):
        parser = ef.build_arg_parser()
        args = parser.parse_args(["some_unlisted_sport", "--props-only"])
        # main() should reject this via parser.error() (-> SystemExit(2)),
        # since PROP_MARKET_DEFAULTS has no entry for an unlisted sport and
        # --markets wasn't given explicitly.
        with self.assertRaises(SystemExit) as ctx:
            ef.main(["some_unlisted_sport", "--props-only"])
        self.assertEqual(ctx.exception.code, 2)


class TestDemoCLI(unittest.TestCase):
    """Full-process smoke tests via `python3 ev_finder.py --demo ...` --
    exercises the actual CLI entry point, not just importable functions."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "ev_finder.py", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_demo_h2h_default(self):
        result = self.run_cli("--demo")
        self.assertEqual(result.returncode, 0)
        self.assertIn("draftkings", result.stdout)
        self.assertIn("+125", result.stdout)
        self.assertIn("+5.2%", result.stdout)

    def test_demo_all_markets(self):
        result = self.run_cli("--demo", "--markets", "h2h,totals,spreads,batter_home_runs")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Aaron Judge", result.stdout)
        self.assertNotIn("Juan Soto", result.stdout)  # distractor, no edge

    def test_missing_api_key_without_demo_fails_cleanly(self):
        import os

        env = {k: v for k, v in os.environ.items() if k != "ODDS_API_KEY"}
        result = subprocess.run(
            [sys.executable, "ev_finder.py"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ODDS_API_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()
