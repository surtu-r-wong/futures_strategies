# Carry 分钟时段采集 eligibility 修正设计 v2

**日期：** 2026-08-14
**状态：** 已确认
**作用范围：** `2026-08-13-carry-minute-execution-design.md` 的时段规则采集补充；完整替代同日无 `-v2` 后缀的旧稿

## 1. 根因、数据库事实与证据范围

初版采集实现对请求区间内每个商品品种日都选择最高持仓合约并要求分钟数据。
这个集合大于 Carry 分钟引擎实际可能访问的集合，因而在 2011-01-04 的
`FU1103.SHF` 上触发了无分钟行硬失败。

FU 并非 2018 年首次上市。上期所的品种沿革材料只用于支持以下事实：FU 于
2004 年上市，2011 年修订为内贸 180CST 船用燃料油合约；2018 年终止旧
180CST 合约并重新挂牌保税 380CST 合约：

- https://www.shfe.com.cn/docview/docview_10141355.htm
- https://www.shfe.com.cn/publicnotice/notice/201806/t20180626_793285.html

库内只读审计另行证明：`FU1604` 在 2016-01-04 的日线与分钟成交量均为 8，
分钟 OHLC 和按 50 吨乘数计算的成交额与日线一致，故 2016 年旧版 FU 数据
有效。2011–2015 年 FU 的默认 120 日、向后移动一日的品种成交额均值最高为
`4,299,422,549.9167` 元，低于默认 `5,000,000,000` 元门槛。这个统计从
2010 年加载原始日线作为 2011 年滚动窗口预热数据；2011–2015 只表示被判定
的目标日期。实现计划必须保存生成这两项结论的只读 SQL、命令与摘要，不能把
这些数值写成不可复现的程序常量。

历史夜盘时段由对应交易所材料支持，而不由上述 FU 链接支持。大商所 2015 年
5 月材料记录夜盘收盘调整至 23:30；郑商所材料记录相关品种 21:00–23:30
夜盘以及节假日前不进行夜盘交易：

- https://www.dce.com.cn/dalianshangpin/resource/cms/2016/07/%E5%A4%A7%E8%BF%9E%E6%9C%9F%E8%B4%A7%E5%B8%82%E5%9C%BA%E6%9C%88%E6%8A%A5%EF%BC%882015%E5%B9%B45%E6%9C%88%EF%BC%89.pdf
- https://www.czce.com.cn/cn/rootfiles/2020/01/13/1572882898738930-1572882898769401.pdf
- https://www.czce.com.cn/cn/rootfiles/2018/12/24/1545632831296256-1545632831311552.pdf

## 2. 固定配置、预热窗口与逐品种护栏

采集命令必须实例化无实验覆盖的默认 `CarryConfig()`；命令行只改变请求起止日
和输出位置，不允许改变 eligibility 参数。当前有效值必须从实例打印，不在
采集脚本复制默认常量：

```text
eligibility_config liquidity_window=120 liquidity_threshold=5000000000.0 prewarm_calendar_days=730
requested_range start=YYYY-MM-DD end=YYYY-MM-DD daily_load_start=YYYY-MM-DD
```

日线加载起点为 `requested_start - timedelta(days=config.prewarm_calendar_days)`，
即当前默认向前 730 个自然日。聚合必须复用
`cta_carry.curve.aggregate_product_liquidity`，保留其 120 个实际观测、
`min_periods=120`、向后移动一日、`>=` 门槛以及正式策略计算顺序。

时段资产必须满足
`capture_start <= backtest_start - timedelta(days=config.prewarm_calendar_days)`，
因为预热期同样驱动日线信号、分钟执行状态和影子账户。基线
`backtest_start=2013-01-04` 与默认 730 天得到最晚
`capture_start=2011-01-05`，因此真实采集从 2011-01-01 开始。采集命令与
分钟回测启动前都必须校验该不变量；将来修改回测起点或预热长度时，旧资产不能
静默沿用。

完整交易日历是预热期与请求期内所有规范化商品日线日期的有序并集；所有
`prev`、`next` 都在这个全局交易日历上计算，不能在单品种现存行或请求范围
切片上 `shift`，也不能用自然日加减替代。

对请求范围内每个有规范化日线的品种，其首个目标品种日必须逐项检查：

- `liquidity_mean` 有限且非 NaN，说明预热完整；或
- 数据库中该品种在 `daily_load_start` 之前没有任何日线，记录为
  `insufficient_since_inception`，按共享函数语义在凑足窗口前不入池；
