# Futures Strategies Roadmap

更新日期：2026-08-10

本页是当前交付状态的唯一摘要；历史设计和实施细节保留在 `docs/plans/`、
`docs/specs/` 和 `docs/superpowers/`。

## 已交付

- `cta_gtja`：三因子价格/量价 guarded 路径、连续合约复权选择与 Data Quality V2。
- `cta_carry`：分合约 Carry 日线研究版、账户对账、报告与 CLI。
- 两条 `public-pg` 路径默认排除股指和国债期货；仅 `cta_gtja` 保留 `--include-financial`
  作为历史诊断例外，`cta_carry` 无此开关；文件源由输入数据负责边界。

## 实验，不是默认策略变更

- Carry 趋势滞后：`trend_band_atr=0`、`trend_confirm_days=1` 时逐点复现基线。
- `secondary_selection=second_by_oi`：研报次主力口径对照；默认仍为 `strictly_later`。
- `equal_weight_capital`：已实测为负面结果；默认关闭。

## 外部阻塞

- `futures_daily` 与 `continuous_contract_ohlc` 的 EOD 日更仍止于 2026-04-29。
- Wind catalog、preflight、recovery package 与 published conservative fundamentals build 尚未完成。
- 因此 `master` 的可信默认是 `price_volume`；不得把 legacy 稀疏表结果称为完整六因子复刻。

## 待验收分支

- `feature/commodity-fundamentals` 已实现 raw basis、PIT reader、覆盖闸门和血统报告。
- 分支保持未合并；真实 build 到位后必须完成 medium、high 和 price-volume control 三组对照并记录 build/catalog 版本，才能评估合入。

## 仓库边界

- 本仓拥有商品期货策略、只读数据适配、回测、质量闸门和研究报告。
- Wind 抽取、writer、数据库 DDL、标准数据构建属于 `market-monitor`。
- 股指期货类策略属于股票生态，不在本仓新增。
- 任何新策略都需要单独设计与批准；“商品期货归本仓”不是具体策略的实施授权。
