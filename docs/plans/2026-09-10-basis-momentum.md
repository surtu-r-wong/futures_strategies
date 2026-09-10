# 基差动量混合层 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 `cta_carry` 真实引擎里加一条基差动量信号腿，与现有期限结构腿按 λ 混合，
使每日出单在 2021 年后那段不再只依赖 carry。

**Architecture:** 新增纯函数模块 `cta_carry/legreturns.py` 产出主力链与次主力链的逐日
收益；`signals.py` 把它们滚成 `basis_momentum`（严格历史闸 + 月度节拍 + 离池清零），
与 carry 秩权重合成 `blend_weight`，**方向由合成权重的符号决定**；`decision.py` 改读
`blend_weight` 而不再自己重算秩权重。λ 默认 0 ⇒ 现有基线逐值不动。

**Tech Stack:** Python 3.13、pandas、pytest。无新依赖。

**设计文档：** `docs/plans/2026-09-10-basis-momentum-design.md`（口径、两条裁决、验收标准）

---

## 为什么方向必须由合成权重定（先读，否则会写错架构）

现在 `rank_linear` 路径下 `signals.rank_direction = sign(rank_linear_weights)`，
而 `decision.plan_signal_targets` 又独立重算一次 `rank_linear_weights(ready)` 并断言
`sign(raw_weight) == after.direction`。若在 `decision` 里做混合，合成权重的符号可能与
carry 腿给的 `after.direction` 相反 —— 持仓状态说多、权重说空，下游的换月、
`direction_reversal` 判据全会错。

**所以混合必须发生在方向被推导之前**，即 `signals.py` 里。`decision.py` 只负责读。

---

## Task 1: 两条腿的 forward-only 链与逐日收益

**Files:**
- Create: `cta_carry/legreturns.py`
- Test: `tests/test_carry_legreturns.py`

**Step 1: 写失败的测试**

```python
from datetime import date

import pandas as pd

from cta_carry.legreturns import build_leg_returns, forward_only_chain


def _prices(rows):
    return pd.DataFrame(
        rows,
        columns=["trade_date", "product", "contract", "delivery_yyyymm", "close"],
    )


def test_forward_only_chain_refuses_to_step_back_to_an_earlier_delivery():
    picks = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405),
            (date(2024, 1, 3), "RB", "RB2403.SHF", 202403),  # OI 抖回近月
            (date(2024, 1, 4), "RB", "RB2410.SHF", 202410),
        ],
        columns=["trade_date", "product", "contract", "delivery_yyyymm"],
    )
    chain = forward_only_chain(picks)
    assert chain["chain_contract"].tolist() == [
        "RB2405.SHF",
        "RB2405.SHF",  # 不回头
        "RB2410.SHF",
    ]


def test_leg_return_prices_the_contract_held_into_today_not_todays_pick():
    prices = _prices(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405, 100.0),
            (date(2024, 1, 3), "RB", "RB2405.SHF", 202405, 110.0),
            (date(2024, 1, 3), "RB", "RB2410.SHF", 202410, 300.0),
        ]
    )
    chain = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF"),
            (date(2024, 1, 3), "RB", "RB2410.SHF"),  # 今天换月
        ],
        columns=["trade_date", "product", "chain_contract"],
    )
    returns = build_leg_returns(prices, chain)
    row = returns.loc[returns["trade_date"] == date(2024, 1, 3)].iloc[0]
    # 换月当天赚的是昨天那张合约的钱，不是新旧两张的价差
    assert row["leg_return"] == pytest.approx(0.10)


def test_leg_return_is_dropped_when_the_held_contract_has_no_bar_today():
    prices = _prices(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF", 202405, 100.0),
            (date(2024, 1, 3), "RB", "RB2410.SHF", 202410, 300.0),
        ]
    )
    chain = pd.DataFrame(
        [
            (date(2024, 1, 2), "RB", "RB2405.SHF"),
            (date(2024, 1, 3), "RB", "RB2410.SHF"),
        ],
        columns=["trade_date", "product", "chain_contract"],
    )
    returns = build_leg_returns(prices, chain)
    # RB2405 今天没有 K 线 -> 该 product-day 无收益，绝不用别的合约顶上
    assert returns.loc[returns["trade_date"] == date(2024, 1, 3)].empty
```

