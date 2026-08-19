# 批次 C/D 离线重放验证：残余不是 2 条，是 217 条（2026-08-19）

> ✅ **2026-08-19 已结案**：用户当天拍板采纳本文「建议的解法」，
> 优先级修改已实施（`11137f8`，TDD 红→绿，含新增的「日盘品种真交易了仍须炸」安全测试）。
> 用**改后的真实代码**重放同一份清单：`resolved=11,124 residual=1 unconsumed=0`，
> 与下文模拟值逐位吻合，残余就是 SHFE NI 2022-03-10。
> 设计文档规则 1 与验收清单第 5 条已同步改写，并加了 §6.1 记录为什么这么定。

## 结论先行

把批次 C/D 的候选行当权威加载、对 11,861 条 ambiguity 清单逐条重放，
**窗口内残余 217 条，不是提案文档声称的 2 条。**

多出来的 216 条全部是同一个成因，且不是批次内容写错了 —— 是
**批次 C 与批次 D 在授权层互相否决**：一个日盘品种撞上本所的节前例外时，
两个权威都说「当晚没有夜盘」，`authorize_night_observation` 却因为
「day-only 不得同时命中 session exception」而 fail-closed。

**这是设计缺陷，不是编码错误。** 代码忠实实现了
`docs/superpowers/specs/2026-08-17-carry-minute-session-exceptions-design.md` 第 170 行
的规则 1，且 `tests/test_carry_session_authority.py:547`
（`test_day_only_and_session_exception_cannot_both_authorize_one_product_day`）
专门钉住了这个行为。改它要动设计与该测试，**需用户拍板**。

## 验证方法

不重跑数据库。清单每行的 `reason` 里带着当时的观测区间
（`context={'observed_night_start': ..., 'observed_night_end': ...}`），
足以离线重放授权判定。脚本直接从提案文档的两个 ```csv 代码块读候选行 ——
**验的就是评审者读的那份东西**，不碰任何 config 资产。

脚本：会话 scratchpad `verify_batches.py`（一次性验证工具，未入库）。

## 逐项结果

```text
proposed: day_only=12  session_exceptions=140
inventory rows: 11,861

  empirical_boundary（不是授权问题，跳过）              120
  night_authority_conflict                          11,741
    ├─ trade_date ≤ 2026-01-31（批次覆盖范围）        11,125
    │    ├─ 被批次解决                               10,908
    │    └─ 残余                                        217
    └─ trade_date > 2026-01-31（批次刻意止步于此）       616