- 若均值为空但加载下界之前仍有历史行，则以
  `liquidity_history_incomplete` 非零退出。

该检查同样适用于请求期中途首次出现的品种，不能由另一个历史完整的品种替它
通过。测试还必须覆盖恰好等于阈值时 `in_pool=True`。

长期停牌后复牌等确有来源的历史缺口，唯一豁免出口为
`config/carry_liquidity_history_exceptions.csv`，精确表头为
`version,exchange,product,effective_start,effective_end,reason,source_url`。
同版本、交易所、品种区间禁止重叠，`version` 必须等于
`SESSION_RULES_VERSION`，空表头资产表示无豁免。只有首个目标品种日恰好命中
一个有官方来源的区间时，`liquidity_history_incomplete` 才改记为
`authorized_history_gap` 并允许采集继续；该品种仍保持 `in_pool=False`，
绝不由豁免抬入池。零匹配、多匹配或无来源仍硬失败。运行日志必须记录该资产
版本、SHA-256 和实际命中行；不得增加代码内品种特判。


## 3. 被审计候选集合：策略访问包络

设 `T` 为目标分钟执行会话日，`P(T)` 表示该品种在 `T` 的共享聚合结果
`in_pool=True`。对每个请求范围内的品种日，静态时段审计条件定义为：

```text
audit(T) = P(T) or P(prev(T)) or P(prev(prev(T)))
```

等价实现应从每个 `P(S)=True` 的键向全局交易日历发射 `S`、`next(S)`、
`next(next(S))`，再与请求范围相交。这样即使品种在退出日 `T` 没有日线行，
候选键也不会在与目标日内连接时静默消失。`P(prev(T))` 覆盖在 `T-1` 收盘
产生并于 `T` 执行的入场、续持和换月目标；`P(prev(prev(T)))` 覆盖品种在
`T-1` 首次出池、但 `T` 仍需执行的退出。`P(T)` 对 `T` 当日执行并非因果
必要项，而是为当日在池产品保留的保守超集；它生成的信号最早在 `next(T)`
执行。不存在需要第三个 lag 的有效持仓，因为首次无信号已生成下一日退出目标。

`normalized_keys` 是请求范围内实际存在的规范化
`(exchange, product, trade_date)` 键；`audit_universe_keys` 是
`normalized_keys` 与上述向后两期发射键的并集。`in_pool_keys` 和
`audit_keys` 必须满足 `in_pool_keys ⊆ normalized_keys`、
`in_pool_keys ⊆ audit_keys ⊆ audit_universe_keys` 以及
`normalized_keys ⊆ audit_universe_keys`。候选不能以内连接目标日行情作为
驱动；缺失 product-day 的 `P(T)` 明确定义为 false。

对有目标日合约行的 `audit(T)`，按 `oi` 降序、`volume` 降序、`contract`
升序选择唯一代表合约。对没有目标日合约行的退出候选，使用最近一个产生该
候选的在池日所选主力合约并保留目标日 `T` 查询；若分钟行也不存在则结构化
失败，不能删除候选。时段规则按交易所和品种解析，同品种换月双腿共享规则，
所以代表合约用于观测产品时段，不声称覆盖每条腿的行情可用性。

时段采集的代表合约缺分钟必须抛
`MinuteDataError(check="session_representative_missing_minutes")`，并在
`context` 携带 `candidate_role="session_representative"`、
`causal_in_pool_date` 和代表合约选择来源。Task 8 对实际持有、换月或退出腿
缺分钟则使用独立的 `check="dynamic_execution_leg_missing_minutes"` 与
真实 `candidate_role`。两类报错不得复用，以免把产品时段观测失败误诊成实际
持仓腿行情缺失。

Task 8 引擎的逐日动态访问集合仍须显式取当日非零信号主力、跨日携带合约、
换月双腿、退出腿和收盘计价合约的并集。默认配置下动态产品日必须是
`audit_keys` 子集；否则采集或回测硬失败并修订设计，不允许猜测时段。

共享 eligibility 结果之外不得复制滚动公式，也不得维护 FU 等品种例外名单。

## 4. 时段枚举与分类

CSV `night_end` 的完整枚举为：

- `none`
- `23:00`
- `23:30`
- `01:00`
- `02:30`

`23:30` 精确映射为 `SessionSegment(-180, -30)`，最后一个权威分钟槽为
23:29；反向分类必须唯一还原为 `23:30`。其他完整映射为：
`23:00 -> (-180, -60)`、`01:00 -> (-180, 60)`、
`02:30 -> (-180, 150)`；`none` 不产生夜盘 segment。所有 segment 长度
必须可被 15 分钟整除。