（文件顶部记得 `import pytest`。）

**Step 2: 跑，确认失败**

Run: `.venv/bin/python -m pytest tests/test_carry_legreturns.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'cta_carry.legreturns'`

**Step 3: 最小实现**

```python
"""Signal-side chain returns for the basis-momentum leg.

The engine sizes positions from real fills; these are a *signal* input and are
built independently: a forward-only contract chain per product, priced close to
close on the contract held into the day.  A day whose held contract has no bar
yields no return -- this module never synthesises a price, and never differences
two different contracts.
"""

import pandas as pd

_CHAIN_COLUMNS = ("trade_date", "product", "chain_contract")
_RETURN_COLUMNS = ("trade_date", "product", "chain_contract", "leg_return")


def forward_only_chain(picks: pd.DataFrame) -> pd.DataFrame:
    """Roll `picks` forward only: a chain never steps to an earlier delivery."""
    if picks.empty:
        return pd.DataFrame(columns=list(_CHAIN_COLUMNS))
    ordered = picks.sort_values(["product", "trade_date"], kind="mergesort")
    rows = []
    for product, sub in ordered.groupby("product", sort=True):
        held_contract = None
        held_delivery = -1
        for trade_date, contract, delivery in zip(
            sub["trade_date"], sub["contract"], sub["delivery_yyyymm"]
        ):
            if pd.isna(contract):
                if held_contract is None:
                    continue
            elif held_contract is None or int(delivery) > held_delivery:
                held_contract, held_delivery = contract, int(delivery)
            rows.append((trade_date, product, held_contract))
    return pd.DataFrame(rows, columns=list(_CHAIN_COLUMNS))


def build_leg_returns(prices: pd.DataFrame, chain: pd.DataFrame) -> pd.DataFrame:
    """Close-to-close return of the contract the chain held into each day."""
    if chain.empty:
        return pd.DataFrame(columns=list(_RETURN_COLUMNS))
    closes = prices.set_index(["trade_date", "contract"])["close"]
    ordered = chain.sort_values(["product", "trade_date"], kind="mergesort").copy()
    ordered["held"] = ordered.groupby("product", sort=False)["chain_contract"].shift(1)
    ordered["previous_date"] = ordered.groupby("product", sort=False)[
        "trade_date"
    ].shift(1)
    held = ordered.loc[ordered["held"].notna()].copy()
    if held.empty:
        return pd.DataFrame(columns=list(_RETURN_COLUMNS))
    today = pd.MultiIndex.from_arrays([held["trade_date"], held["held"]])
    yesterday = pd.MultiIndex.from_arrays([held["previous_date"], held["held"]])
    held["leg_return"] = (
        closes.reindex(today).to_numpy() / closes.reindex(yesterday).to_numpy() - 1.0
    )
    held = held.dropna(subset=["leg_return"])
    return held.loc[:, list(_RETURN_COLUMNS)].reset_index(drop=True)
```

**Step 4: 跑，确认通过**

Run: `.venv/bin/python -m pytest tests/test_carry_legreturns.py -q`
Expected: 3 passed

**Step 5: 提交**

```bash
git add cta_carry/legreturns.py tests/test_carry_legreturns.py
git commit -m "feat(carry): forward-only chain returns for the basis-momentum leg"
```

---

## Task 2: 配置字段

**Files:**
- Modify: `cta_carry/config.py`
- Test: `tests/test_carry_config.py`

**Step 1: 写失败的测试**

```python
def test_basis_momentum_defaults_are_off_so_the_baseline_does_not_move():
    config = CarryConfig()
    assert config.basis_momentum_weight == 0.0
    assert config.basis_momentum_window == 500
    assert config.basis_momentum_min_coverage == 0.9
    assert config.basis_momentum_rebalance == "monthly"


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_basis_momentum_weight_outside_the_unit_interval_is_rejected(weight):
    with pytest.raises(ValueError, match="basis_momentum_weight"):
        CarryConfig(basis_momentum_weight=weight)


def test_unknown_basis_momentum_rebalance_is_rejected():
    with pytest.raises(ValueError, match="basis_momentum_rebalance"):
        CarryConfig(basis_momentum_rebalance="fortnightly")
```

