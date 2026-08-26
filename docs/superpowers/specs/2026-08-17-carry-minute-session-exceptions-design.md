# Carry 分钟会话异常与可变夜盘起点设计

**日期：** 2026-08-17
**状态：** 已获用户批准，待实施
**适用版本：** `commodity-v1`

## 1. 背景与决策

当前会话规则只保存 `night_end`，隐含所有夜盘都从 21:00 开始；权威资产
`carry_minute_no_night_dates.csv` 也只能表达整晚无夜盘。该模型无法表达大商所
2019-12-25 晚间延迟至 22:30 开盘、归属目标交易日 2019-12-26 的异常会话。

本设计采用统一异常资产：用 `carry_minute_session_exceptions.csv` 直接替换尚未发布的
`carry_minute_no_night_dates.csv`，并为最终会话规则增加 `night_start`。不保留旧格式
兼容层，不升级 `commodity-v1`，因为正式会话资产尚未发布，当前不存在已发布消费者的
迁移义务。

批准 schema 不等于批准伪造权威事实。DCE 2019-12-26 的正式异常行只有在权威公告被
取得、归档并在来源登记中标为 reviewed 后才能写入仓库资产。

## 2. 目标与非目标

### 2.1 目标

- 精确表达常规夜盘、日盘-only、单日无夜盘和异常延迟开盘。
- 让经验分钟边界与权威异常记录双向、逐键、逐时间匹配。
- 保持最终会话规则可折叠、可复读、可哈希并可由分钟回测器直接消费。
- 对缺失权威、额外权威、非法时间、未审计间隙和经验/权威不一致全部 fail-closed。
- 保留现有产品级长期日盘制度与流动性历史例外的职责边界。

### 2.2 非目标

- 不在本变更中填补 AL1803 2018-01-02 的原始分钟数据。
- 不生成不完整或仅覆盖五品种 smoke 的临时正式会话资产。
- 不改变分钟 VWAP、15 分钟止损、账户、查询计划或日线信号逻辑。
- 不把自由格式 JSON segments 引入权威资产或最终规则。
- 不为未发布的旧 `no_night_dates` 格式提供兼容读取。

## 3. 文件与职责

### 3.1 权威异常资产

删除：

```text
config/carry_minute_no_night_dates.csv
```

新增：

```text
config/carry_minute_session_exceptions.csv
```

精确表头：

```text
exchange,version,trade_date,night_start,night_end,reason,source_url
```

每一行表示一个交易所、一个目标交易日的权威会话异常。键为
`(exchange, trade_date)`；重复键一律拒绝。

示例语义：

```csv
exchange,version,trade_date,night_start,night_end,reason,source_url
SHFE,commodity-v1,2024-02-19,none,none,holiday_no_night,https://...
DCE,commodity-v1,2019-12-26,22:30,23:00,delayed_night_open,https://...
```

示例只定义格式；在来源进入 reviewed 状态前，不得把示例 URL 或事实行直接写入正式
资产。

### 3.2 产品长期制度资产

`config/carry_minute_day_only_regimes.csv` 保持不变，继续表达某产品在一个连续日期范围内
长期没有夜盘。单日、交易所级或改变夜盘起止时间的异常不得塞入该资产。

### 3.3 流动性历史例外

`config/carry_liquidity_history_exceptions.csv` 保持不变，只授权日线流动性历史缺口，不能
授权分钟会话缺失或异常时间。

### 3.4 最终会话规则

`config/carry_minute_sessions.csv` 使用精确表头：

```text
exchange,product,effective_start,effective_end,night_start,night_end,version
```

语义示例：

```csv
SHFE,RB,2020-01-02,2020-01-31,21:00,23:00,commodity-v1
DCE,I,2019-12-26,2019-12-26,22:30,23:00,commodity-v1
DCE,JD,2014-01-01,2014-12-31,none,none,commodity-v1
```

## 4. 时间模型与严格校验

### 4.1 夜盘时钟

继续使用现有目标交易日逻辑时钟：

- 21:00 映射到 `-180`；
- 23:00 映射到 `-60`；
- 23:30 映射到 `-30`；
- 午夜映射到 `0`；
- 01:00 映射到 `60`；
- 02:30 映射到 `150`；
- 22:30 因此映射到 `-90`。

负偏移落在 `previous_trade_date` 晚间；非负且小于日盘起点的偏移落在
`previous_trade_date + 1 calendar day`；日盘槽仍落在目标 `trade_date`。

### 4.2 合法值

