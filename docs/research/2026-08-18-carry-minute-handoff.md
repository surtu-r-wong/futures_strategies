# Carry 分钟线交接检查点（2026-08-18）

> 面向**明天新开对话**的入口文档。先读本文，不要重新推导已确认口径。
> 上一份检查点是 `docs/research/2026-08-14-carry-minute-execution-handoff.md`，
> 其中「DCE 异常会话」与「下次恢复顺序」两节已被本文更新，其余仍然有效。

> ⏩ **2026-08-19 追加，见文末「2026-08-19 下午」一节。**
> 一句话：授权层的设计死结已拍板修复（`11137f8`），批次补成 C+D+E 后**授权类歧义只剩 1 条**，
> 而这 1 条经公告原文 + 逐合约数据核对**也不该由权威行解决**（是代表合约选取问题）。
> **CTA 分钟线的会话权威侧至此没有待决问题**；剩下的是 CZCE 逐年日历的口径裁决、
> 5 个 product-day 的外部分钟数据、以及采集侧的一处改进建议。

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

## ⚠️ 当晚实证推翻了两处原判断

详见 `docs/research/2026-08-18-carry-minute-empirical-findings.md`。摘要：

1. **批次 A 不能直接写入。**（✅ 已于 2026-08-19 解除）2019-12-25 夜的真实数据里，
   21:00–22:28 是补齐空 K 线，22:29 是集合竞价撮合，22:30 才是连续交易。经验边界不滤
   volume 得 `21:00`、滤了得 `22:29`，两条都拿不到权威所写的 `22:30`。
   → 用户 2026-08-19 拍板「夜盘起点滤 `volume > 0` + 竞价归位」，已实施并在真实库验证，
   五个 DCE 主力全部得到 `("22:30","23:00")`。批次 A 只剩用户逐行过目一步。
   见 `docs/research/2026-08-19-carry-minute-auction-attribution-verification.md`。
2. **缺分钟不是孤例。** 有界采集 round 1 fail-closed 于**第二个**缺口
   （2019-01-02 `CU1903.SHF`）。扫描历年元旦后首日发现：**每年有 0–3 个整品种
   从供应商归档消失**（2018 AL、2019 CU/PM/RU、2020 AL/FU、2016 B/BB/RI、
   2022 RI、2023 JR/ZC；2017/2021/2024 无）。多数是非流动品种。
   这把阻塞从"无限期"变成一张**四五个 contract-day 的采购清单**。

## 现在只剩一个外部阻塞

原来两个闸门，现在剩一个：

| 闸门 | 状态 |
|---|---|
| DCE `6202113` 原文 | ✅ **已解除**（2026-08-18） |
| 22:29 竞价 K 线的处理方向 | ✅ **已解除**（2026-08-19，见上） |
| 元旦后首日的整品种归档缺口 | ❌ **仍阻塞**，但范围已从"AL1803 一个"扩清为一张确定清单 |

原交接文档把 AL1803 2018-01-02 当孤立缺口。实测是**每年元旦后首个交易日有 0–3 个
整品种从供应商归档消失**（见实证发现文档第三节）。本地一切来源已穷尽：库内目标窗口
0 行；`market_data_minute` 只从 2026-01-22 起；供应商归档 CSV 本身就缺
（`AL1803.csv` SHA-256 已存证）。**只能从 MyQuant / Wind / 上期所授权档案外部取得。**
禁止日线合成、替代合约、伪造 `none`、删审计键或硬编码绕过。

## 正在跑 / 产物位置 ⚠️

**一次有界采集**（PID 1185936，已于 2026-08-18 约 19:1x **fail-closed 结束**，未产出文件）：

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

**结果**：在 2019-01-02 `CU1903.SHF` 上 `session_representative_missing_minutes` 失败，
未写出任何产物。失败前的覆盖统计与完整结论已抄进实证发现文档，无需重跑即可继续。

⚠️ 原始日志在会话 scratchpad，明天新对话看不到该路径：
`/tmp/claude-1000/-home-elfbob-claude-code-futures-strategies/696d83d0-7ba6-4cef-8527-aed09824d41f/scratchpad/`
若本会话未来得及转存，明天**直接重跑上面的命令**即可（约 20–40 分钟），
或去 `output/`（gitignored）看是否已转存。

## 待审批次（不写资产，用户 2026-08-18 拍板）

`docs/research/2026-08-18-carry-minute-session-exception-pending-batch.md`

- **批次 A**：DCE 2019-12-26 延迟开盘，七个字段全部由原文坐实；技术阻塞已于 2026-08-19 解除（竞价归位已实施并验证），**只等用户逐行过目**
- **批次 B**：窗口内 45 条已复核节前 `none,none` 候选，缺 `trade_date` 日历映射与经验清单交叉验证
- 文档里标了**提取器的三个已知缺口**，人工复核时必看（尤其 DCE 2020-01-23 因
  `2020-01-23→修订后 02-03` 的写法未被正则抓到）

## 明天的推荐顺序