**Step 2: 跑，确认失败**（`TypeError: unexpected keyword argument`）

**Step 3: 实现** —— 在 `CarryConfig` 里加四个字段，照既有注释风格写清"不是调参旋钮"：

```python
    # Weight of the basis-momentum leg in the blended cross-section.  0.0 leaves
    # the carry leg alone, so every existing configuration reproduces exactly.
    # 0.5 is the researched setting: it is the prior no-view split, not a scan
    # winner (the 2021-2026 optimum was 0.75 and was deliberately not taken).
    basis_momentum_weight: float = 0.0
    # Lookback in trading days for cum(main chain) - cum(secondary chain).
    # 500 is an interior optimum under strict history; 750 is worse in both
    # post-2015 eras.  See 2026-09-10-basis-momentum-design.md 1.2.
    basis_momentum_window: int = 500
    # Fraction of the window that must be real observations on *both* legs.
    # Rolling a zero-filled return series silently manufactures a long lookback
    # for a young product and inflated every long window in the research code.
    basis_momentum_min_coverage: float = 0.9
    # Cadence of the basis-momentum leg.  "monthly" re-ranks on the first trade
    # date present in each calendar month and holds in between (the original
    # factor's cadence, and what keeps blended turnover at 13x).  "daily" exists
    # so the cadence can be measured, not as a tuning knob.
    basis_momentum_rebalance: str = "monthly"
```

加校验：`basis_momentum_window` 进 `_POSITIVE_INTEGER_FIELDS`；新增
`_BASIS_MOMENTUM_REBALANCES = frozenset({"monthly", "daily"})` 并在
`__post_init__` 校验 `basis_momentum_weight ∈ [0, 1]`、
`basis_momentum_min_coverage ∈ (0, 1]`、`basis_momentum_rebalance ∈` 上述集合。

**Step 4: 跑通** `.venv/bin/python -m pytest tests/test_carry_config.py -q`

**Step 5: 提交** `feat(carry): basis-momentum configuration, defaulted off`

---

## Task 3: `basis_momentum` 信号列（严格历史闸）

**Files:**
- Modify: `cta_carry/signals.py`
- Test: `tests/test_carry_signals.py`

前置：`build_signals` 的输入 `curve_with_atr` 此时已带
`main_leg_return` / `secondary_leg_return` 两列（Task 6 接线；本任务先在测试里手工造）。

**Step 1: 写失败的测试**

```python
def test_basis_momentum_is_the_gap_between_the_two_chains_cumulative_returns():
    # 主力腿每天 +1%，次主力腿不动，窗口 3 天 -> 1.01^3 - 1 = 0.030301
    curve = _curve_with_leg_returns(
        main=[0.01, 0.01, 0.01],
        secondary=[0.0, 0.0, 0.0],
    )
    config = small_config(basis_momentum_weight=0.5, basis_momentum_window=3,
                          basis_momentum_min_coverage=1.0)
    signals = build_signals(curve, config).signals
    assert signals["basis_momentum"].iloc[-1] == pytest.approx(1.01 ** 3 - 1.0)


def test_basis_momentum_is_not_ready_until_the_window_has_real_observations():
    curve = _curve_with_leg_returns(main=[0.01, 0.01], secondary=[0.0, 0.0])
    config = small_config(basis_momentum_weight=0.5, basis_momentum_window=3,
                          basis_momentum_min_coverage=1.0)
    signals = build_signals(curve, config).signals
    assert not signals["bmom_ready"].any()


def test_a_gap_in_one_leg_does_not_borrow_coverage_from_the_other():
    # 次主力腿缺一天 -> 覆盖度不足 -> 整个 product-day 不参与基差动量排名
    curve = _curve_with_leg_returns(
        main=[0.01, 0.01, 0.01],
        secondary=[0.0, float("nan"), 0.0],
    )
    config = small_config(basis_momentum_weight=0.5, basis_momentum_window=3,
                          basis_momentum_min_coverage=1.0)
    signals = build_signals(curve, config).signals
    assert not signals["bmom_ready"].iloc[-1]
```

