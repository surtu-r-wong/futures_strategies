# 项目目标对齐与主线收口设计

- 日期：2026-08-10
- 状态：待用户书面复核
- 适用仓库：`futures_strategies`

## 1. 背景

本仓的稳定边界是商品期货券商研报复刻。国君 CTA 与国信 Carry 都属于初始路线，
因此本次不撤销 Carry，也不把商品基本面误判为新方向。需要纠正的是交付状态：
`master` 默认暴露名义上的六因子运行，但标准基本面 build 尚不存在；Carry 的研究产物
只记录 Git `HEAD`，不足以证明生成时工作树与该提交一致；金融期货过滤和顶层文档也有
已确认的不一致。

本设计按“先阻止误用，再整理分支，随后补可追溯性与边界，最后收口文档”的顺序推进。

## 2. 目标

1. 在 `master` 上让可信的量价路径成为默认值，并让不具备标准基本面数据的六因子运行
   明确失败，不再静默退化成三因子策略。
2. 将 `feature/commodity-fundamentals` 同步到最新主线，同时保持未合并状态，直到真实的
   published conservative build 完成端到端验收。
3. 让新生成的 Carry 报告能够区分干净提交与 dirty 工作树，并能用稳定指纹追溯生成代码。
4. 关闭显式 `--symbols` 绕过金融期货排除的路径，使 CLI 行为符合仓库领域边界和 runbook。
5. 修正文档事实错误，并提供一页权威路线图，明确已完成、实验、外部阻塞和待办。

## 3. 非目标

- 本次不执行 Wind 抓取、数据库 DDL、writer 部署或生产库写入。
- 不伪造基本面 catalog、build、覆盖率或策略收益验收数据。
- 不把 `feature/commodity-fundamentals` 在真实 build 缺失时合入 `master`。
- 不修改 Carry 的信号方向、风险参数或实验参数默认值。
- 不重新生成所有历史 Excel；只在代码可追溯机制落地后生成一个小型、可验证的离线
  smoke 产物。全历史产物需要生产数据和较高资源，另行执行。

## 4. 方案选择

### 4.1 采用：安全顺序、分阶段提交

每个阶段先写失败测试，再做最小实现并独立提交：

1. 主线六因子 fail-closed 与默认量价化。
2. 将基本面分支 rebase 到这个安全检查点；处理 CTA 冲突并跑分支全测，但不合并。
3. Carry 代码指纹与 dirty 状态记录。
4. 金融期货边界修复。
5. README、`CLAUDE.md`、runbook 与项目路线图同步。
6. 主线全部提交完成后，再把基本面分支 rebase 到最终 `master` 并复跑验证，确保它不会
   因后续主线提交再次落后。

优点是每一步都可单独回滚，基本面分支不会把尚未满足的外部前置条件带入主线。

### 4.2 未采用：先合并基本面分支

该方案能立即解决 raw basis 和覆盖闸门问题，但分支落后主线且生产库尚无标准 build，
会把“代码准备好”误写成“六因子可运行”。

### 4.3 未采用：只改文档与默认值

该方案不会解决研究产物无法追溯、金融期货过滤绕过和分支老化，不能完成本次收口目标。

## 5. 行为设计

### 5.1 `master` 的 CTA 入口

- `--factor-set` 默认值从 `six_factor` 改为 `price_volume`。
- `public-pg + six_factor` 在主线 legacy reader 下直接以非零状态退出，错误信息必须说明：
  完整六因子需要 published conservative fundamentals build；当前可使用
  `--factor-set price_volume`，不得建议把 legacy 稀疏表当正式替代。
- 文件源仍允许显式 `six_factor`，但必须在运行前验证 `basis_rate` 或 `spot`、`inventory`、
  `profit` 三类输入在数据集中存在有限值；完全缺失时失败。
- 量价路径的已有权重、收益和报告行为保持逐点不变。

基本面功能分支保留其更严格的 `standard` / `legacy` / `none` 路由。rebase 时，分支的
`six_factor -> standard` fail-closed 规则优先于主线的临时 legacy 阻断，不得退回稀疏旧表。

### 5.2 基本面分支同步

- 先确保六因子 fail-closed 变更形成干净提交，再在现有
  `.worktrees/commodity-fundamentals` worktree 中 rebase 到最新 `master`。
