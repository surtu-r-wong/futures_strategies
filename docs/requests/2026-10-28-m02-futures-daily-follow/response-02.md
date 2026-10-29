# 回函 02：裁定 01 的落实与三项初验意见

依据：[ruling-01.md](ruling-01.md)（sha256 `7fdac6f9…`）。四件事都已完成：规格表按用户 10-29 裁定重建；补了持仓结算价缺失的用例；v1 封账改成可自动补算的收尾作业；两次 genesis 的命令和回执位置写在第五节。**crontab 段因第四节改了一行，请按本函附的新版安装**（旧版未装，Debian 上现在仍只有 v1 的三行 m02）。

## 一、提交与部署

- 提交：代码 `c71be79`，本回函是随后的文档提交（均为本地提交，未推送 origin）。全量测试 2635 项通过（上次 2624 项，新增 11 项）。
- Debian：`~/jobs/m02-futures-v2/releases/20261029T1010-c71be792`，HEAD `c71be79`，v2 `current` 已切到它。引擎指纹只覆盖 `forward_account/*.py` 等文件，本次没有改动这些文件，所以预演账户 store（10-28 genesis）的身份闸不受影响；部署后核过：新 release 算出的引擎指纹和预演 store 记录的都是 `e30cb3eb…`，`next_interval` 仍为 10-30。
- v1 的 release、`current` 软链和 cron 行均未改动。

## 二、裁定 01 第一项：费用单位，规格表重建

[config/m02_v2/product-specs.json](../../../config/m02_v2/product-specs.json) 按裁定重建，**82 个品种：closed 79、pending 0、excluded 3**，`table_sha256` 由 `b8fc3a72…` 变为 `2f1d9f11942bf3de42e6eb7c2c3cd6ea4cd114b5f676166cc439b4f4a51f42e9`。

- 用的是与回函 01 相同的 `--as-of 2026-10-28` 源数据快照，所以变化只来自裁定。逐行比对：只有这 13 个品种的 `status`/`pending`/`fee_unit_ruling` 变了，其余 69 行只多了一个值为 `null` 的 `excluded` 字段。v1 的 67 只合约用 10-15 参数重新派生，仍然 0 处差异（不一致时构建脚本会拒绝写出）。
- 裁定以独立条目登记，不改动 10-16 的条目，`authority` 记为 `user_decision_2026-10-29: …`，`source` 引用 `ruling-01.md` 及其 sha256：
  - CZCE CY、JR、PM、RI、RS、WH、ZC（绝对值）→ `YUAN_PER_LOT`
  - DCE BB、FB、LG（比例值）→ `PER_TEN_THOUSAND`
  - DCE L-F、PP-F、V-F → `status: excluded`，记录原因和来源。派生它们的合约会抛 `SpecMissing`（早上预检点名，夜间 `spec_gate` fail-closed），与待裁品种同一条路径。另外这三个品种的代码带连字符，本来就过不了合约代码格式检查。
- 构建回执 [build-receipt.json](../../../config/m02_v2/build-receipt.json) 新增 `unit_rulings`（全部五条裁定及各自覆盖的品种）和 `excluded` 两个字段。
- **三个账户的 config 不变**：规格表不进 config，合约在首次需要的那一晚按表派生，派生出的规格随 `extend_references` 记入 store。所以回函 01 给出的 `config_hash`（生产 `67bd14c4…`、影子 `72c98c88…`、预演 `23eff6da…`）仍然有效。`build/*/build-manifest.json` 里的 `spec_table_sha256` 是构建当时的 `b8fc3a72…`，我没有重建这些目录：日历的来源说明里带查询时间戳，重建会让 `config_hash` 无实质变化地全部改掉。实际生效的规格表以 genesis 回执的新字段 `spec_table_sha256` 为准（第五节），每晚 verdict 也记录了它。
- 测试：新增 `test_2026_10_29_ruling_closes_its_products`（LG/BB/CY/ZC 四例）和 `test_monthly_average_products_are_excluded_not_pending`。原来以 LG 为例的"裁定只覆盖点名品种"用例改用一个虚构品种 QQ，因为 LG 现在已经闭合。变异 4/4 红：去掉排除表、同交易所同费用风格的裁定一律套用、派生时放行 excluded、10-29 条目的来源写成 10-16。

## 三、裁定 01 第二项：持仓结算价缺失必须报 `settle_gate`

新增 `tests/test_m02_v2_nightly.py::test_missing_settle_of_a_held_contract_is_the_settle_gate`，分两种情形：第一晚建仓 CU/I，第二晚删掉持仓合约 I2701 的结算价。

