# Carry 分钟线交接检查点（2026-08-18）

> 面向**明天新开对话**的入口文档。先读本文，不要重新推导已确认口径。
> 上一份检查点是 `docs/research/2026-08-14-carry-minute-execution-handoff.md`，
> 其中「DCE 异常会话」与「下次恢复顺序」两节已被本文更新，其余仍然有效。

## 工作位置

- 分支：`feature/carry-minute-execution`
- 工作树：`/home/elfbob/claude-code/futures_strategies/.worktrees/carry-minute-execution`
- 分支保持原状，未合并未推送（2026-08-18 用户拍板）

## 今天完成了什么

### 1. Session exception schema 迁移计划（Task 1–5）全部完成

计划 `docs/superpowers/plans/2026-08-17-carry-minute-session-exceptions.md`，37 个 step 全勾。

| 提交 | 内容 |
|---|---|
| `594b2d5` | 会话规则 CSV 加 `night_start`；三个严格时钟接缝 |
| `84ebb83` | `NoNightDate` → `SessionException`；精确双向区间授权 |
| `b121707` | 经验分类返回精确 `(start,end)` 对（不取整）；例外消费追踪 |
| `585a6f1` | 按区间对折叠、发布、回放 |
| `d8e6575` | 纯 `ruff format`（HEAD 既有漂移，单独成提交） |
| `ebcf973` | 实施记录 + 来源登记/交接更新 |

验证：聚焦回归 `431 passed`；全量排除已知闸门 `835 passed / 0 failed`；
全量不排除 `1 failed / 835 passed`（唯一失败=缺 `config/carry_minute_sessions.csv`，
计划起点与 master 上同样红）。详见
`docs/research/2026-08-17-carry-minute-session-exceptions-implementation.md`。

### 2. 母计划 Task 12 Step 0 + Step 1 完成（`59b553a`）

Step 1 拿**真实生产库**跑 EXPLAIN-only，四项期望全过：

- 裸 `symbol = c.minute_symbol` 索引条件
- 264 个 chunk 剪枝到 **2** 个
- 最大估计行 **852,013**（闸门 10,000,000）
- hypertable 上无 Seq Scan（唯一 Seq Scan 打在 5 行临时候选表 = 设计要求的小表驱动）
- `audit.minute_query_months == 0` / `minute_rows == 0` 证明确实没取数

详见 `docs/research/2026-08-18-carry-minute-task12-step1-explain-smoke.md`。

### 3. ✅ DCE 闸门已关闭（`ddb9532` / `294b8c6`）

原文由用户 2026-08-18 人工取回（脚本不可达：官方页与镜像均返瑞数 JS 挑战 HTTP 412）。

- 《关于调整夜盘交易时间的通知》**大商所发[2019]553号**，2019-12-25 发布
- 原文：「2019年12月25日晚夜盘交易时间调整为22:30—23:00，集合竞价时间为22：25—22：30」
- **无品种限定 = 全所口径** → `SessionException` 的无 product 列唯一键足以表达，schema 不返工
- 生效 URL：`http://qhxy.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6202113/index.html`
- 来源登记状态 `pending_manual_fetch` → **`reviewed_direct`**

## 现在只剩一个外部阻塞

原来两个闸门，现在剩一个：

| 闸门 | 状态 |
|---|---|
| DCE `6202113` 原文 | ✅ **已解除**（2026-08-18） |
| AL1803 2018-01-02 原始分钟数据 | ❌ **仍阻塞**，且已穷尽本地一切来源 |

AL1803 已核实穷尽：库内目标窗口 0 行；`market_data_minute` 只从 2026-01-22 起；
供应商归档 `期货数据/1m/2005-2024/SHFE/AL/AL1803.csv` 本身就从 2017-12-29 14:59
跳到 2018-01-02 21:00（SHA-256 已存证）；全部 `AL18*.csv` 当日日盘合计 0 行。
**只能从 MyQuant / Wind / 上期所授权档案外部取得。** 禁止日线合成、替代合约、
伪造 `none`、删审计键或硬编码绕过。

