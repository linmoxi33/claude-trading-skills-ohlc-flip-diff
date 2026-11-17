"""Core differential: strict (missing -> not_evaluable) vs zero-fill (missing -> 0).

The upstream project reads price bars with ``bar.get("close", 0)`` in 107
sites across 9 skills (issue #655).  Its own checker
(``scripts/check_price_bar_defaults.py``) is a *static* AST counter: it says
where the default is written, never what it does to a verdict.  This module
answers the behavioural question for a concrete bar series:

* ``strict``   -- an unavailable field anywhere in the rule window makes the
  rule ``not_evaluable`` (reason ``missing_data``), i.e. no verdict is issued.
* ``zerofill`` -- the same field is replaced by ``0.0``, exactly like
  ``dict.get(field, 0)``, and a verdict *is* issued.

Two finding classes come out of that comparison:

* ``candidate_minted``      -- strict has no verdict, zero-fill reports a
  candidate.  This is issue #655's "can flip a trading signal".
* ``masked_not_evaluable``  -- strict has no verdict, zero-fill reports *no*
  candidate.  The output looks like a market result ("0 candidates") although
  nothing could be evaluated; this is issue #711's failure class.

A field is *unavailable* when the key is absent, is ``null``, is NaN/Inf, or
is not numeric.  In ``zerofill`` mode all of those become ``0.0``.
"""

from __future__ import annotations

import json
import math

FIELDS = ("open", "high", "low", "close", "volume")

#: Every rule names the fields it reads and the window it needs, so the
#: omission scan stays honest (we never blame a field the rule cannot see).
RULES = {
    "rsi14-oversold": {"fields": ("close",), "window": 15},
    "sma50-trend": {"fields": ("close",), "window": 50},
    "burst-20pct": {"fields": ("close",), "window": 60},
    "volume-confirm": {"fields": ("volume",), "window": 21},
    "range-expansion": {"fields": ("high", "low", "close"), "window": 2},
}

RULE_NOTES = {
    "rsi14-oversold": "Wilder RSI(14) < 30 -> oversold candidate",
    "sma50-trend": "last close above its own SMA(50) -> trend candidate",
    "burst-20pct": "last close >= 1.2 x close 60 bars ago -> burst candidate",
    "volume-confirm": "last volume > 1.5 x mean of the previous 20",
    "range-expansion": "(high-low) of the last bar > 5% of its close",
}


class InputError(ValueError):
    """Raised for malformed tool input (exit code 2)."""


