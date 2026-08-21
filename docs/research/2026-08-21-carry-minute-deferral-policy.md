# 推迟策略：定不了价就不执行（2026-08-21）

用户 2026-08-21 拍板：缺分钟的产品日**推迟**，不是取消。

## 先纠正我自己的一处说法

我最初把选项 ① 描述成「视为无信号」。查代码后发现**执行时序不是那样**：

```python
def _candidate_roles_for_date(*, index, dates, active):
    previous = active.get(dates[index - 1], {})   # T-1 的信号
    prior    = active.get(dates[index - 2], {})   # T-2 的信号
```

**T 日执行的是 T-1 与 T-2 的信号**，所以拿掉 T 日的信号不影响 T 日。
而且 `_candidate_roles_for_date` 只决定**取哪些合约的分钟数据**，
真正的执行角色来自 `_execution_roles(before/after: PositionState)` 状态机。

所以正确的落点是**执行侧**：那一天不为该品种取数据、不执行、持仓状态不动。

## 策略

> **一个定不了价的产品日不可交易。当天的计划被丢弃而不是对着没见过的价格执行；
> 持仓原样保留；下一个可交易日用当天的新信号重新决策。**

丢弃而非排队重放，是因为 Carry 每日重新决策：次日的计划已经吸收了新信息，
重放一份隔夜的旧计划反而会执行一个已经过时的决定。

## 三处后果，各自明写

| 位置 | 处理 | 理由 |
|---|---|---|
| `_prepare_candidates` | 不生成候选、不取分钟数据 | 没有数据可取 |
| 执行循环（逐品种） | `states[product] = before`，跳过 | 计划丢弃，持仓不动 |
| 日内 15 分钟 bar 循环 | 跳过，并写 `untradable_product_day_unmonitored` 质量记录 | **当天无法做止损监控**，如实记下而不是假装盯了 |
| `event_prices` 盯市 | 用 `mark_prices`（**最后已观测价**） | 见下 |

### ⚠️ 盯市为什么不用当日日收盘价

这三天的**日线数据是完整的**，用当日官方收盘价盯市听上去很自然。但
`event_prices` 会在**盘中事件时刻**被调用（为别的品种执行时，持仓的这条腿也要标价）。
拿当日收盘价去标一个盘中时刻，是**前视偏差**。

`mark_prices`（最后一次真实观测到的价格）是后视的、安全的：
「没有新价格，所以上一个价格仍然成立」。若连一个更早的标价都没有，
则抛 `untradable_product_day_unmarkable` —— 不猜。

## 一道构造期校验

执行循环只拿得到**品种代码**，拿不到交易所。若两个交易所在同一天登记了同名品种，
跳过就会变得有歧义、**静默地停错品种**。所以构造 `CarryMinuteBacktester` 时直接拒绝：

```text
absent_product_day_ambiguous: A on 2024-02-06 is registered for both SHFE and DCE
```

中国期货目前没有这种碰撞，但这是会静默出错的那类隐患，宁可拒绝也不猜。

## 测试

`test_minute_backtester_defers_a_registered_absent_product_day`：
在合成面板上把品种 A 的某一天登记为缺席，断言

1. 该产品日**从未被查询**（`source.calls` 里没有 `(absent_date, "A")`）
2. 该日**没有 A 的执行行**
3. A 在该日的 `contract` / `direction` / `weight` 与前一日**完全相同**

## 未覆盖的边

- **止损**：该日不做日内止损监控。若那天本会触发止损，回测不会触发它。
  这是「定不了价」的直接后果，已写进质量表，但**会影响回测结果**，评估时要知道。
- 影响范围仅限**全历史**分钟回测；2020-07 → 2026-04 那段不含这 5 天。

## 验证

```text
全量  1 failed, 867 passed   （唯一失败＝基线的缺资产测试，master 上同样红）
ruff check / format          通过，未引入漂移
```
