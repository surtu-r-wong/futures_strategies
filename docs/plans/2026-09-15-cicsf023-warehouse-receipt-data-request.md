# 数据请求函：期货标准仓单数量（CICSF023 仓单策略指数复刻）

| | |
|---|---|
| 请求方 | `futures_strategies`（elfbob） |
| 日期 | 2026-09-15 |
| 用途 | 复刻中信期货 CICSF023 仓单策略指数 |
| 依据研报 | `/home/elfbob/exchange/20260903/中信期货仓单策略指数编制方式.pdf` |
| 治理级别 | M1（`commodity_research.*` 与 `product_mapping` 均已登记为 M1） |
| 样本数据 | `/home/elfbob/exchange/20260915/meg仓单注册量.csv`（MEG，242 个交易日） |

**本函只请求数据，不请求日更管道。** 按 `futures_strategies` 的仓库边界，Wind 抽取、
writer 与 DDL 属 market-monitor / db_manager；本项目只做只读消费。

---

## 1. 为什么需要

研报 §3.1 的因子是仓单年同比变化率：

```
TS = C_p / C_{300-200} − 1
```

`C_p` = 最近 p 个交易日的标准仓单数量均值；`C_{300-200}` = 前 300 到前 200 个交易日的
仓单数量均值。**这是策略唯一的因子输入，没有它整条复刻不成立。**

### 1.1 现有数据不可用（已实测）

`public.commodities_spot_prices` 有 `attribute='仓单'` 的 93,955 行（50 品种，
2003-07-08..2021-01-13），**但那一列存的是价格，不是数量**。2021-01-12 实测：

| 品种 | `attribute='仓单'` 的值 | 当日主力收盘 | 比值 |
|---|---:|---:|---:|
| HC | 4540 | 4462 | 1.02 |
| L | 7550 | 7560 | 1.00 |
| RB | 4250 | 4327 | 0.98 |
| J | 3070 | 2773 | 1.11 |
| ZC | 990 | 719 | 1.38 |

七个品种全部与当日主力收盘同量级，而真实仓单量是"十几万吨 / 上万张"的量级。
该表另已登记为 `PAUSED`（DEC-L，无活跃 writer），数据止于 2021-01-13。

全库另行确认：`public.spot_prices` 只有现货价；`public.inventory` 只有豆粕的
压榨厂/饲料厂库存（1 个品种）；按列名搜 `warrant/receipt/registered` 的命中项
全部是 `data_manager` 的事务收据。**库内没有任何期货标准仓单数量序列。**

---

## 2. 请求项（五条，有依赖顺序）

所有 `dataset_id` 均取自 `data_manager.dataset` 现有登记（`change_request.dataset_id`
有外键约束）。

### R1 — `DDL`：扩两处 CHECK 枚举

| | |
|---|---|
| dataset_id | `commodity_research.series_catalog` / `commodity_research.fundamental_daily` |
| request_type | `DDL` |
| 依赖 | 无（最先执行） |

现有约束不接受仓单这个 metric：

```sql
-- 现状
series_catalog_metric_role_check
  CHECK (metric_role = ANY (ARRAY['spot','inventory','profit_direct',
                                  'profit_component','conversion_component']))
fundamental_daily_metric_check
  CHECK (metric = ANY (ARRAY['spot','basis_rate','inventory','profit']))
```

请求各增加一个取值 `'warehouse_receipt'`。**不新增表、不改列、不改主键。**

> 命名建议用 `warehouse_receipt` 而非 `warrant`：研报定义的是"期货标准仓单"
> （交割仓库签发的实物提货凭证），`warrant` 在金融语境里指认股权证，容易误读。
> 最终命名请管理者裁定，本函其余部分按 `warehouse_receipt` 书写。

### R2 — `product_mapping`：新增 28 个品种

| | |
|---|---|
| dataset_id | `commodity_research.product_mapping` |
| request_type | 建议 `BACKFILL`（参考数据补齐；若管理者认为属例行维护可改 `ROUTINE_UPDATE`） |
| 依赖 | 无 |

现有 9 个品种（AL / BU / CU / M / MA / PP / RB / RU / TA）全部在研报名单内，
研报 §3.2 点名 37 个品种，故需新增 **28 个**：

| 交易所 | 新增品种 |
|---|---|
| SHFE | AU 沪金、AG 沪银、ZN 沪锌、NI 沪镍、HC 热轧卷板、FU 燃油、SP 纸浆 |
| DCE | J 焦炭、JM 焦煤、I 铁矿石、V PVC、PG 液化石油气、C 玉米、L 塑料、P 棕榈油、Y 豆油、JD 鸡蛋、CS 玉米淀粉、EG 乙二醇 |
| CZCE | FG 玻璃、CF 郑棉、OI 菜油、SR 白糖、RM 菜粕、ZC 动力煤、SM 锰硅、AP 苹果 |
| INE | SC 原油 |

