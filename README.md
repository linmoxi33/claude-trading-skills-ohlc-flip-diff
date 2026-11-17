> ⚠️ **本仓库未通过去重闸门，不计入交付（`runs.jsonl` status=`empty`），保留仅供审计。**
> 上游 `tradermonty/claude-trading-skills` 已被本账号使用两次：
> `claude-trading-skills-failure-map`（2026-11-14，正是 issue #711 的「失败 vs 真空结果不可区分」类）与
> `claude-trading-skills-indicator-parity`（2026-11-16，7 份上游 RSI/EMA 副本的漂移见证）。
> 本工具与前者在 #711 覆盖度那一半功能重合，且上游本身已列入 `state/used-upstreams.txt`（2026-11-15 之前）。
> 请勿把它当作新项目；下一轮应换未用过的上游。

# ohlc-flip-diff

Tell whether a single missing OHLC field **flips a screener verdict** before it
reaches your signals — the runtime question that the upstream's static
site-counter deliberately does not answer.

Upstream: https://github.com/tradermonty/claude-trading-skills

## Why this exists

`tradermonty/claude-trading-skills` reads price bars with
`bar.get("close", 0)` in **107 sites across 9 skills**; a single missing bar
can silently become `0`. That is tracked upstream as
[issue #655](https://github.com/tradermonty/claude-trading-skills/issues/655)
("Missing OHLC values silently become 0 in 9 skills (107 sites) and can flip
signals"), and the repository already ships a checker for it:
[`scripts/check_price_bar_defaults.py`](https://github.com/tradermonty/claude-trading-skills/blob/main/scripts/check_price_bar_defaults.py).

That checker is a **static AST counter**. Its own docstring lists what it
*does not* detect (variable field names, subscripts, longer `or 0` chains) and,
more importantly, it reports **where the default is written — never what it
does to a verdict**. Its ratchet file
[`config/price-bar-default-allowlist.json`](https://github.com/tradermonty/claude-trading-skills/blob/main/config/price-bar-default-allowlist.json)
is a per-site count: "this many remaining, migrate them".

A sibling issue asks for the other half,
[#711](https://github.com/tradermonty/claude-trading-skills/issues/711)
("Report evaluation coverage so 'no candidates' is distinct from 'could not
evaluate'"): a zero-candidate report must not look like a market result. As of
2026-11-17 there is no shared output convention for it — a code search for
`evaluation_coverage universe_size` returns nothing.

`ohlc-flip-diff` closes that behavioural gap: it takes **one concrete bar
series**, runs the same rules under both consumption modes, and prints the
omissions that change or hide a verdict, with a 1-minimal witness.

```
strict   : an unavailable field in the rule window -> no verdict (not_evaluable)
zerofill : the same field -> 0.0, exactly like dict.get(field, 0) -> a verdict
```

## What it does / does not do

Does:

- injects every single-field omission inside each rule window and reports
  `candidate_minted` (strict has no verdict, zero-fill reports a candidate —
  issue #655) and `masked_not_evaluable` (zero-fill reports "no candidate"
  although nothing could be evaluated — issue #711);
- shrinks a declared omission set to a 1-minimal witness with `ddmin`, so the
  reported cause is reproducible on its own;
- emits an `evaluation_coverage` block in the shape issue #711 proposes
  (`universe_size`, `evaluated`, `not_evaluated`,
  `not_evaluated_reasons`, `verdict`);
- runs fully offline on a local JSON file and is deterministic.

Does not:

- run the upstream skills, fetch quotes, or need any API key;
- claim that a specific skill flips. The verdicts come from five small built-in
  rules (RSI(14) oversold, SMA(50) trend, 20% burst, volume confirmation, range
  expansion) that stand in for the *shape* of the failure, not for one skill's
  code.

## Install

Zero dependencies, Python 3.8+:

```bash
git clone git@github.com:linmoxi33/claude-trading-skills-ohlc-flip-diff.git
cd claude-trading-skills-ohlc-flip-diff
python3 bin/ohlc-flip-diff examples/in/rising-60.json
```

## Usage

Input is one JSON object: `bars` (required) and `gaps` (optional). A bar is an
object whose `open`/`high`/`low`/`close`/`volume` keys may be absent, `null`,
non-finite or non-numeric — all four count as *unavailable*. `gaps` selects
fields to remove; with `gaps` the tool minimizes the set, without `gaps` it
scans every single omission (mode is auto-detected, override with `--mode`).

```bash
# scan: which single omissions mint or mask a verdict?
python3 bin/ohlc-flip-diff examples/in/rising-60.json

# declared gaps: shrink 5 declared gaps to a 1-minimal witness
python3 bin/ohlc-flip-diff examples/in/declared-gaps.json

# machine-readable
python3 bin/ohlc-flip-diff examples/in/rising-60.json --json

# exit 0 unless a candidate is actually minted by a missing value
python3 bin/ohlc-flip-diff examples/in/last-volume-missing.json --strict-exit
```

Real run on `examples/in/rising-60.json` (60 bars, no gaps):

```
ohlc-flip-diff 1.0.0  mode=scan  bars=60  symbol=SYN
strict coverage: complete (5/5 evaluated; not_evaluated=0 reasons={})
omissions tested: 152
findings (scan): 152  (candidate_minted=53, masked_not_evaluable=99)
```

Real run on `examples/in/declared-gaps.json` (5 declared gaps): the witness
shrinks to **1** (`close@30`), and the declared-set totals stay visible:

```
strict coverage: not_evaluable (0/5 evaluated; not_evaluated=5 reasons={'missing_data': 5})
gaps declared: 5 -> 1-minimal witness: 1
declared-set findings: candidate_minted=3 masked_not_evaluable=2
findings (witness): 2  (candidate_minted=1, masked_not_evaluable=1)
  sma50-trend   candidate_minted     witness=close@30  zerofill_ok=True  strict=missing_data
  burst-20pct   masked_not_evaluable witness=close@30  zerofill_ok=False strict=missing_data
```

`--json` prints the whole report; it parses with
`python3 -c "import json,sys;json.load(sys.stdin)"`:

```json
{
  "tool": "ohlc-flip-diff", "version": "1.0.0", "mode": "scan", "symbol": "SYN",
  "evaluation_coverage": {"universe_size": 5, "evaluated": 5, "not_evaluated": 0,
                          "not_evaluated_reasons": {}, "verdict": "complete"},
  "summary": {"omissions_tested": 152,
              "declared": {"verdict_flips": 53, "masked_not_evaluable": 99},
              "witness": {}},
  "findings": [{"rule": "burst-20pct", "class": "candidate_minted",
                "strict": {"ok": null, "reason": "missing_data"},
                "zerofill": {"ok": true},
                "minimal_witness": [{"index": 0, "field": "close"}]}]
}
```

### Exit codes

| code | meaning |
| --- | --- |
| 0 | no finding; or only `masked_not_evaluable` findings under `--strict-exit` |
| 1 | at least one `candidate_minted`, or a `masked_not_evaluable` (without `--strict-exit`) |
| 2 | usage or input error (missing file, invalid JSON, bad `gaps` entry, wrong `--mode`) |

## Tests

```bash
python3 tests/test_core.py
```

27 tests, all green: unit coverage of the numeric coercion, Wilder RSI, both
consumption modes, the window/attribution logic, `ddmin`, the coverage block and
the loader, plus CLI behaviour tests that assert exit codes and parse `--json`
through a subprocess (including a run with a dead HTTP proxy to show the core
path never touches the network).

## Limits

- The five rules are models, not the upstream skills' code; a flip here means
  "this shape of omission changes this kind of verdict", not "skill X is wrong".
- `adjClose` is not modelled; a missing `adjClose` is invisible.
- Provider-side renames, 402/403 plan gating and stale-but-present values are
  different failure classes and are out of scope (see issues #707, #799).
- Rules stop at candidate / no candidate; sizing, stops and ordering downstream
  are not simulated.
- The scan is O(rules × window × fields) on a single series — 152 evaluations
  for 60 bars, milliseconds. It is not a backtester.

## 与同类项目的差异

`gh search repos "OHLC missing values signal flip"` and
`gh search repos "price bar validator missing data"` both returned **0**
repositories; `gh search code "price-bar-default-allowlist"` returns only the
upstream itself and its pre-commit config. The nearest thing that exists is the
upstream's own static counter, and the difference is the whole point:

| | upstream `check_price_bar_defaults.py` | ohlc-flip-diff |
| --- | --- | --- |
| input | AST of the skill sources | a concrete bar series |
| output | site count per `(path, scope, field)` | per-rule verdict flip + 1-minimal witness |
| answers | "where is `0` written?" | "does `0` change or hide the verdict?" |
| coverage | none | issue #711's `evaluation_coverage` block |
| network | none | none |

## 本轮创新

榜单项目自己只有一张静态站点计数表（`check_price_bar_defaults.py` + 107 站点的
ratchet allowlist），**它从不说缺失值会不会翻转结论**；本轮首次把这条静默补 0 从
「静态站点计数」推进到「可重放的运行时差分」：对同一份真实 K 线（60 根）注入
152 次单字段缺失，逐条给出 strict（不可评估）与 zerofill（补 0 出结论）的对照，
并把声明的缺失集用 ddmin 缩到 1-minimal witness（5→1），同时按 issue #711 提议的
`evaluation_coverage` 形状区分「真的没有候选」与「根本没评估」。和上游的差异是判定
对象不同：上游数「0 写在哪」，本工具答「0 会不会造出/藏起一个结论」。

## License

MIT — see `LICENSE`.