分类要求现有三个标准日盘区段的首尾全部精确匹配，并要求夜盘首尾成对。
未知夜盘尾点、单边缺失、日盘边界缺失、同一候选缺行或重复行都成为结构化
`ambiguous` 并最终非零退出。

日盘存在而夜盘双缺失不能仅凭分钟事实自动分类为 `none`，必须由以下仓库资产
授权：

- `config/carry_minute_no_night_dates.csv`，精确表头
  `version,exchange,trade_date,reason,source_url`，唯一键为
  `(version, exchange, trade_date)`。这里 `trade_date` 是夜盘缺失所归属的
  **目标交易日**，不是公告所称的节前最后交易日或夜盘前夕自然日；整理时必须
  用第 2 节完整交易日历把公告前夕映射到下一目标交易日；`reason` 必须包含
  可机读的 `notice_evening=YYYY-MM-DD`，采集命令逐行复算并拒绝 off-by-one；
- `config/carry_minute_day_only_regimes.csv`，精确表头
  `version,exchange,product,effective_start,effective_end,reason,source_url`，
  唯一键为 `(version, exchange, product, effective_start)`；空
  `effective_end` 表示无界结束，同版本同交易所品种的区间禁止重叠。

两个 loader 都拒绝空必填值、无效日期、重复键、未知版本和空来源，且每行
`version` 必须等于 `SESSION_RULES_VERSION`。每个 `audit_key` 都先解析日盘
制度与停夜盘日期的权威匹配，再与经验分类双向核对：

- 经验分类为 `none`：恰好命中一个日盘制度区间，或制度零命中且恰好命中一个
  交易所停夜盘日期；
- 经验分类为任一夜盘时段：两张 `none` 权威表都必须零命中；
- 其他组合，包括权威表要求 `none` 但实测有完整夜盘，都成为 `ambiguous`。

普通周末的自然日差大于 1 不是授权条件。夜盘制度启用前或明确终止后的连续
`none` 必须由第二张表明确给出，不能从数据缺失反推制度。

运行时必须输出两个资产的版本和 SHA-256 内容哈希：

```text
session_authority version=commodity-v1 no_night_sha256=... day_only_sha256=...
```

规则版本保持 `commodity-v1`。仓库尚未生成或发布该版本 CSV，因此本次枚举
扩展没有已发布资产兼容迁移。

## 5. 查询、折叠、间隙与原子发布

候选按目标交易日自然月分批写入临时表，通过裸列
`m.symbol = c.minute_symbol` 和字面量物理时间界限执行边界聚合。左连接保留
候选无分钟行；`PublicMinuteSource.iter_session_boundaries` 对缺行、重复行
或 `observed_rows=0` 结构化硬失败。

折叠中的相邻只指同一品种在第 2 节全局交易日历中的相邻交易日，且两天都属于
`audit_keys`。只有相邻、已审计且 `night_end` 相同的行才能合并。任何缺少
审计键的中间交易日都保持无映射；即使间隙两端值相同也不外推。两端值不同更
不得猜测切换日。这样实验配置或动态候选访问资产覆盖外日期时，resolver 会因
零匹配硬失败，而不会命中未经审计的连续区间。

命中权威停夜盘清单的节后首个目标交易日可分类为 `none`。因此
`[23:00, none, 23:00]` 必须折叠成三段，中央段
`effective_start == effective_end`。所有孤立或连续 `none` 都逐日匹配版本化
清单；自然日差和按年确定性样本只是附加审计，不替代全量匹配。大量合法单日
`none` 区间是预期资产形态。

命令先写同目录临时文件，再用正式 loader 复读；每个已审计边界必须恰好映射
到一个权威分钟槽，生成规则反向校验的键集合必须恰好等于 `audit_keys`，不得
覆盖任何未审计交易日。全部通过后才执行 `os.replace`；任何异常不得留下部分
正式 CSV。

## 6. 可观测性与统计不变量

每个请求范围内的目标交易年份都必须输出一行，即使 `audited_days=0`：

```text
coverage_year=YYYY all_product_days=N in_pool_days=N in_pool_ratio=R audit_universe_days=N audited_days=N audited_ratio=R normalization_excluded_product_days=N normalization_unkeyable_rows=N
```

