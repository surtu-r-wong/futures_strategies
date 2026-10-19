# 回函勘误 01：裁决引用编号与突变覆盖表述

2026-10-19，回应验收方两项跟进（`multi_asset_backtest_db/docs/dispatch/2026-10-17-m02-initialization/futures-response-01-acceptance.md`）。
**已冻结构件（account.config.json / 创世状态）零改动**——其 sha 绑定于账本，勘误只落在
本函与构建脚本注释（脚本改动仅影响未来代际）。

## 1. 滑点费用制口径的完整出处链（验收方跟进 ②）

两处引用各对一半，合并后链条如下：

1. **值（单边 4bp）＝ 用户 2026-10-16 第二轮裁决第 4-2 项**。用户原话（贵方档案
   `docs/dispatch/2026-10-16-m02-rulings-round2/authorization.json` 第 7 行）：
   > “4-1、期货按你列的1.1的方法，股票按万2（注意这些都必须可以设置）。**4-2、滑点4bp。4-3、先按2%加，也要支持设置。**”

   即：佣金＝4-1、**滑点＝4-2**、**保证金+2%＝4-3**；用户答复中没有 4-4。
2. **机制（4bp 记 CNY 费用、不做 tick 取整）＝ 用户 2026-10-18 21:43:46+08 答复本仓
   AskUserQuestion**（问题提出于 2026-10-17 18:57:40+08）：在「维持 4bp 记费用（推荐）」
   与「实现价格调整+tick 取整」之间**选定前者**。问题与答复原文见本仓 transcript
   `~/.claude/projects/-home-elfbob-claude-code-futures-strategies/5aa52c36-7c0e-4704-81bf-1e067dffe214.jsonl`。

**本仓笔误**：构建脚本与 config 注释把滑点引作 "2026-10-16 4-3"、保证金引作 "4-4"——
编号错位（4-3 实为保证金项），且把 10-18 的机制裁决并进了 10-16 的引用串。回函 §1
「2026-10-18 用户裁决」对**机制**是对的。数值与机制本身无误，仅引用编号错。

## 2. 「构建器突变」表述更正（验收方跟进 ①）

回函 §1 所称两条突变（broker 0.1→0.11、冻结 sha 改一位变红）实际作用于
`forward_inputs/m02_fees.py` 及其既有测试，**不是**新构建脚本
`prepare_m02_futures_initialization.py`。验收方实测成立：四个新脚本
（prepare / advance / capture / rehearsal）目前零回归覆盖（佣金乘数 0.1→0.11 后 33 项测试仍绿）。

**承诺**：D 日（2026-10-20）首跑完成后补 `tests/test_m02_futures_*.py` 回归，对齐股票侧
`test_m02_initialization.py` 标准，突变敏感点至少覆盖：佣金乘数、滑点费基、冻结 sha、
收据校验 fail-closed、数量冻结（hold 日零交易零费用）。

## 3. 一并更正

- `scripts/prepare_m02_futures_initialization.py` 注释源串已按第 1 节修正
  （slippage → 4-2 值 + 2026-10-18 机制；margin → 4-3；policy_source 拆分两处日期）。
- 其余裁决引用（佣金 4-1、CZCE 绝对值单位=元/手〔10-16 第 1 项〕、数量冻结与
  CZCE 过渡采集〔2026-10-18〕）核对无误。