- 主线剩余变更全部完成后，再执行一次最终 rebase；第二次只用于吸收 Carry、金融边界和
  文档提交，不改变基本面运行证据的 `PENDING` 状态。
- rebase 后运行分支全套测试和 Ruff，并确认 `docs/cta-fundamentals.md` 的运行证据仍为
  `PENDING`。
- 分支保持独立，不 fast-forward、不 merge 到 `master`。
- 真实 build 到位后的后续验收必须记录 build version、catalog version、覆盖闸门结果和
  三组对照工作簿；这不属于本次可完成范围。

### 5.3 Carry 代码追溯

`run_config` 保留 `code_version`，并新增：

- `code_dirty`：Git tracked/untracked 状态是否非空。
- `code_diff_sha256`：对稳定、无颜色的 tracked diff（含 staged 与 unstaged）以及按路径排序的
  untracked 文件内容清单计算 SHA-256。干净工作树记录空字符串。

为避免把输出文件自身算入 dirty 状态，指纹只考虑未被 Git ignore 的文件；`output/` 已被忽略。
Git 不可用时，`code_version="unknown"`、`code_dirty="unknown"`、
`code_diff_sha256="unknown"`，报告仍可生成但明确不可追溯。

新逻辑通过纯函数和临时 Git 仓库测试覆盖：干净、tracked 修改、staged 修改、untracked 文件、
Git 不可用五种情况。不会把完整 diff 写入 Excel，避免泄露配置或扩大报告体积。

### 5.4 金融期货边界

- 当 `include_financial=False` 时，无论是否显式传 `symbols`，SQL 都必须排除
  `IF/IC/IH/IM/T/TF/TL/TS`。
- 显式 symbols 中同时含商品和金融期货时，只保留商品；如果过滤后为空，沿用“无 CTA 品种”
  的明确失败路径。
- `--include-financial` 暂时保留，用于历史兼容和诊断；路线图将其标记为边界例外，后续若要
  删除需单独决定。

### 5.5 文档与路线图

- README 的 Carry 默认成本从 13 bps 修正为 4.0 bps。
- `CLAUDE.md` 不再称 Carry 为“下一个策略”，改为说明 CTA、Carry 当前状态和基本面阻塞。
- 新增 `docs/ROADMAP.md`，作为唯一当前状态页，包含：
  - 已完成：CTA 量价 guarded 路径、Data Quality、Carry 日线研究版；
  - 实验：Carry 趋势滞后、次主力口径、等资金对照，默认兼容关系；
  - 外部阻塞：EOD 停在 2026-04-29、Wind catalog/build 尚未发布；
  - 待合并：`feature/commodity-fundamentals`，明确不能提前宣称完成；
  - 非目标/仓库边界：Wind/DDL/writer 属于 `market-monitor`，股指策略属于股票生态。
- README 只链接路线图，不复制易过期的详细状态。

## 6. 测试与验收

主线验收：

1. 默认 CLI 解析得到 `factor_set=price_volume`。
2. `public-pg + six_factor` 在查询前失败，并给出可行动错误。
3. 文件源六因子完全缺失基本面时失败，完整 fixture 仍通过。
4. 显式 `symbols=["IF", "CU"]` 且未开启金融期货时，SQL 同时包含 symbols 条件和金融排除。
5. Carry 指纹覆盖五种 Git 状态，并写入 `run_config`。
6. 主线完整 pytest 与 Ruff 通过。

分支验收：

1. rebase 后无冲突残留、工作树干净。
2. 分支完整 pytest 与 Ruff 通过。
3. 默认六因子仍路由到 `standard`，覆盖闸门与 raw basis 测试保持通过。
4. 运行证据表仍为 `PENDING`，没有合成或推测值。

## 7. 提交与回滚边界

提交按下列顺序独立形成：

1. `fix: fail closed on incomplete CTA fundamentals`
2. `fix: fingerprint dirty Carry research runs`
3. `fix: enforce commodity futures boundary`
4. `docs: align project status with delivered scope`

基本面分支只执行 rebase，不产生“验收完成”提交。任一阶段失败时停止在最近的绿色提交；
不使用 `git reset --hard`，不删除现有 worktree，不覆盖用户产物。