⚠️ `product_mapping_currency_check` 限定 `currency='CNY'`；上述品种（含 INE 原油）
均为人民币计价，不冲突。`futures_quote_unit` 请按各交易所合约单位填写。

### R3 — `series_catalog`：新增 37 条仓单 series，**并开 `catalog_version = v2`**

| | |
|---|---|
| dataset_id | `commodity_research.series_catalog` |
| request_type | 建议 `BACKFILL` |
| 依赖 | R1（枚举）、R2（`product_code` 外键） |

**必须开 v2，不能往 v1 里加。** `fundamental_build` 记录 `catalog_version`，现存
`conservative-20260818T060911Z-622e88840fa3`（`complete`，82,943 行）引用 v1 并已被
CTA 端到端消费；`series_catalog` 主键是 `(catalog_version, series_id)`，往 v1 追加
会改变 v1 的内容，使那次 build 不可复现。v2 应包含 v1 的全部 25 条 series
（9 inventory + 9 spot + 6 profit_component + 1 profit_direct）加本次 37 条。

每条 series 按现有 `inventory` 的填法（模板取自 `al.inventory.primary`）：

| 字段 | 值 | 备注 |
|---|---|---|
| `catalog_version` | `v2` | |
| `series_id` | `<product 小写>.warehouse_receipt.primary` | 如 `eg.warehouse_receipt.primary` |
| `source` | `wind` | CHECK 限定 |
| `source_code` | **待填** | Wind EDB 码，格式 `S#######` |
| `source_name` | **待填** | EDB 指标中文全名 |
| `product_code` | 如 `EG` | 外键 |
| `metric_role` | `warehouse_receipt` | 依赖 R1 |
| `frequency` | `daily` | 样本实测为日频，见 §3 |
| `api_method` | `edb` | 与现有 inventory 一致 |
| `field_name` / `wind_options` | 空 | EDB 无字段概念 |
| `source_unit` / `target_unit` | **待填** | 吨 / 张，逐品种可能不同 |
| `scale_multiplier` | `1`（待确认） | |
| `aggregation_rule` | `primary` | |
| `date_semantics` | `observation_date` | |
| `release_lag_rule` | `{"time": null, "calendar_days": 0}` | 仓单为交易所收盘后公布，若实际有时滞请按实填 |
| `unknown_time_policy` | `next_trading_close` | CHECK 限定 |
| `max_staleness_trading_days` | 建议 `3`（日频序列，比 weekly 的 10 严） | 请管理者裁定 |

### R4 — `fundamental_observation`：回填 2008-10-01 .. 2026-09-14

| | |
|---|---|
| dataset_id | `commodity_research.fundamental_observation` |
| request_type | `BACKFILL` |
| 依赖 | R3 |

**起点为何是 2008-10**：指数基日 2010-01-04（研报 §3.5，亦与官方序列
`CICSF023.WI` 的首日一致），因子分母要回看到前 300 个交易日 ⇒ 约需提前 15 个月。
早于各品种上市日的部分自然缺席，按现有 `known_absence` 机制登记即可。

- `vintage_quality` 用 `backfill_final`（CHECK 三选一中对应历史回填的那个）
- 需先建 `fundamental_ingest_run`（`fundamental_observation.run_id` 有外键）
- 唯一键 `(run_id, catalog_version, series_id, observation_date)`
- 量级估算：37 品种 × 约 4,100 个交易日 ≈ **15 万点**。今日网关配额
  `used=893 / max=5e8`，占用可忽略；但历史上 `wsd` 常被其他任务耗尽，
  建议走 `wind_usage_reservation` 预约窗口。

### R5 — `fundamental_daily`：重算出 `warehouse_receipt` metric

| | |
|---|---|
| dataset_id | `commodity_research.fundamental_daily` |
| request_type | `REBUILD` |
| owner | `db_manager`（该 dataset 的 owner_project 即为 db_manager） |
| 依赖 | R1、R4 |

新 `build_version` 基于 `catalog_version=v2`，产出 `metric='warehouse_receipt'`。
v1 的既有 build 不动。

---

## 3. 样本数据检查结论（`meg仓单注册量.csv`）

**格式与质量可用**，据此确认取数口径正确：

| 检查项 | 结果 |
|---|---|
| 行数 / 区间 | 242 行，2025-09-15 .. 2026-09-14（末日与 `futures_daily` 截断日一致） |
| 缺失日 | 工作日缺 19 天，**全部为法定节假日**（国庆 10-01~08、元旦、春节 02-16~23、清明 04-06）⇒ 日频无缺口 |
| 空值 / 重复日期 | 0 / 0 |
| 数值 | min 0、中位 7,566、max 15,734 ⇒ **是数量不是价格** |
| 频率性质 | 日变化占比 65.3%，最长不变游程 12 天 ⇒ 真日频**存量**序列，非流量、非周频填充 |