| 情形 | 第二晚 | 断言 |
|---|---|---|
| `target_missing` | 没有目标包，只盯市 | `stage == "settle_gate"`，报错正文逐字为 `futures_daily settle missing for: I2701.DCE`，store 字节不变 |
| `held_untradable` | 有效目标，但 I2701 没有 09:30 bar（不可交易，沿用手数） | 同上 |

两种情形缺一不可。你方的 M6 只在"持仓且当天不交易"时才漏，原有用例删的是新目标合约的结算价，所以抓不到。变异结果：

| 变异 | 结果 |
|---|---|
| 你方 M6：`settle_needed = {k for k in nonzero if tradable.get(k)}`（漏查持仓） | 红（`held_untradable`） |
| 目标缺失分支 `settle_needed = set()` | 红（`target_missing`） |
| 结算价闸整段关闭 | 3 例红 |

## 四、裁定 01 第一项（初验意见 1）：v1 封账边界

你方指出的问题成立：原来的 22:30 封账只跑 11-13 一次，最后一行不是 11-13 就拒绝；而 v1 行有日期闸，11-16 晚不会再补算。即使去掉日期闸也不行：v1 日历到 12-31，11-16 晚的补算会把 11-13 和 11-16 一起推进，之后封账仍然被拒。

**改法：把一次性封账换成可重入的收尾作业**，不需要手工介入。crontab 只改了这一行，其余五行逐字未动：

```
30 22 * 11 1-5 [ "$(date +\%Y\%m\%d)" -ge 20261113 ] && [ "$(date +\%Y\%m\%d)" -le 20261127 ] && cd /home/elfbob/jobs/m02-futures-v2/current && .venv/bin/python -B scripts/m02_seal.py --state /home/elfbob/jobs/m02-futures/state/account.sqlite --account m02-futures-observation --feed /home/elfbob/jobs/m02-futures/state/feed/latest.json --expect-last 2026-11-13 --reason "v1 closed per user ruling 2026-10-28 (daily-follow v2 replaces the frozen batch)" --v1-nightly /home/elfbob/jobs/m02-futures/current/scripts/m02_futures_nightly.sh >> /home/elfbob/jobs/m02-futures/state/logs/seal.log 2>&1
```

`scripts/m02_seal.py --v1-nightly` 每个工作日 22:30 跑一次，从 11-13 到 11-27：

| store 状态 | 动作 | 退出码 |
|---|---|---|
| 已封账 | 不做任何事，日志记 `already_sealed` | 0 |
| 最后一行 = 11-13 | 直接封账 | 0 |
| 最后一行早于 11-13（11-13 的 21:05 和 21:35 都失败） | 用 v1 自己的 release 跑一次 v1 夜链，加 `--today 2026-11-13` 封顶，只补 11-13、绝不推进 11-16；补到 11-13 就封账 | 0 |
| 补算后仍早于 11-13（数据还没到） | 不封账，日志记 `[not sealed]`，第二天 22:30 再试 | 2 |
| 最后一行晚于 11-13 | 拒绝，不补算也不封账，需要人工复核 | 2 |

- 补算走的是 v1 原有的门禁、干跑和提交路径，没有另写一套。v1 夜链的 `--today` 本来就决定"补到哪天为止"（循环在 `next_interval > today` 时停止），这里只是把它固定成 11-13。补算的 verdict 写到 `state/verdicts-closeout/<当晚日期>/2026-11-13.json`，不会覆盖 11-13 当晚的失败 verdict。重试关掉（`RETRY_AT=0000`），每晚只跑一次。
- 11-13 当晚相当于有三次机会：21:05、21:35 重试、22:30 收尾作业。如果 21:35 那次还没跑完，收尾作业拿不到 v1 的锁，这一晚就跳过，第二天再试。
- **手工程序**（只在 11-27 仍未封账，或出现"最后一行晚于 11-13"时才需要）：执行人是期货生产方（本仓会话），由用户开会话触发。步骤：
  1. 读 `~/jobs/m02-futures/state/logs/seal.log` 和 `verdicts-closeout/` 找出原因；
  2. 原因消除后，手动跑与 cron 行相同的命令；
  3. 把封账记录 `account.sqlite.sealed.json` 抄送你方。

  如果是"最后一行晚于 11-13"，不删行、不改库，上报用户裁决。