1. ~~先拍板 22:29 竞价 K 线的处理方向~~ **已于 2026-08-19 完成**。
   补测的爆炸半径证据把四选一收敛成一个：夜盘**起点**滤 `volume > 0` 零代价
   （372 个夜盘 0 例被挪动），而收尾与日盘滤了就是垃圾（连 CU 主力都 `23:59→23:57`）。
   于是采用「只滤夜盘起点 + 竞价归位」，权威行保持公告原文 `22:30`，网格闸与授权层都没放宽。
2. ~~收敛采购清单~~ **已于 2026-08-18 完成**。最小清单确定为 5 个 product-day，
   全在上期所：`SHFE.al1803`(2018-01-02)、`SHFE.cu1903` 与 `SHFE.ru1905`(2019-01-02)、
   `SHFE.al2002` 与 `SHFE.fu2005`(2020-01-02)。建议连次主力一并取
   （`AL1802`/`CU1902` 等，成本极低）。扫描已覆盖全窗口 2011–2026，无遗漏年份。
3. **补完数据后重跑有界采集**，才能拿到第一份真实 ambiguity 清单。
4. **批次 B 三方交叉**：官方来源 × 日历映射 × 经验清单，三者一致才进批次。
5. Task 12 Step 2（五品种真实烟测）**仍被 `config/carry_minute_sessions.csv` 阻塞**
   （`cta_carry/__main__.py:294` 硬加载），而该资产需全历史 `ambiguous=0`，
   因此**根子上仍卡 AL1803**。

## 今天新确认的操作事实

- DCE 官方页与 `qhxy` 镜像**都**上了瑞数 JS 挑战（HTTP 412），
  **以前能机读的 DCE URL 现在也不能再机读复核了**。需要正文一律走浏览器人工取。
- `ruff format` 漂移在 `803b7f7` 就已存在于四个文件，与本次改动无关；
  用户口径是单独开 style 提交（已记入记忆 `formatting-goes-in-its-own-commit`）。

---

## 2026-08-19 下午（追加）

提交：`f2eaf78` → `e6541da` → `e19048f`。分支仍未合并未推送。

### 待修的四处，处理结果

| # | 内容 | 结果 |
|---|---|---|
| 4 | `night_untraded_padding` 计数恒为 0 | ✅ **已修** `f2eaf78`（TDD：先用真实的 NI 2022-03-09 写红测） |
| 1 | 批次 C 的 `effective_start` 取采集窗口左端而非上市日 | ✅ **已修** `e6541da`，12 个品种改为 `futures_daily` 实测首日 |
| 2 | 批次 C 中 DCE 的 `source_url` 被截断 | ✅ **已修** `e6541da` |
| 3 | SHFE.NI 来源「URL 是 404」 | ⚠️ **前提被推翻**，见下 |

第 3 条：该 URL 复测返回 **200 + 瑞数 WAF 校验页**，与上期所首页、以及批次 D 里**已被采纳的
两条 SHFE URL 表现完全一致**。五个交易所域名逐一实测，**今天只有 CZCE 的 PDF 直链可机读**。
所以「URL 我能不能打开」不能当取舍标准（会连带否掉已采纳的行），代码层也从不校验可达性。
缺的是**公告正文存证**，仍需你在浏览器取。详见
`docs/research/2026-08-19-authority-url-reachability.md`。

### 🔴 新发现：批次 C/D 合起来仍留 217 条歧义

把两批当权威、对 11,861 条清单离线重放（不重跑数据库，清单 `reason` 里带着观测区间）：

```text
窗口内(≤2026-01-31)  解决 10,908   残余 217        ← 提案文档称残余 2
窗口外(>2026-01-31)  616（批次刻意止步于此，非缺陷）
empirical_boundary   120（不是授权问题）
未被消费的例外行      0（批次 D 的 140 行没有一条多余）
```

216 条同一个成因：**批次 C 与批次 D 在授权层互相否决**。日盘品种撞上本所节前例外时，
两个权威都说「当晚没有夜盘」，`authorize_night_observation` 却因规则
「day-only 不得同时命中 session exception」而 fail-closed。

对照组很干净：**GFEX 是唯一「在批次 C、不在批次 D」的交易所，其三个日盘品种残余为 0**；
CZCE/DCE/INE 的九个日盘品种无一幸免。

**这是设计缺陷不是编码错误**：代码忠实实现了会话例外设计第 170 行的规则 1，
且 `tests/test_carry_session_authority.py:547` 专门钉住该行为。因为
`SessionException` 按交易所授权（无 product 列），只要一所同时有日盘品种和夜盘品种，
碰撞就必然发生 —— **按现行规则 `ambiguous=0` 结构上不可达，正式资产永远发不出来**。

### ✅ 已拍板并实施（`11137f8`）

用户当天采纳：**day-only 无条件优先** —— 产品命中唯一 day-only regime 且观测为
`none,none` 时直接放行，不看例外写的是 `none,none` 还是具体夜盘时段，且**不消费**该例外。

不采用「仅当例外也是 `none,none` 才让路」的严格变体：实测它在批次 A 那天仍然留洞
（DCE 2019-12-26 延迟开盘 × JD 日盘品种 → 照炸），只是把同一个死结挪小。