CSV 结构为 Wind 导出的 `"DateTime","CLOSE"` 两列，文件尾带"数据来源：Wind"。

### 3.1 正式交付需要补的元数据

样本仅有日期与数值，`series_catalog` 的四个必填项无法从文件名推得，且 37 个文件
靠文件名映射 `product_code` 不可靠。**请随数据附一张 37 行的清单**：

```csv
product_code,source_code,source_name,source_unit,frequency
EG,S#######,期货注册仓单量:乙二醇,吨,daily
```

⚠️ 样本文件名为 `meg`，而大商所乙二醇的品种代码是 **EG**；Wind 指标名可能用 MEG。
映射以 `product_code` 列为准，不以文件名为准。

---

## 4. 需要管理者同时裁决的一件口径问题

样本中 **2026-03-31 仓单为 0**，前后为 `03-30 1856 → 03-31 0 → 04-01 1800 → 04-02 7500`，
是典型的**集中注销后重注册**，不是缺数据。

**研报对此完全没有规定**——§3.1 公式无除零保护，§3.2 的"特殊调整"只列了涨跌停与
退市两条（"其他特殊情况"未展开），§3.5 第一步只复述公式。研报唯一沾边的表述在第 1 页，
且是把注销当作**信号**：「大量仓单被注销，说明现货价格高于期货价格，多头力量占优」——
按 §3.5 第二步"从大到小排序"，仓单降得最多的品种会被推到做多端。

于是有两个层次的问题：

1. **技术层**：`C_{300-200}` 那 100 个交易日若全为 0，因子除零。
   建议按本仓既有口径 C（"形不成证据就不覆盖"）处理：分母无效时该品种当日不进截面，
   并落可查清单。**这条属策略侧裁决，不影响本请求函，由用户拍板。**
2. **数据层（与本函相关）**：如果集中注销是**制度性**的（交易所标准仓单有效期规则），
   它就是每年同月重复的季节现象，会把受该制度约束的品种在注销月系统性推向做多端；
   横截面排序只有在所有品种同时注销时才抵消得掉，而各交易所规则不同。

   ⇒ **请求在 R4 交付后附一项验收检查**（见 §5 第 5 条）。这一条是推论，尚无证据，
   正需要全历史数据来判。

---

## 5. 验收规则（数据到位后由请求方执行并回报）

1. **逐品种覆盖度**：每个 series 的观测日集合 vs `trading_calendar`，缺失日必须落在
   节假日或该品种上市日之前，否则登记 `known_absence`。
2. **数值合法性**：非负；不得为 NULL 冒充 0；单位与 `source_unit` 一致。
3. **与样本对账**：EG 在 2025-09-15..2026-09-14 的 242 个点，须与
   `meg仓单注册量.csv` **逐点相等**（这是确认取的是同一条 EDB 序列的唯一硬证据）。
4. **量级抽检**：任取 5 个品种×5 个日期，与交易所仓单日报核对量级。
5. ⭐ **清零与骤降的时间分布**：统计各品种"值为 0"及"单日降幅 >50%"的**月份分布**。
   - 若集中在固定月份且按交易所聚集 ⇒ 制度性季节现象，策略侧口径须专门处理；
   - 若分散 ⇒ 按 §4 第 1 条的除零规则处理即可。

   这项检查的结论会回写到策略侧设计文档，不改变数据本身。

---

## 6. 明确不请求的事

- **不请求日更管道**。本次只要历史回填；是否纳入 `ROUTINE_UPDATE` 模板由管理者
  按 `commodity_research` 的整体节奏决定。
- **不请求改动 v1**。v1 及其既有 build 保持原样。
- **不请求碰 `public.commodities_spot_prices`**。该表 `PAUSED` 状态不变；建议
  在其 dataset 备注里补一句"`attribute='仓单'` 实为价格"，避免后来者再被列名误导。
- **不请求改网关**。`/fetch/futures` 的字段写死在网关配置（哑管道），加 `st_stock`
  会动到 Wind 网关这一共享资源；本函一律走既有的 `/fetch/edb` 通路。

---

## 7. 待填清单（阻塞 R3 及其后）

| # | 待填 | 由谁提供 |
|---|---|---|
| 1 | 37 个品种的 Wind EDB 码与指标全名 | 用户（Wind 终端导出） |
| 2 | 各品种仓单的单位（吨 / 张 / 手） | 同上 |
| 3 | `metric_role` 最终命名（建议 `warehouse_receipt`） | 管理者 |
| 4 | `max_staleness_trading_days`（建议 3） | 管理者 |
| 5 | R2 的 `request_type`（`BACKFILL` 还是 `ROUTINE_UPDATE`） | 管理者 |

