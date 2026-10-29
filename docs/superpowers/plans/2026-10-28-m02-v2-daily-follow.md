# M02 期货观察账本 v2（每日跟随 027）实施计划

函：`docs/requests/2026-10-28-m02-futures-daily-follow/request.md`（R1–R9）。回函写同目录 `response.md`，≤11-06。

## 已核事实（2026-10-28，只读查库）

- `futures_daily` 五所 T 日 17:00 同批落库；**09-10 起 0 天晚于 T+1 08:30**（09-01..09-09 DCE 晚 6 天，最晚 6 天 16 时，人工投递期）。
  目标管线（`citic_index.pg_source`）只读 `futures_daily`（排除 CFE），截断日 = 五所 `max(trade_date)` 最小值。
- `exchange_data.futures_settle_param`：五所逐合约逐日，T 日 17:40 落库；四费 + `fee_style/fee_scale` + 投机保证金率 + 源 URL/SHA。
  `fee_scale` 为空：CZCE 全部（比例值 6 品种 / 绝对值 20 品种）、DCE 比例值 8 品种 → 按 10-16 用户单位裁决推广，**推广本身列给对方上报裁**。
  `price_limit_rate` 只有 CZCE 有。
- `futures_contract_info`（T 日 17:13–17:30 落库）：`涨停价/跌停价` = **T 日**限价（由 T−1 结算价推出，cu2612 10-28 核过）；
  `最后日期` = 最后交易日，**与对方更正后的到期日历 16/16 吻合**（BZ/EB/EG 2611 = 11-25，JD/PG 2612 = 12-28）。
- `futures_minute`：五所 + CFFEX，符号大写、郑商所四位年月（`AP2612`），列 `exchange` 用 SHFE/DCE/CZCE/INE/GFEX。
- `data_manager.business_calendar` CN 只到 **2026-12-31** → R6「到 2027-03-31」做不到经核实的版本（2027 放假安排未公布），
  2027 段待公告后 `extend_references` 追加。
- Debian：31 GB / 12 核；v1 cron `5 21 * * 1-5 ... ~/jobs/m02-futures/current/...`；v1 store 绑旧引擎指纹
  ⇒ **v2 部署到独立根 `~/jobs/m02-futures-v2/`，绝不翻 v1 的 `current`**。

## 设计

### 引擎（forward_account，ENGINE_VERSION → v3，旧 config 行为逐位不变）
1. config 新增可选键 `daily_policy = {"target_missing": "hold_and_mark", "untradable": "hold_prior_quantity"}`；
   无此键的旧 config 走原路径。
2. 目标缺失：packet `target=None` + `target_missing={reason, evidence}`；沿用持仓、无执行价、结算价对结算价盯市、零成交。
3. 不可交易：执行报价 `tradable[k] = False` + `untradable_reasons[k]`；该合约手数 = 上日手数；
   有 09:30 价则用其盯市（涨跌停），无价则参考价取上日结算价（gap=0）——总 PnL 不变，只影响定手数用的 pretrade NAV。
4. `required` = 持仓 ∪ 非零目标（daily_policy 下）；零权重未持仓合约不需要规格和报价。
5. `extend_references` 同时追加费率规则：新合约整份加入；旧合约只能在尾部追加新时段（既有时段逐字节不变）。
6. 封账不进引擎：封账 manifest + store/feed 只读；v2 夜链遇封账 manifest 拒绝推进。

### 规格表（R5）`forward_inputs/m02_v2_specs.py`
按品种：交易所、乘数（contract_info，品种内恒定校验）、费单位映射（交易所声明 / 10-16 用户裁决 / PENDING）、
保证金（交易所投机率 +2pp，加入时冻结）、涨跌停（逐日 contract_info 限价）。合约规格按需从 T 日 settle_param 行派生；
费率变化 → 尾部追加新时段（valid_from = 变化日）。

### 早上目标（R2）`forward_inputs/m02_daily.py` + `scripts/m02_v2_morning.sh`（Debian 08:30）
复用 m01 的只读快照/对账/留证；截断日必须 = 日历前一交易日，否则写 `not_produced` manifest（夜里转 target_missing）。
预检：目标合约规格齐备、到期护栏（持仓合约最后交易日 ≤ T 且未换出 → 报警）。

### 夜链（R9）`scripts/m02_v2_nightly.{py,sh}`
闸序：封账 → 到期区间 → 目标包校验（不合格 ⇒ target_missing，非失败）→ 整所分钟线零行 fail-closed →
规格齐备（缺 ⇒ fail-closed）→ 结算价覆盖 → 干跑 → 提交 → feed → 金额链 → verdict。

### 影子（R8）
独立 store/账户 `m02-futures-observation-v2-shadow`，日历自 11-09（genesis ≤ 11-06 15:00）。
提交只进影子 store、不导 feed——"不提交"按"不进生产账本"执行，否则第二天起没有拟交易手数可报（回函披露）。

### genesis（R1）
生产 v2 日历首区间 11-16，`start_at` = 11-13 15:00 ⇒ genesis 必须在 **11-13 15:00 前**；引擎在 genesis 时冻结。

## 步骤
- [x] 1 引擎 v3 + 单测（目标缺失/不可交易/零权重/费率追加/旧 config 不变）
- [x] 2 规格表 + 待裁清单
- [x] 3 早上目标生产 + 本机实跑一次（计时、内存）
- [x] 4 v2 夜链 + 封账 + 单测
- [x] 5 genesis/影子构建脚本 + 日历
- [x] 6 全量测试 + mutation
- [~] 7 Debian 部署 7969cc3 ✓、预演 genesis 10-28 ✓；待：影子 genesis（≤11-06 15:00）、生产 genesis（≤11-13 15:00）、用户装 crontab
- [x] 8 回函（01，10-28）
- [x] 9 裁定 01（10-29）：规格表 79/0/3、v1 收尾作业（封顶补算 + 封账，Debian 副本演练 A/B/C）、持仓结算价闸用例、genesis 回执加 release/规格表；部署 c71be79；回函 02