计数以唯一 `(exchange, product, trade_date)` 键、按 `trade_date.year` 归属。
`all_product_days`、`in_pool_days`、`audit_universe_days`、`audited_days`
分别是 `normalized_keys`、`in_pool_keys`、`audit_universe_keys`、
`audit_keys` 的基数。`in_pool_ratio = in_pool_days / all_product_days`，
`audited_ratio = audited_days / audit_universe_days`；两者固定输出 6 位小数，
对应分母为零时输出 `0.000000`。

`normalization_excluded_product_days` 只统计能够从原始合约和有效日期解析出
完整身份、但该品种日没有任何规范化行存活的唯一键。
`normalization_unkeyable_rows` 统计无法形成完整身份、但交易年份仍可解析的
原始排除行。交易日期也无法解析的行不虚构年度归属，单列全期
`normalization_unkeyable_unknown_date_rows=N`。任一 unkeyable 计数非零都
禁止发布并要求先完成数据审计。

必须逐年及全期断言 `in_pool_keys ⊆ normalized_keys ⊆
audit_universe_keys`、`in_pool_keys ⊆ audit_keys ⊆ audit_universe_keys`
以及 `boundary_keys == audit_keys`。最终摘要为：

```text
products=N rules=N checked_days=N ambiguous=N
```

`checked_days` 等于全期 `boundary_keys` 基数，也等于年度 `audited_days`
之和；成功发布要求 `ambiguous=0`。年度指标从独立键集合计算，不能让
`checked_days` 与 `audited_days` 共享一个已筛选计数器形成无效自证。

## 7. 自动化与真实验收

自动化测试必须覆盖：

- 默认 `CarryConfig()` 的 120 日窗口、50 亿元门槛和 730 天预热，以及配置、
  请求范围、加载起点日志；
- `capture_start <= backtest_start - prewarm_calendar_days`，基线边界为
  2011-01-05，采集起点 2011-01-01 合法，欠覆盖必须在采集与回测预检失败；
- 一个品种预热完整而另一个截断、请求期中途首次出现品种、加载下界前历史
  存在与不存在两条路径；长期停牌豁免 CSV 的严格 schema、非重叠、哈希、
  零/一/多匹配，以及命中后仍保持 `in_pool=False`；
- 阈值下方、恰好阈值、最高 OI 及两个 tie-break 排序；
- `P(T)`、`P(T-1)`、`P(T-2)` 包络，范围首日由范围外在池日发射的候选，
  首次出池后的持有与退出，以及目标日无日线时候选仍保留；首次缺信号生成
  下一日清零目标必须直接测试真实 `plan_signal_targets` 行为；
- `23:29` 分类、`23:30` 与 `SessionSegment(-180, -30)` 双向映射、未知尾点；
- 普通周末假 `none`、未授权连续 `none` 必须失败，权威停夜盘日的
  `[night, none, night]` 产生三段；两个权威 CSV 的严格表头、版本、唯一键、
  区间非重叠、匹配优先级、清单声明 `none` 却实测完整夜盘的反向冲突，
  以及内容哈希稳定；公告所列夜盘前夕日期必须经全局交易日历映射为下一目标
  `trade_date`，直接把公告日期写入清单的 off-by-one 用例必须失败；
- 同值和异值端点之间只要存在真实未审计交易日都不外推，实验配置访问间隙
  由 resolver 零匹配失败；
- 整年零 audited、四个年度键集合、两种固定精度比率、可归键与不可归键的
  标准化排除计数、非零 unkeyable 发布阻断、`boundary_keys == audit_keys`
  与总计不变量；
- 缺行、重复行、零分钟行、复读或反向校验失败均回滚且不发布部分资产；
  代表合约缺分钟与动态执行腿缺分钟的 `check` 和 `candidate_role` 必须明确
  区分。

真实只读验收范围固定为 2011-01-01 至 2026-04-29，使用仓库外实际数据库
配置，保留 FU 2016 一致性与 FU 2011–2015 流动性审计命令和摘要，并对每年
确定性停夜盘样本人工核对官方来源。最终要求 `ambiguous=0`、统计不变量成立、
正式 loader 复读和候选边界反向校验通过，才原子发布仓库 CSV。

## 8. 明确不做

- 不回填、插值或从日线合成 FU 分钟数据；
- 不把 FU 硬编码为特殊排除品种；
- 不降低 50 亿元门槛来扩大采集覆盖；
- 不把 23:30 数据截断或归类为 23:00；
- 不外推未审计交易日，也不用引擎 fallback 掩盖规则缺失或歧义；
- 不改变日线策略的历史输出或其他 Task 的执行语义。