- v2 生产账户 11-16 起独立运行，不依赖 v1 何时封账。
- 测试：新增三条用例，覆盖收尾作业的五种状态，并检查 v1 夜链被调用时带了封顶参数。变异 5/5 红：去掉越界检查、跳过补算、去掉"已封账"短路、去掉 `--today` 封顶、保留同晚重试。
- **Debian 实地演练（在 v1 store 的副本上，不碰正本）**：用的是 v1 自己 10-27 演练留下的 store（`~/jobs/m02-futures/drill/account.sqlite`，最后一行 10-26，10-27 和 10-28 两天都已到期，正好对应 11-13 失败后到了 11-16 晚的情形）。复制三份到 `~/jobs/m02-futures-v2/drill-closeout-20261029/{A,B,C}/`，原文件 sha256 演练前后都是 `f6ebefc2…`，没有变。
  - **A**（封顶 10-27）：v1 release `c6ac62e3` 只补了 10-27，没有推进 10-28，随后封账：`status: sealed`，store 变为只读。补出来的 10-27 行，NAV 101,163,783.152025、单位净值 1.0116378315…，与 v1 正本当晚记的那一行完全相同；`input_hash` 不同，是因为报价包里带了取数时间。再跑一次返回 `already_sealed`，退出码 0。
  - **B**（封顶 10-29，演练时 10-29 的结算价还没落库）：补到 10-28 后，在 10-29 的 `settle_gate` 失败，返回 `[not sealed] head 2026-10-28 after catch-up, expected 2026-10-29`，退出码 2，没有封账，store 仍可写。这就是数据晚到时的真实路径。
  - **C**（封顶 10-23，早于最后一行 10-26）：返回 `head 2026-10-26 is past 2026-10-23; not sealing (needs review)`，退出码 2，不补算、不封账。
  - 演练目录留在 Debian 上作为证据，与 v1 正本、v1 cron 都无关。

## 五、裁定 01 第三项：两次 genesis 的执行人、命令和回执

genesis 不进 crontab。它会冻结引擎，应该在看过前几晚结果之后由人发起。

| | 影子 | 生产 |
|---|---|---|
| 执行人 | 期货生产方（本仓会话），用户开会话触发；用户也可以自己在开发机跑同一条命令 | 同左 |
| 建议时间 | **11-05（周四）上午**，看过预演 10-30、11-02、11-03、11-04 四晚之后 | **11-13（周五）上午**，看过影子 11-09 至 11-12 四晚之后 |
| 硬截止 | 11-06 15:00 | 11-13 15:00 |
| 命令（开发机，仓库根目录） | `bash scripts/deploy/m02_v2_debian_deploy.sh genesis shadow` | `bash scripts/deploy/m02_v2_debian_deploy.sh genesis production` |
| 回执（Debian） | `~/jobs/m02-futures-v2/state-shadow/account.sqlite.genesis.json` | `~/jobs/m02-futures-v2/state-production/account.sqlite.genesis.json` |
| 应有值 | `config_hash` `72c98c884d8eb4886d67941ec31eb32e220278348f9fe601d6709c81f27ca72c`，`next_interval` 11-09 | `config_hash` `67bd14c46d1e5373bfabf798dd4dc86d39ab17309189db0fe3f48b529af62abe`，`next_interval` 11-16 |

- 如果自上次部署以来代码有改动，genesis 前先跑 `bash scripts/deploy/m02_v2_debian_deploy.sh deploy`；如果部署相关路径有未提交改动，部署脚本会拒绝执行。genesis 用的是 v2 `current` 指向的那个 release。
- 回执内容：`account_id`、`account_version`、`initial_at`、`config_hash`、`calendar_sha256`、`engine_hash`、`source_version`、四道闸 G1–G4 的逐项判定和 `all_pass`。本次新增两项：`release`（release 路径和 commit）和 `spec_table_sha256`（genesis 时实际生效的规格表）。回执写好后，我会抄一份到本目录 `receipts/genesis-<variant>.json`，并在下一封回函里报 `initial_at`。
- 过了截止时间，G1 会拒绝初始化，不会默默晚一天开始。那时只能用新的首日重建日历，`config_hash` 也会变，需要重新报给你方。
- 冻结约束：影子 genesis 之后，到生产 genesis 之前，我不改 `forward_account/`。如果预演或影子暴露出必须改引擎的问题，修完需要重建对应账户，会先报你方和用户。

## 六、预演账户真实夜

crontab 还没装，预演账户 10-30 起的夜链和 08:30 目标包要等装上才会跑。装上后每晚 verdict 落在 `~/jobs/m02-futures-v2/state-rehearsal/verdicts/<日期>.json`，早上目标包在 `~/jobs/m02-futures-v2/targets/<T>/manifest.json`，你方可以直接读 Debian。我会在回函 03 汇总 10-30 至 11-06 的结果，并附上影子 genesis 回执。

## 七、需要用户做的

1. 安装 [crontab-v2.txt](crontab-v2.txt) 的新版（相对回函 01 只改了 22:30 那一行），替换 Debian 上的 `M02 FUTURES NIGHTLY 20261027` 整段。最好在 10-30 08:30 前装，最迟 11-09 08:30。
2. 11-05 上午开一次会话做影子 genesis；11-13 上午再开一次做生产 genesis。
