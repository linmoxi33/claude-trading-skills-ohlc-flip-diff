#!/usr/bin/env python3
"""Tests for ohlc-flip-diff: unit predicates plus behavioural CLI runs."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flipdiff import core  # noqa: E402

CLI = os.path.join(ROOT, "bin", "ohlc-flip-diff")
RISE = os.path.join(ROOT, "examples", "in", "rising-60.json")
GAPS = os.path.join(ROOT, "examples", "in", "declared-gaps.json")
LASTVOL = os.path.join(ROOT, "examples", "in", "last-volume-missing.json")


def run(args):
    proc = subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


def series(closes, volumes=None):
    out = []
    for i, close in enumerate(closes):
        bar = {"date": "2026-01-%02d" % (i + 1), "open": close, "high": close,
               "low": close, "close": close}
        if volumes is not None:
            bar["volume"] = volumes[i]
        out.append(bar)
    return out


class NumericInputs(unittest.TestCase):
    def test_unavailable_forms(self):
        for value in (None, float("nan"), float("inf"), "abc", True, []):
            self.assertIsNone(core._num(value), value)

    def test_numeric_forms(self):
        self.assertEqual(core._num(3), 3.0)
        self.assertEqual(core._num(" 12.5 "), 12.5)

    def test_rsi_extremes(self):
        self.assertEqual(core._rsi([1.0] * 15), 100.0)
        self.assertLess(core._rsi([float(x) for x in range(15, 0, -1)]), 1.0)

    def test_rsi_single_zero_close_mints_oversold(self):
        closes = [100.0 + i for i in range(15)]
        self.assertGreater(core._rsi(closes), 99.0)
        self.assertLess(core._rsi(closes[:-1] + [0.0]), 30.0)


class EvaluateModes(unittest.TestCase):
    def setUp(self):
        self.bars = series([100.0 + i for i in range(60)])

    def test_strict_refuses_missing(self):
        bars = core.apply_gaps(self.bars, [{"index": 59, "field": "close"}])
        ok, reason = core.evaluate("rsi14-oversold", bars, zerofill=False)
        self.assertIsNone(ok)
        self.assertEqual(reason, "missing_data")

    def test_zerofill_issues_verdict(self):
        bars = core.apply_gaps(self.bars, [{"index": 59, "field": "close"}])
        ok, reason = core.evaluate("rsi14-oversold", bars, zerofill=True)
        self.assertIsInstance(ok, bool)
        self.assertIsNone(reason)

    def test_short_series_is_not_missing_data(self):
        ok, reason = core.evaluate("sma50-trend", series([1.0] * 10), False)
        self.assertIsNone(ok)
        self.assertEqual(reason, "insufficient_bars")

    def test_gap_outside_window_is_invisible(self):
        bars = core.apply_gaps(self.bars, [{"index": 1, "field": "close"}])
        ok, reason = core.evaluate("rsi14-oversold", bars, False)
        self.assertIsInstance(ok, bool)
        self.assertIsNone(reason)


class CompareAndScan(unittest.TestCase):
    def setUp(self):
        payload = json.load(open(RISE))
        self.bars = payload["bars"]

    def test_zeroing_first_close_mints_burst(self):
        findings = core.compare(self.bars, [{"index": 0, "field": "close"}])
        by_rule = {f["rule"]: f for f in findings}
        burst = by_rule["burst-20pct"]
        self.assertEqual(burst["class"], "candidate_minted")
        self.assertTrue(burst["zerofill"]["ok"])
        self.assertEqual(burst["minimal_witness"], [{"index": 0, "field": "close"}])

    def test_last_volume_gap_is_masked_not_evaluable(self):
        findings = core.compare(self.bars, [{"index": 59, "field": "volume"}])
        self.assertEqual(
            [f["class"] for f in findings], ["masked_not_evaluable"]
        )

    def test_rule_filter_attribution(self):
        findings = core.compare(
            self.bars, [{"index": 59, "field": "close"}], rules=["rsi14-oversold"]
        )
        self.assertEqual({f["rule"] for f in findings}, {"rsi14-oversold"})

    def test_scan_counts_every_injection_once(self):
        result = core.scan(self.bars)
        expected = sum(
            len(spec["fields"]) * spec["window"] for spec in core.RULES.values()
        )
        self.assertEqual(result["tested"], expected)
        self.assertEqual(len(result["findings"]), expected)
        keys = [
            (f["rule"], f["minimal_witness"][0]["index"],
             f["minimal_witness"][0]["field"])
            for f in result["findings"]
        ]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ddmin_shrinks_and_still_flips(self):
        gaps = [
            {"index": 59, "field": "close"},
            {"index": 0, "field": "close"},
            {"index": 30, "field": "close"},
            {"index": 12, "field": "volume"},
            {"index": 45, "field": "volume"},
        ]
        witness, flips = core.ddmin(self.bars, gaps)
        self.assertTrue(flips)
        self.assertLess(len(witness), len(gaps))
        self.assertTrue(
            any(
                f["class"] == "candidate_minted"
                for f in core.compare(self.bars, witness)
            )
        )

    def test_ddmin_reports_no_flip_sets(self):
        bars = series([100.0 + i for i in range(60)], volumes=[10_000] * 60)
        witness, flips = core.ddmin(bars, [{"index": 5, "field": "volume"}])
        self.assertFalse(flips)
        self.assertEqual(len(witness), 1)


class CoverageBlock(unittest.TestCase):
    def setUp(self):
        self.bars = json.load(open(RISE))["bars"]

    def test_complete_when_nothing_missing(self):
        block = core.coverage(self.bars, [], list(core.RULES))
        self.assertEqual(block["verdict"], "complete")
        self.assertEqual(block["not_evaluated"], 0)

    def test_partial_lists_missing_data_reason(self):
        block = core.coverage(
            self.bars, [{"index": 59, "field": "volume"}], list(core.RULES)
        )
        self.assertEqual(block["verdict"], "partial")
        self.assertEqual(block["not_evaluated_reasons"], {"missing_data": 1})

    def test_not_evaluable_when_every_rule_is_blocked(self):
        gaps = [{"index": 59, "field": "volume"}]
        gaps += [{"index": i, "field": "close"} for i in (0, 1, 30, 59)]
        block = core.coverage(self.bars, gaps, list(core.RULES))
        self.assertEqual(block["verdict"], "not_evaluable")


class Loader(unittest.TestCase):
    def test_rejects_unknown_gap_field(self):
        with self.assertRaises(core.InputError):
            core.load(_write({ "bars": [{"close": 1.0}], "gaps": [{"index": 0, "field": "adjClose"}]}))

    def test_rejects_out_of_range_index(self):
        with self.assertRaises(core.InputError):
            core.load(_write({"bars": [{"close": 1.0}], "gaps": [{"index": 7, "field": "close"}]}))


class CliBehaviour(unittest.TestCase):
    def test_scan_exit_1_and_json_parses(self):
        code, out, _ = run([RISE, "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["tool"], "ohlc-flip-diff")
        self.assertEqual(report["summary"]["omissions_tested"], 152)
        self.assertGreater(report["summary"]["declared"]["verdict_flips"], 0)

    def test_gaps_mode_shrinks_to_one_minimal_witness(self):
        code, out, _ = run([GAPS, "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["mode"], "gaps")
        self.assertEqual(report["summary"]["gaps_declared"], 5)
        self.assertEqual(report["summary"]["gaps_in_minimal_witness"], 1)
        for finding in report["findings"]:
            self.assertEqual(len(finding["minimal_witness"]), 1)

    def test_strict_exit_zero_when_nothing_is_minted(self):
        code, out, _ = run([LASTVOL, "--json", "--strict-exit"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["summary"]["witness"]["verdict_flips"], 0)
        self.assertEqual(report["summary"]["witness"]["masked_not_evaluable"], 1)

    def test_masked_class_exits_1_without_strict_exit(self):
        code, _, _ = run([LASTVOL])
        self.assertEqual(code, 1)

    def test_usage_error_exits_2(self):
        code, _, err = run(["/tmp/definitely-not-here.json"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_malformed_json_exits_2(self):
        path = _write("not json at all", raw=True)
        code, _, err = run([path])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_scan_mode_with_declared_gaps_is_rejected(self):
        code, _, err = run([GAPS, "--mode", "scan"])
        self.assertEqual(code, 2)
        self.assertIn("--mode scan", err)

    def test_offline_behaviour_is_deterministic(self):
        env = dict(os.environ)
        env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        proc = subprocess.run(
            [sys.executable, CLI, RISE, "--json"],
            capture_output=True, text=True, cwd=ROOT, env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["summary"]["omissions_tested"], 152)


def _write(payload, raw=False):
    path = "/tmp/ohlc-flip-diff-test-%d.json" % abs(hash(str(payload))) 
    with open(path, "w", encoding="utf-8") as handle:
        if raw:
            handle.write(payload)
        else:
            json.dump(payload, handle)
    return path


if __name__ == "__main__":
    unittest.main(verbosity=2)