第 1、2 项到位后，本函可从 `DRAFT` 推进到 `VALIDATED`。

---

## 附录 A：可直接插入 `data_manager.change_request` 的记录

`request_type` 与 `dataset_id` 已按表约束校验（枚举合法、dataset 均已登记）。
`request_id` 为占位 UUID，管理者可自行重新生成；`_local_ref` 只是本函内的编号，
不是表字段，插入前请删除。**依赖顺序：R1a/R1b → R2 → R3 → R4 → R5。**

```json
[
  {
    "_local_ref": "R1a",
    "request_id": "3c79e5bc-edf0-46b6-8c32-94b1693c3f26",
    "dataset_id": "commodity_research.series_catalog",
    "request_type": "DDL",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R1a",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "extend_check_enum",
      "constraint": "series_catalog_metric_role_check",
      "add_value": "warehouse_receipt",
      "table_change": "none",
      "column_change": "none"
    }
  },
  {
    "_local_ref": "R1b",
    "request_id": "dd7bf9ae-e103-4189-8512-fc0f5f0f5d72",
    "dataset_id": "commodity_research.fundamental_daily",
    "request_type": "DDL",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R1b",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "extend_check_enum",
      "constraint": "fundamental_daily_metric_check",
      "add_value": "warehouse_receipt",
      "table_change": "none",
      "column_change": "none"
    }
  },
  {
    "_local_ref": "R2",
    "request_id": "1aff3754-2bf2-42c4-8c0a-147acbfb1c59",
    "dataset_id": "commodity_research.product_mapping",
    "request_type": "BACKFILL",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R2",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "insert_reference_rows",
      "product_codes": [
        "AU",
        "AG",
        "ZN",
        "NI",
        "HC",
        "FU",
        "SP",
        "J",
        "JM",
        "I",
        "V",
        "PG",
        "C",
        "L",
        "P",
        "Y",
        "JD",
        "CS",
        "EG",
        "FG",
        "CF",
        "OI",
        "SR",
        "RM",
        "ZC",
        "SM",
        "AP",
        "SC"
      ],
      "count": 28,
      "note": "currency 一律 CNY；futures_quote_unit 按交易所合约单位"
    }
  },
  {
    "_local_ref": "R3",
    "request_id": "2e45894a-e1c8-4b1a-a183-c074ea172843",
    "dataset_id": "commodity_research.series_catalog",
    "request_type": "BACKFILL",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R3",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "publish_catalog_version",
      "new_catalog_version": "v2",
      "carry_forward_from": "v1",
      "carry_forward_series": 25,
      "add_metric_role": "warehouse_receipt",
      "add_series": 37,
      "series_id_pattern": "<product_lower>.warehouse_receipt.primary",
      "api_method": "edb",
      "frequency": "daily",
      "pending_fields": [
        "source_code",
        "source_name",
        "source_unit",
        "target_unit"
      ],
      "reason_new_version": "fundamental_build conservative-20260818T060911Z-622e88840fa3 引用 v1 且已交付；改 v1 破坏可复现性"
    }
  },
  {
    "_local_ref": "R4",
    "request_id": "efcd9d4c-d761-42b0-80e9-2bdfc5e4a7f3",
    "dataset_id": "commodity_research.fundamental_observation",
    "request_type": "BACKFILL",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R4",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "backfill_observations",
      "catalog_version": "v2",
      "metric_role": "warehouse_receipt",
      "range": {
        "start": "2008-10-01",
        "end": "2026-09-14"
      },
      "vintage_quality": "backfill_final",
      "products": 37,
      "estimated_points": 150000,
      "note": "起点提前至 2008-10 是因为因子分母回看前 300 个交易日，指数基日 2010-01-04"
    }
  },
  {
    "_local_ref": "R5",
    "request_id": "e270c84b-a42d-494d-bed7-c78c92d3e60b",
    "dataset_id": "commodity_research.fundamental_daily",
    "request_type": "REBUILD",
    "idempotency_key": "cicsf023-warehouse-receipt-2026-09-15-R5",
    "requested_by": "futures_strategies/elfbob",
    "project_id": "futures_strategies",
    "status": "DRAFT",
    "scope": {
      "action": "rebuild_metric",
      "catalog_version": "v2",
      "metric": "warehouse_receipt",
      "depends_on": [
        "R1b",
        "R4"
      ],
      "leave_untouched": "v1 的既有 build"
    }
  }
]
```

`scope` 内的 `pending_fields` 标出 R3 被 §7 待填清单阻塞的四个字段；
在它们补齐前 R3 及其下游不应推进到 `VALIDATED`。