def _num(value):
    """Return a finite float, or None when the value is unavailable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _mean(values):
    return sum(values) / len(values)


def _rsi(closes, period=14):
    """Wilder RSI over the tail; ``closes`` must hold period+1 values."""
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _verdict(rule, cols):
    if rule == "rsi14-oversold":
        return _rsi(cols["close"]) < 30.0
    if rule == "sma50-trend":
        closes = cols["close"]
        return closes[-1] > _mean(closes)
    if rule == "burst-20pct":
        closes = cols["close"]
        return closes[-1] >= 1.2 * closes[0]
    if rule == "volume-confirm":
        vol = cols["volume"]
        return vol[-1] > 1.5 * _mean(vol[:-1])
    if rule == "range-expansion":
        return (cols["high"][-1] - cols["low"][-1]) > 0.05 * cols["close"][-1]
    raise AssertionError(rule)


def evaluate(rule, bars, zerofill):
    """Return ``(ok, reason)``; ``ok`` is None when no verdict is issued."""
    spec = RULES[rule]
    window = spec["window"]
    if len(bars) < window:
        return None, "insufficient_bars"
    tail = bars[-window:]
    cols = {}
    for field in spec["fields"]:
        column = []
        for bar in tail:
            value = _num(bar.get(field))
            if value is None:
                if not zerofill:
                    return None, "missing_data"
                value = 0.0
            column.append(value)
        cols[field] = column
    return _verdict(rule, cols), None


def _bar_without(bar, field):
    clone = dict(bar)
    clone.pop(field, None)
    return clone


def apply_gaps(bars, gaps):
    """Return a copy of ``bars`` with every declared field removed."""
    out = [dict(bar) for bar in bars]
    for gap in gaps:
        out[gap["index"]] = _bar_without(out[gap["index"]], gap["field"])
    return out


def _classify(strict, zero):
    if strict is not None:
        # A complete window cannot disagree with itself: both paths see the
        # same numbers, so strict verdicts are never findings.
        return None
    return "candidate_minted" if zero else "masked_not_evaluable"


def compare(bars, gaps, rules=None):
    """Compare both consumption modes under one omission set.

    ``rules`` limits the comparison; the scan uses it so that a finding is
    always attributed to the rule whose own window contains the omission.
    """
    gapped = apply_gaps(bars, gaps)
    findings = []
    for rule in (rules if rules is not None else RULES):
        strict, strict_reason = evaluate(rule, gapped, zerofill=False)
        zero, _ = evaluate(rule, gapped, zerofill=True)
        klass = _classify(strict, zero)
        if klass:
            findings.append(
                {
                    "rule": rule,
                    "class": klass,
                    "strict": {"ok": strict, "reason": strict_reason},
                    "zerofill": {"ok": zero},
                    "minimal_witness": sorted(
                        ({"index": g["index"], "field": g["field"]} for g in gaps),
                        key=lambda g: (g["index"], g["field"]),
                    ),
                }
            )
    return findings


def scan(bars, symbol=None):
    """Inject one omission at a time inside each rule window and report flips."""
    findings = []
    tested = 0
    for rule, spec in RULES.items():
        window = spec["window"]
        if len(bars) < window:
            continue
        start = len(bars) - window
        for index in range(start, len(bars)):
            for field in spec["fields"]:
                if _num(bars[index].get(field)) is None:
                    continue  # already absent in the source series
                tested += 1
                gaps = [{"index": index, "field": field}]
                for finding in compare(bars, gaps, rules=[rule]):
                    finding["date"] = bars[index].get("date")
                    findings.append(finding)
    findings.sort(
        key=lambda f: (f["rule"], f["minimal_witness"][0]["index"])
    )
    return {"tested": tested, "findings": findings}


def ddmin(bars, gaps):
    """Shrink an omission set to a 1-minimal set that still flips a signal."""
    gaps = [dict(g) for g in gaps]

    def flips(subset):
        return any(
            f["class"] == "candidate_minted" for f in compare(bars, subset)
        )

    if not flips(gaps):
        return gaps, False
    chunk = max(1, len(gaps) // 2)
    while len(gaps) > 1:
        split = False
        for start in range(0, len(gaps), chunk):
            candidate = gaps[:start] + gaps[start + chunk:]
            if candidate and flips(candidate):
                gaps = candidate
                chunk = max(1, len(gaps) // 2)
                split = True
                break
        if not split:
            if chunk >= len(gaps):
                break
            chunk = min(len(gaps), chunk * 2)
    return gaps, True


def coverage(bars, gaps, rule_names):
    """Issue #711's convention: no candidate must not look like not_evaluable."""
    gapped = apply_gaps(bars, gaps)
    evaluated = 0
    reasons = {}
    for rule in rule_names:
        ok, reason = evaluate(rule, gapped, zerofill=False)
        if ok is None:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            evaluated += 1
    total = len(rule_names)
    if evaluated == total:
        verdict = "complete"
    elif evaluated == 0:
        verdict = "not_evaluable"
    else:
        verdict = "partial"
    return {
        "universe_size": total,
        "evaluated": evaluated,
        "not_evaluated": total - evaluated,
        "not_evaluated_reasons": reasons,
        "verdict": verdict,
    }


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise InputError("invalid JSON in %s: %s" % (path, exc))
    if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
        raise InputError("input must be an object with a 'bars' array")
    bars = payload["bars"]
    if not bars:
        raise InputError("'bars' must not be empty")
    for i, bar in enumerate(bars):
        if not isinstance(bar, dict):
            raise InputError("bars[%d] must be an object" % i)
    gaps = payload.get("gaps") or []
    if not isinstance(gaps, list):
        raise InputError("'gaps' must be an array")
    for gap in gaps:
        if (
            not isinstance(gap, dict)
            or not isinstance(gap.get("index"), int)
            or gap.get("field") not in FIELDS
        ):
            raise InputError(
                "each gap needs an integer 'index' and a 'field' in %s"
                % (", ".join(FIELDS))
            )
        if not 0 <= gap["index"] < len(bars):
            raise InputError("gap index %d is outside the series" % gap["index"])
    return payload, bars, gaps