`_curve_with_leg_returns` 是本文件内的小 helper，造单品种的 curve 帧（照
`tests/carry_fixtures.py` 的既有风格），必须填齐 `_SIGNAL_COLUMNS` 上游需要的列。

**Step 2-4: 失败 → 实现 → 通过**

实现要点（放在 `build_signals` 里 `price_ma` 那批 MA 之后）：

```python
    window = int(config.basis_momentum_window)
    minimum = max(1, int(round(window * float(config.basis_momentum_min_coverage))))
    grouped = signals.groupby("product", sort=False)
    for source, target in (
        ("main_leg_return", "_main_cum"),
        ("secondary_leg_return", "_secondary_cum"),
    ):
        # log1p sums skip NaN, so min_periods counts real observations only --
        # never fillna(0.0) here, which would pad a young product's window with
        # zeros and hand it a lookback it has not lived through.
        signals[target] = grouped[source].transform(
            lambda values: np.expm1(
                np.log1p(values).rolling(window, min_periods=minimum).sum()
            )
        )
        signals[f"{target}_count"] = grouped[source].transform(
            lambda values: values.notna().rolling(window, min_periods=1).sum()
        )
    signals["basis_momentum"] = signals["_main_cum"] - signals["_secondary_cum"]
    signals["bmom_ready"] = (
        _finite_mask(signals["basis_momentum"])
        & signals["_main_cum_count"].ge(minimum).fillna(False)
        & signals["_secondary_cum_count"].ge(minimum).fillna(False)
    )
```

（`np.expm1(log1p 求和)` 等价于 Π(1+r) − 1，且对缺失天自动跳过。用完把四个下划线
中间列 drop 掉，`_SIGNAL_COLUMNS` 只加 `basis_momentum` 与 `bmom_ready`。）

**Step 5: 提交** `feat(carry): basis-momentum signal column with a strict-history gate`

---

## Task 4: 月度节拍

**Files:** Modify `cta_carry/signals.py`；Test `tests/test_carry_signals.py`

**Step 1: 写失败的测试**

```python
def test_monthly_cadence_re_ranks_on_the_first_trade_date_of_each_month():
    # 1/31 与 2/1 的基差动量排名相反；月度节拍下 1 月剩下的日子必须沿用 1 月初那次
    ...
    assert january_weights_are_constant_within_the_month
    assert february_reranks_on_its_first_trade_date


def test_monthly_cadence_uses_the_first_date_present_not_the_calendar_first():
    # 2 月 1 日不在帧里（覆盖度截断/非交易日）-> 2 月 2 日就是重排日
    ...


def test_daily_cadence_reranks_every_day():
    ...
```

⚠️ 还要一条**直接反手**的用例 —— 见 `cadence-gate-misses-direct-reversal`：
粗节拍下"按跨越零判方向变化"会漏掉多翻空。断言必须落在**权重值**上，不是
"方向是否改变"上：

```python
def test_a_product_that_flips_from_long_to_short_between_rebalances_holds_its_old_side():
    # 月内信号从 +0.4 直接翻到 −0.4，月度节拍下持仓必须仍是 +0.4，
    # 且下一个重排日必须真的翻到 −0.4（不是停在 0）
```

**Step 3: 实现**

```python
def _rebalance_dates(trade_dates: pd.Series, cadence: str) -> set:
    ordered = pd.Series(sorted(pd.unique(trade_dates)))
    if cadence == "daily":
        return set(ordered)
    months = pd.to_datetime(ordered).dt.to_period("M")
    return set(ordered.groupby(months).first())
```

重排日算 `bmom_weight`（在 `bmom_ready` 子集上做 `rank_linear_weights`，
按 `basis_momentum` 升序），非重排日按 product 前向填充。

**Step 5: 提交** `feat(carry): hold the basis-momentum leg between monthly rebalances`

---

## Task 5: 离池清零 + 重新居中