闸门没放宽，且这次给它补了测试：`test_day_only_product_that_traded_at_night_still_fails_closed`
钉住「被误登记成 day-only 的品种当晚真交易了必须炸」（有/无例外两种情形都测）——
这正是放手所依赖的那道闸，改之前竟没有测试直接盯着它。

同步改了设计文档规则 1、验收清单第 5 条，并新增 §6.1 记录判据，
免得下一个人照旧规则改回去。

**用改后的真实代码重放同一份 11,861 行清单**：`resolved=11,124 residual=1 unconsumed=0`，
与事前模拟逐位吻合，残余就是 SHFE NI 2022-03-10。

### 真实库重跑确认了 `f2eaf78`

同参数重跑采集（2020-07-01→2026-04-29，backtest-start 2022-07-01，约 5 分钟，峰值 RSS 2.0 GB）：

```text
products=63 rules=0 checked_days=70734 ambiguous=11861   ← 与 8-19 上午逐位相同
night_untraded_padding=2022-03-10 SHFE NI                ← 原为 0 次，现为 1 次
```

11,861 行歧义清单**逐条完全相同**，即修复只补回丢失的观测事实、没有改动任何判定。
产物在会话 scratchpad `capture/rerun_{inventory.csv,audit.txt}`。

⚠️ 在工作树里跑采集必须显式传
`--settings /home/elfbob/claude-code/futures_strategies/config/settings.yaml`
—— `settings.yaml` 是 gitignored 的，工作树里没有，否则 PG 认证失败。
另外脚本 stdout 是块缓冲，长跑期间日志文件会一直是 0 字节，别据此判定它挂了；
判活用 `ps -p <pid> -o etimes=,rss=`。

### 验证状态

全量 `854 tests: 853 passed, 1 failed`（3 分 28 秒）。唯一失败是
`test_repository_session_rules_are_nonoverlapping_and_cover_fixture_products`
缺 `config/carry_minute_sessions.csv` —— 计划起点即红、master 上同样红，与本次改动无关
（已用 `git stash` 复核）。`ruff check` / `ruff format --check` 均通过。

### 现在剩下的（已无待决的设计问题）

1. **你去 Wind 数据机那趟能一次办三件事**：取 5 个 product-day 的分钟数据
   （`SHFE.al1803` 2018-01-02、`SHFE.cu1903` 与 `ru1905` 2019-01-02、
   `SHFE.al2002` 与 `fu2005` 2020-01-02，建议连次主力一并取）；
   取上期所镍暂停通知正文；顺手看一眼停掉的 `FuturesDataBackfill` 计划任务。

   ~~浏览器顺手可办两件~~ **✅ 已于当日闭合**（用户提供两个 URL）：
   DCE 2026 元旦安排 `http://www.dce.com.cn/dce/content/2025/ywggytz/18625821.html`；
   上期所 2026 休市公告 `https://www.shfe.cn/publicnotice/notice/202512/t20251217_829805.html`
   （〔2025〕157 号，本机取回全文核实，七个前夕全部列明）。
2. ✅ **NI 2022-03-10 的 schema —— 问题本身取消了**。取回上期发〔2022〕73 号原文并
   逐合约核对分钟数据：那晚 NI 夜盘**开着**，NI2203/2208/2210/2211/2302 五个合约
   正常成交；零成交的恰是公告点名的七个合约，而采集的代表合约 NI2205 不巧在其中。
   **两种 schema 提案都要断言一句假话**（产品级「没有夜盘」），都不能用。
   建议改在采集侧从数据判定（代表合约整夜零成交时查兄弟合约），fail-closed 性质不变。
   详见 `docs/research/2026-08-19-ni-2022-03-10-is-not-a-session-fact.md`。
3. 批次 A/C/D 逐行过目；批次 B 三方交叉。**过目前先看批次文档顶部的三条红字** ——
   机器体检查出：批次 D 只差 8 行（2026-02-24 与 2026-04-07 两个节后首日）；
   CZCE 的 35 行全靠一个 `rows_derived=0` 的规则型来源（356 条歧义仅靠它支撑）；
   DCE 2026-01-05 一行引用了 CZCE 的 URL。
   ⚠️ 取回该 PDF 原文核实后收窄了结论：**规则本身没问题**（第八条逐字写着「法定节假日
   （不包含双休日）后第一个交易日无夜盘交易」），**缺的是逐年日历** ——
   35 个日期是从三所前夕取并集推的。待裁决：对 CZCE 什么算权威逐年日历
   （CZCE 自家 12 份年度公告 / 国务院逐年安排 + 常设规则 / 接受三所并集）。
   **这决定了 341 条能不能进资产。CZCE 站内页 412 不可机读，PDF 直链搜索引擎也没索引到，
   走 (a) 的话只能你在浏览器取，12 次即可（12 个年度覆盖 77 个前夕）。**
4. `futures_daily` 2026-03 的 6 个交易日空洞：建议用分钟表/本地档案回填，
   而不是把研究窗口截到 2026-01-31 —— 前者清掉 114 条噪声且不损失 3 个月配对回测样本。

外部阻塞仍是元旦后首日的整品种归档缺口（5 个 product-day，全在上期所）。