## 正在跑 / 产物位置 ⚠️

**一次有界采集**（PID 1185936，`setsid` 脱离，2026-08-18 约 18:5x 启动）：

```bash
python -m scripts.carry.capture_minute_sessions \
  --start 2018-06-01 --end 2020-06-30 --backtest-start 2020-06-01 \
  --output <scratch>/round1_sessions.csv \
  --inventory-output <scratch>/round1_inventory.csv \
  --audit-report <scratch>/round1_audit.txt
```

**为什么是这个窗口**：预热固定 730 天，`backtest-start 2020-06-01` 使采集起点只需
≥2018-06-02，于是窗口**绕开 2018-01-02 的 AL 缺口**，同时**包含 2019-12-26**
（DCE 延迟开盘那天）。输出全部落临时目录，**绝不碰正式资产**
（`validate_capture_request` 只在写正式路径且起点非 2011-01-01 时才拒绝，故合法）。

⚠️ **产物在会话 scratchpad，明天新对话看不到该路径**：
`/tmp/claude-1000/-home-elfbob-claude-code-futures-strategies/696d83d0-7ba6-4cef-8527-aed09824d41f/scratchpad/`
若本会话未来得及转存，明天**直接重跑上面的命令**即可（约 20–40 分钟），
或去 `output/`（gitignored）看是否已转存。

## 待审批次（不写资产，用户 2026-08-18 拍板）

`docs/research/2026-08-18-carry-minute-session-exception-pending-batch.md`

- **批次 A**：DCE 2019-12-26 延迟开盘，**七个字段全部坐实，只等你点头写入**（一行）
- **批次 B**：窗口内 45 条已复核节前 `none,none` 候选，缺 `trade_date` 日历映射与经验清单交叉验证
- 文档里标了**提取器的三个已知缺口**，人工复核时必看（尤其 DCE 2020-01-23 因
  `2020-01-23→修订后 02-03` 的写法未被正则抓到）

## 明天的推荐顺序

1. **写入批次 A**（若你同意）：一行进 `config/carry_minute_session_exceptions.csv`，
   跑 `tests/test_carry_session_authority.py` 确认仓库契约回归仍绿
   —— 注意该测试断言资产是**纯表头**，写入后需同步更新该断言。
2. **读采集产物**（或重跑）：`round1_inventory.csv` 的 ambiguity 分布，
   特别看 **2019-12-26 DCE** 是否以延迟开盘的特征出现，
   这是我今天写的 session-exception 机制第一次接触真实数据。
3. **验证集合竞价 K 线**：原文给出集合竞价 22:25—22:30。若供应商为集合竞价成交
   单独打一根 22:25–22:29 的 K 线，`night_first` 就不是 22:30，经验分类会 fail-closed。
   这不是缺陷，是必须先确认的事实。用一条 symbol+时间字面量界定的查询即可确认。
4. **批次 B 三方交叉**：官方来源 × 日历映射 × 经验清单，三者一致才进批次。
5. Task 12 Step 2（五品种真实烟测）**仍被 `config/carry_minute_sessions.csv` 阻塞**
   （`cta_carry/__main__.py:294` 硬加载），而该资产需全历史 `ambiguous=0`，
   因此**根子上仍卡 AL1803**。

## 今天新确认的操作事实

- DCE 官方页与 `qhxy` 镜像**都**上了瑞数 JS 挑战（HTTP 412），
  **以前能机读的 DCE URL 现在也不能再机读复核了**。需要正文一律走浏览器人工取。
- `ruff format` 漂移在 `803b7f7` 就已存在于四个文件，与本次改动无关；
  用户口径是单独开 style 提交（已记入记忆 `formatting-goes-in-its-own-commit`）。