**Files:** Modify `cta_carry/signals.py`；Test `tests/test_carry_signals.py`

设计裁决 A：品种月内掉出流动性池 ⇒ 当日 `bmom_weight = 0`，并对当日**剩余非零**
权重减去其均值，使其精确零和。

**Step 1: 写失败的测试**

```python
def test_a_product_that_leaves_the_pool_loses_its_basis_momentum_leg_the_same_day():
    ...
    assert signals.loc[exited_row, "bmom_weight"] == 0.0


def test_the_surviving_basis_momentum_weights_are_recentred_to_sum_to_zero():
    ...
    assert day["bmom_weight"].sum() == pytest.approx(0.0, abs=1e-12)
```

「掉出池」的判据：该 (trade_date, product) 在 `curve_with_atr` 里没有行
（`build_curve` 对池外品种不产出 curve 行）。所以前向填充**只能在该品种当天有
curve 行时生效**；没有行就没有权重，天然为 0 —— 实现上只要**不要**把权重
reindex 到完整日历网格。写测试锁死这个行为，避免以后有人"顺手补全"。

**Step 5: 提交** `feat(carry): drop the basis-momentum leg on pool exit and recentre`

---

## Task 6: `blend_weight` 与方向

**Files:** Modify `cta_carry/signals.py`, `cta_carry/decision.py`；
Test `tests/test_carry_signals.py`, `tests/test_carry_decision.py`

**Step 1: 写失败的测试**

```python
def test_lambda_zero_leaves_rank_direction_identical_to_the_carry_leg():
    baseline = build_signals(curve, small_config(weighting="rank_linear")).signals
    blended = build_signals(
        curve, small_config(weighting="rank_linear", basis_momentum_weight=0.0)
    ).signals
    pd.testing.assert_frame_equal(
        baseline[["rank_direction", "strength", "effective_direction"]],
        blended[["rank_direction", "strength", "effective_direction"]],
    )


def test_the_blended_weight_decides_the_direction_not_the_carry_rank():
    # carry 说做空、基差动量更强地说做多 -> 合成为正 -> 方向必须是 +1
    signals = build_signals(curve, small_config(
        weighting="rank_linear", basis_momentum_weight=0.5)).signals
    row = signals.loc[signals["product"] == "RB"].iloc[-1]
    assert row["blend_weight"] > 0.0
    assert row["rank_direction"] == 1


def test_plan_signal_targets_sizes_from_the_blended_weight():
    plan = plan_signal_targets(states, signal_rows, config_with_lambda)
    assert plan.raw_weights["RB2410.SHF"] == pytest.approx(
        expected_blend * strength
    )
```

**Step 3: 实现**

`signals.py` 的 `rank_linear` 分支改为：

```python
            carry_weights = rank_linear_weights(ready)
            lam = float(getattr(config, "basis_momentum_weight", 0.0))
            if lam > 0.0:
                blend = (1.0 - lam) * carry_weights + lam * bmom_weights_for_day
            else:
                blend = carry_weights
            signals.loc[ready.index, "blend_weight"] = blend.to_numpy()
            signals.loc[ready.index, "rank_direction"] = (
                np.sign(blend).astype(int).to_numpy()
            )
```

`decision.plan_signal_targets` 的 `rank_linear` 分支改为读 `signal.blend_weight`，
删掉那里对 `rank_linear_weights` 的重算（连同 `ready` 的构造）：

```python
            raw_weights[after.contract] = float(signal.blend_weight) * float(
                signal.strength
            )
```

⚠️ λ=0 时 `blend_weight` 必须与旧路径**逐值相等**（同一函数、同一输入）。
Task 8 的全套件回归是这条的守门人。

**Step 5: 提交** `feat(carry): blend the two legs before the direction is derived`

---

## Task 7: 接线（把链收益送进信号）

**Files:** Modify `cta_carry/decision.py`（`build_daily_research`）；
Test `tests/test_carry_decision.py`

在 `build_daily_research` 里，`curve_with_atr` 组装之后、`build_signals` 之前：