`night_start/night_end` 必须同时满足以下条件：

1. 两者同时为 `none`，或两者都为严格 `HH:MM`；一端为 `none`、另一端不是时拒绝。
2. 时间必须是 15 分钟网格上的值。
3. 时间必须落在商品夜盘逻辑范围 21:00 至次日 02:30 内。
4. 逻辑偏移必须满足 `start < end`。
5. 区间长度必须能被 15 分钟整除，保证十五分钟桶不跨越会话边界。
6. `version` 必须精确等于 `commodity-v1`。

最终规则的常规行通常使用 `night_start=21:00`；异常资产不限制为固定枚举，但仍受上述
时钟、网格和顺序约束，以便表达未来有权威记录的 21:30、22:00 或 22:30 异常，而无需
再次改变 schema。

## 5. 内部数据契约

`cta_carry.session_authority` 用冻结数据类 `SessionException` 替换 `NoNightDate`：

```python
@dataclass(frozen=True, order=True)
class SessionException:
    exchange: str
    version: str
    trade_date: date
    night_start: str
    night_end: str
    reason: str
    source_url: str
```

`SessionAuthority` 字段从 `no_night_dates` 改为 `session_exceptions`；哈希清单键从
`no_night` 改为 `session_exception`。loader 必须从同一次不可变字节快照解析并计算
SHA-256，延续现有防 TOCTOU 行为。

`cta_carry.minute_sessions.SessionRule` 继续存储不可变 `SessionSegment`，但 CSV loader 不再
根据单个 `night_end` 查固定映射，而是把经过严格校验的 `night_start/night_end` 转换成一个
夜盘 segment；`none,none` 不添加夜盘 segment。三个标准日盘 segment 保持不变。

## 6. 经验边界与权威对账

边界分类结果从单个 `night_end` 改为：

```text
(night_start, night_end)
```

分类继续要求标准三个日盘段完整。夜盘经验边界必须落在合法逻辑时钟上并满足 15 分钟
网格；否则产生确定性 ambiguity，不做四舍五入或推断。

对每个 `(exchange, product, trade_date)`：

1. 若命中唯一产品级 day-only regime，经验值必须为 `none,none`；**同时命中 session
   exception 时 day-only 优先**，该行不消费例外。
2. 若命中唯一 session exception，经验起止必须与异常行精确相等。
3. 若没有任何 authority，只有从 21:00 开始且结束于正常已支持终点的完整夜盘才通过。
4. 经验值为 `none,none`、开始时间不是 21:00，或其他异常区间时，缺少 exception 必须
   fail-closed。
5. authority 宣称异常但经验边界正常、不同或不存在时必须 fail-closed。
6. 加载日历内存在但未被任何经验键消费的 session exception 必须报告为额外 authority，
   阻止最终发布。

异常按交易所/目标交易日授权，并应用于该交易所当天**有夜盘的**被审计产品。若同一交易所当天
不同产品产生不同经验边界，不能用一条 exception 掩盖，必须报告数据冲突。

### 6.1 规则 1 的优先级为什么是这样（2026-08-19 修订）

原规则 1 要求 day-only 命中时**不得同时命中** session exception。实测该要求与例外的
交易所级作用域结构性冲突：节前「当晚无夜盘」必须按所登记（否则该所有夜盘的品种全炸），
一登记就必然罩住同所的日盘品种，于是两个权威都说「当晚没有夜盘」时代码反而 fail-closed。
碰撞次数 = 日盘品种数 × 节前日数，2020-07→2026-01 窗口实测 **216 条**，
导致 `ambiguous=0` 结构上不可达、正式资产永远发不出来。

改为 day-only 优先的依据：session exception 描述的是**该所当晚的夜盘时段**，
而日盘品种根本不参与夜盘，不在这句话的射程内。两者在 `none,none` 上是同一事实的两种说法。

**闸门没有放宽**：观测仍必须是 `none,none`。若某品种被错误登记成 day-only 而当晚
其实交易了，观测就是一个真实区间，规则 1 照样 fail-closed
（`tests/test_carry_session_authority.py` 的
`test_day_only_product_that_traded_at_night_still_fails_closed` 直接钉住这条）。
day-only 行不消费例外，故规则 6 的未消费检查仍然有效。

证据与替代方案的比较见 `docs/research/2026-08-19-batch-cd-replay-verification.md`。

## 7. 折叠、复读与原子发布

分类后的逐产品交易日记录包含：

```text
exchange,product,trade_date,night_start,night_end
```

