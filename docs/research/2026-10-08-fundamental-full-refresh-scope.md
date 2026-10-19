# 基本面全量更新：原计划接续与请求范围更正

2026-10-08，用户指出此前计划更新全部数据，要求重新检查记录。本记录更正同日质量请求的范围；
旧审计数值仍有效，但「优先 PP/TA、分批修薄弱源即可」不能替代已确定的全量更新目标。

## 1. 原计划证据

- [08-26 全量重填设计](/home/elfbob/market-monitor/docs/superpowers/specs/2026-08-26-commodity-fundamentals-source-workbook-design.md)：
  状态为「用户已批准先实现原型」，目标明确是“全量重填九个品种的现货、库存和利润数据源”。
  九品种各有一条现货、一条库存，以及直接利润或完整利润公式；旧 v1 仅作参考，不能自动继承。
- 原型与字段说明已提交：`ae46c1a`、`120f377`（08-26）。
- [08-27 发布日历设计](/home/elfbob/market-monitor/docs/superpowers/specs/2026-08-27-commodity-fundamentals-release-calendar-design.md)：
  状态为「用户已确认」。新工作簿以当日、每周、每月、逐期实际发布时间取代固定 T+N；
  使用真实交易日历处理节假日，信号截点仍为 15:00。

## 2. 已完成但尚未接回当前上游的实现

发布日历实现已在 `feature/fundamentals-release-calendar` 的 `aab916e`（08-27）提交，
不是只有设计文档。工作树：`/home/elfbob/market-monitor/.worktrees/fundamentals-release-calendar`。
本次 `git branch --contains aab916e` 仅列出该分支，当前上游工作分支未包含它。
提交含发布规则模型、availability/capture/catalog/db 接线、工作簿升级、测试及新版 Excel。
工作树干净；本次没有合并或重跑该分支测试，不把历史测试代码的存在当作当前集成验收。

接续文件：

- [发布日历版 Excel](/home/elfbob/market-monitor/.worktrees/fundamentals-release-calendar/docs/data/commodity-fundamentals/commodity-fundamentals-source-replacement-v2-calendar.xlsx)
- [该分支填写说明](/home/elfbob/market-monitor/.worktrees/fundamentals-release-calendar/docs/operations/commodity-fundamentals-source-workbook.md)
- [实施计划](/home/elfbob/market-monitor/.worktrees/fundamentals-release-calendar/docs/superpowers/plans/2026-08-27-commodity-fundamentals-release-calendar.md)

已检查仓库主副本及此分支的旧/新版工作簿：各 27 条预填源行，新 Wind 代码填写数和采用数均为 0。
这只说明本次找到的仓库副本尚未填写，不能断言用户没有在其他位置保存填好的文件。
后续若有已填副本，应保留内容并用非破坏性升级，不重新生成覆盖它。

## 3. 正确范围与接续顺序

范围为 **M、RB、CU、AL、TA、PP、MA、BU、RU 九品种，现货 9 条、库存 9 条、利润路线 9 条**，
共 27 条业务路线；利润可拆成多个原料/成本输入，所以并非固定 27 个 Wind 指标。
所有采用项重新填写与核验；允许经核验重新选择旧源，但不默认沿用。
本范围是基本面数据源与发布口径，不扩大为本仓全部行情/分钟数据库的重建。

1. 接续已有发布日历实现及新版工作簿，检查与 09 月后上游变更的兼容性；不从头重写。
2. 完成九品种全套输入、利润路线、单位与发布规则的填写/核验。
3. 校验完整性，导出新目录/公式及待核验证据。旧工具使用的 `v2` 名称须处理：
   现有 catalog v2 已被仓单扩围占用，不能覆盖；新版本身份另定。
4. 按全量候选清单和真实历史窗口向办公室更新额度与回填范围；执行可拆批，交付范围仍是全量。
5. 生成新 build，在同价格、同九品种、同窗口下完成整体新旧对照。逐项消融可以辅助归因，
   不再作为“只替换两条”的范围限制。保留既有 v1 日更与旧 build，发布按章程另形成具体处置。

## 4. 对同日办公室请求的更正

原请求保留为往来记录；以同目录新增的
`response-01-futures-strategies-full-refresh-scope.md` 为范围更正。
更正覆盖原请求的「PP/TA 第一批」「A/B/C 优先级」和单类变更限定，以及质量报告中的同类表述。
原质量问题清单只作全量核验时的关注点，不作为最终范围。
办公室本轮仍只受理和核算，未授权或执行生产写入；本次没有替换数据源。