```python
    if float(getattr(config, "basis_momentum_weight", 0.0)) > 0.0:
        curve_with_atr = attach_leg_returns(prices, curve_with_atr)
```

`attach_leg_returns` 放在 `legreturns.py`：从 `curve` 的
`main_contract` / `secondary_contract` 各建一条 forward-only 链（`delivery_yyyymm`
分别取 `main_delivery_yyyymm` / `secondary_delivery_yyyymm`），调 `build_leg_returns`，
把两列 merge 回去。λ=0 时**整条路径不执行**，基线一行代码都不多跑。

测试：一条端到端的 —— λ>0 时 `signals` 帧带 `main_leg_return` 且值正确；
λ=0 时该列不存在。

**Step 5: 提交** `feat(carry): wire chain returns into daily research when blending`

---

## Task 8: 基线回归（守门人）

**Step 1:** `.venv/bin/python -m pytest -q`
Expected: 与 Task 0 记录的基线**同样的通过数**，0 失败。
**Step 2:** golden 夹具逐值比对 —— 若仓里有 `--report` 的黄金产物比对测试，确认未变。
**Step 3:** 提交（若无改动则跳过）。

---

## Task 9: CLI 开关

**Files:** Modify `cta_carry/__main__.py`；Test `tests/test_carry_report_cli.py`

加 `--basis-momentum-weight`、`--basis-momentum-window`、
`--basis-momentum-min-coverage`、`--basis-momentum-rebalance`，
照既有 `--near-leg` / `--weighting` 的写法。测试：四个开关都能从命令行走到
`CarryConfig`，且不传时配置与今天相同。

**Step 5: 提交** `feat(carry): CLI switches for the basis-momentum leg`

---

## Task 10: 变异验证

跑前先清缓存（见 `mutation-runs-need-pycache-clear`：等长变异 + 同秒 mtime 会让
`.pyc` 不失效，假报"测试没抓住"）：

```bash
find . -name __pycache__ -type d -prune -exec rm -rf {} +
```

逐条改坏、确认变红、改回：

| # | 变异 | 必须变红的用例 |
|---|---|---|
| 1 | `forward_only_chain` 允许回退到更早交割月 | Task 1 第一条 |
| 2 | `build_leg_returns` 用当天的 pick 而不是昨天持仓的合约计价 | Task 1 第二条 |
| 3 | 严格历史闸改回 `fillna(0.0)` 后滚动 | Task 3 第三条 |
| 4 | 月度节拍改成"只在方向跨越零时才换" | Task 4 的直接反手用例 |
| 5 | 离池后不清零（沿用上月权重） | Task 5 第一条 |
| 6 | 清零后不重新居中 | Task 5 第二条 |
| 7 | 方向仍取 carry 腿的符号而非合成权重 | Task 6 第二条 |

**七条都必须真的变红。** 有一条没红就说明该用例没有价值，重写它而不是放行。

---

## Task 11: 全历史验收（WSL2）

见 `wsl2-futures-strategies-runbook`：`ssh -p 2223 ghls@100.120.152.1`，2223 不通**必须
再试 2222**；走 2222 时脚本从 stdin 喂，避开引号被 PowerShell 吃掉。

1. λ=0 与 λ=0.5 各跑一遍 2012-01-04 起的全历史，`--exclude-products` 用生产的剔除 A 名单。
2. 对照 `docs/plans/2026-09-10-basis-momentum-design.md` §5 的验收标准。
3. **与研究序列交叉验证前先做 lag 扫描**（025 那次日相关只有 0.105、
   `corr(engine_t, replica_{t−1})` 才 0.87 —— 见 `carry-term-structure-index-status`）。
4. 出单 smoke：`--emit-next-targets --capital 100000000`，48 品种量级、无 0 手。

---

## Task 12: 收尾

- 更新 `docs/ROADMAP.md` 与 `docs/operations/carry-daily-research.md`。
- 用 superpowers:finishing-a-development-branch 决定合并方式。
- ⚠️ 若期间 HEAD 有既有的 ruff format 漂移，**单独开 style 提交**
  （见 `formatting-goes-in-its-own-commit`），先分辨漂移是本次引入还是既有。