只有同时满足下列条件的记录才能折叠为一个 effective range：

- 交易所和产品相同；
- `night_start` 与 `night_end` 都相同；
- 两个目标交易日在全局已审计交易日历中相邻；
- 两天都存在于 `audit_keys`。

未审计间隙不得外推。发布流程继续写 sibling 临时文件，随后：

1. 用严格 loader 复读；
2. 展开 effective ranges；
3. 要求反向键集合精确等于 `audit_keys`；
4. 要求每个经验边界与复读规则一致；
5. 重新核对 authority 文件哈希；
6. 仅在所有检查通过后 `os.replace` 原子发布。

任何失败都不得覆盖既有正式文件。

## 8. 迁移与 provenance

本变更一次性完成以下重命名，不保留兼容别名：

- `NoNightDate` → `SessionException`；
- `no_night_dates` → `session_exceptions`；
- `load_no_night_dates` → `load_session_exceptions`；
- `matching_no_night_dates` → `matching_session_exceptions`；
- `validate_no_night_calendar` → `validate_session_exception_calendar`；
- authority hash key `no_night` → `session_exception`；
- CLI/诊断字段 `no_night_sha256` → `session_exception_sha256`。

来源登记与 capture audit 必须记录新资产行数、重复键数、已消费/未消费 exception 数和
SHA-256。`commodity-v1` 保持不变；若旧正式资产已对外发布的事实后来被发现，则停止实施并
重新设计显式版本迁移，不能静默复用本方案。

## 9. 错误与失败策略

所有错误保持结构化、带 exchange/product/trade_date/check/context。至少区分：

- `authority_csv_header`：表头不精确；
- `authority_duplicate_key`：异常键重复；
- `authority_csv_time`：时间格式、网格、范围或顺序非法；
- `authority_match_cardinality`：同键不是恰好零或一行；
- `night_authority_conflict`：经验与 authority 不一致；
- `session_exception_unconsumed`：加载日历内存在额外异常；
- `session_rule_time`：最终规则起止非法；
- `session_rule_replay`：发布后复读/展开不等于经验审计键。

不得通过替代合约、日线合成、填零、删除审计键、放宽 cardinality 或把异常误标为
`none,none` 来消除错误。

## 10. 测试与验收

实施必须采用 TDD，并至少覆盖：

1. 新异常 CSV 精确表头、空文件、重复键、版本、日期、必填字段和原始字节哈希。
2. `none,none`、`21:00/23:00`、`21:00/23:30`、`21:00/01:00`、
   `21:00/02:30`、`22:30/23:00` 的解析与槽位。
3. 一端 `none`、非 15 分钟网格、越界、倒序和零长度区间拒绝。
4. DCE 2019-12-26 经验 `22:30/23:00` 在有精确 exception 时通过，无 exception、
   时间不同或额外 exception 时失败。
5. 产品 day-only 与 session exception 同时命中且观测为 `none,none` 时**放行**且不消费例外
   （无论例外写的是 `none,none` 还是具体夜盘时段）；同一情形下观测为真实区间时仍失败。
6. 同交易所同日不同产品边界冲突失败。
7. 相邻同值折叠；起点或终点不同即断开；未审计日间隙不得折叠。
8. loader replay、反向键相等、哈希变化、临时文件清理与原子发布回滚。
9. repository authority 资产只含新 schema；旧文件和旧公开接口不存在。
10. 现有分钟时钟、采集、authority、回测和 CLI 聚焦回归全部通过。

正式资产发布的额外验收条件保持不变：全历史 capture 必须 `ambiguous=0`、unkeyable 为零、
动态访问键属于 `audit_keys`，且正式 `carry_minute_sessions.csv` 复读成功。

## 11. 外部数据门

schema 实施后仍有两个独立数据门：

1. DCE 2019-12-25 延迟开市公告当前为 `pending_manual_fetch`。在原始公告或等价权威资料
   被取得并登记前，只能测试合成 DCE 异常，不得写正式 2019-12-26 authority 行。
2. AL1803 2018-01-02 日盘分钟数据缺失。必须从 MyQuant、Wind 或上期所授权档案取得未经
   补值的 OHLCV、成交额和持仓量；缺口未解决前不得声称全历史 capture 成功。

因此，本 schema 可以独立完成并通过合成/单元测试，但 Task 12 五品种正式 smoke 只有在
权威资产完整、全历史 capture `ambiguous=0` 且最终规则原子发布后才能继续。