session exceptions never consumed by any observation:    0
```

`unconsumed=0` 说明批次 D 的 140 行没有一条是多余的 —— 每条都被至少一个产品日消费。

## 217 条残余的构成

| 交易所 | 品种 | 残余 | 说明 |
|---|---|---:|---|
| CZCE | SM / AP | 35 / 35 | 撞上全部 35 个批次 D 目标日 |
| CZCE | SF / UR | 30 / 28 | 撞上该品种被审计到的那些目标日 |
| DCE | LH / JD | 29 / 27 | 同上 |
| CZCE | PK / CJ | 14 / 7 | 同上 |
| INE | EC | 11 | 同上 |
| SHFE | NI | 1 | **已知的那一条**（2022-03-10 单品种停盘） |

前 216 条 = 9 个日盘品种 × 各自撞上的节前日。

## 一个干净的自然实验坐实了成因

批次 C 覆盖 4 个交易所的 12 个日盘品种，批次 D 只覆盖 4 个交易所 ——
**两者交集是 CZCE/DCE/INE，GFEX 只出现在批次 C**（广期所 SI/LC/PS 三个品种全是日盘，
所里没有夜盘品种，自然不需要节前例外行）。

于是：**GFEX 的 LC / PS / SI 三个日盘品种残余为 0**，而 CZCE/DCE/INE 的九个日盘品种
无一幸免。同样是日盘品种、同样观测到 `none,none`，差别只在「本所当天有没有一条
session exception 与之相撞」。碰撞假说不需要更多证据。

## 为什么这是结构性的、不是数据问题

设计第 180 行自己写明：「异常按**交易所/目标交易日**授权，并应用于该交易所当天
所有被审计产品」。`SessionException` 的唯一键是 `(version, exchange, trade_date)`，
**没有 product 列**。

所以只要一个交易所同时存在「日盘 only 品种」和「有夜盘品种」，节前那一晚就必然产生碰撞：
例外必须登记（否则有夜盘品种全炸），登记了就必然罩住同所的日盘品种。
碰撞次数 = 日盘品种数 × 节前日数，本窗口 216 条，**全历史只会更多**。

换句话说：**按现行规则，正式资产 `config/carry_minute_sessions.csv` 永远发不出来**，
因为 `ambiguous=0` 结构上不可达。这不是补数据能解决的。

## 建议的解法（请拍板）

**让 day-only 优先于 session exception**：产品命中唯一 day-only regime 且观测为
`none,none` 时直接放行，不再要求 `not exceptions`。

理由：session exception 描述的是**该所当晚的夜盘时段**；一个从不参与夜盘的品种
不在这句话的射程内。两个权威在 `none,none` 上并不矛盾，是同一个事实的两种说法。

安全性不降低：

- 若某品种被**错误**登记成 day-only 而当晚其实交易了，观测就不会是 `none,none`，
  规则 1 的「经验值必须为 `none,none`」照样拦下 —— 这道闸没有松动。
- 例外消费追踪仍然有效：day-only 行提前返回 `None`（不消费），
  若某条例外当天只被日盘品种「覆盖」而无人消费，
  `session_exception_unconsumed` 照旧拦住发布。本窗口实测 `unconsumed=0`，无影响。

**代价**：要改设计文档第 170 行、改 `authorize_night_observation`、
并改写 `tests/test_carry_session_authority.py:547` 那条测试（它现在钉的是相反行为）。
我没有动其中任何一处。

### 采纳后能买到什么：217 → 1

同一份清单、同一批权威行，把上述优先级在**脚本里**模拟一遍（仓库代码一行未改）：

```text
current semantics            resolved= 10,908   residual=217
proposed: day-only wins      resolved= 11,124   residual=  1
                                                        └─ SHFE NI 2022-03-10
```

216 条一次清空，且**只清这 216 条** —— 残余精确落回那一条本就需要单独裁决的
单品种停盘。没有顺带放行任何别的东西，这也是「优先级修改不会误伤」的直接证据。

脚本：会话 scratchpad `verify_proposed.py`（一次性验证工具，未入库）。

### 与 SHFE.NI 那条的关系

提案文档建议用 `day_only_regimes` 表达 NI 2022-03-10 的单日停盘。若采纳上面的优先级修改，
这条路会顺带变得自洽：NI 那天既有 day-only 单日区间、又落在 SHFE 当天没有例外的日子里，
放行无碍。**但两件事仍是两个决定**，不要因为解法相容就当成一个。

## 对既有文档的两处更正

1. `docs/research/2026-08-19-carry-minute-pending-batch-cd.md` 称「除两条外全部归因」，
   按授权层实际行为是 **217 条**。已在该文档标注。
2. 同文档记批次 D「涉及 36 个目标交易日」，实为 **35 个**（140 行 ÷ 4 个交易所）。

另有一处与本文相邻的更正，见
`docs/research/2026-08-19-carry-minute-multiyear-inventory.md`：
该文记 `night_untraded_padding` 在 70,734 个产品日里触发 **0 次**，并解释为「本窗口
不含 2019-12-26，规则不应触发」。实际上 SHFE NI 2022-03-09 那一夜就在本窗口内、
且确实是「有 K 无量」，本应触发。零计数是审计管线缺陷造成的假象，
已于 `f2eaf78` 修复（note 收集移到授权调用之前）。**修复后需重跑才能拿到真实计数。**
