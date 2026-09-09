# Carry 采集拆分后的逐字节不变性验证（2026-08-28）

## 为什么要做

`feature/continuous-session-backfill` 把 `_build_default_liquidity_audit` 拆成
「宇宙无关核心 `build_audit` + Carry 包装 `_carry_liquidity_pool`」，并给
`_capture_and_publish_outcome` 加了三个默认为 `None` 的参数
（`audit_builder` / `coverage_check` / `boundaries`）。

用户的约束是**不影响其他策略**。全量测试绿**不构成**证据 ——
按 [[shared-layer-extraction-lessons]]，「逐点不变」要拿真实资产比 sha256，读代码看不出来。

## 做法

在 WSL2 上以**完全相同的参数**各跑一次真实 Carry 采集，输出到各自的临时路径
（**不指向** `config/carry_minute_sessions.csv`）：

- 拆分前：`/tmp/carry-baseline`，git worktree @ `b95a74f`
- 拆分后：`/home/ghls/futures_strategies_continuous_20260827`，@ `8e23c6a`

```
python -m scripts.carry.capture_minute_sessions \
  --start 2024-01-08 --end 2024-01-31 --backtest-start 2026-01-08 \
  --output <...>-sessions.csv --inventory-output <...>-inventory.csv \
  --audit-report <...>-audit.txt
```

## 结果：三份产出逐字节相同

| 产出 | sha256 | |
|---|---|---|
| `audit.txt` | `d4d3e762a3c81a2421762b6e1d1c730b65e9df1f5c699c6faf5707885d0af42d` | 两份相同 |
| `inventory.csv` | `3a06049be3910eb18a3da304d6f9c50c57abc544f9ea5fa614be66103ca4c9a7` | 两份相同 |
| stdout log | `c78e83f827dbbcf00f16ed2db2a402c7bdbc2e010af3b51fa8c261397157d424` | 两份相同 |

两次退出码同为 1，计数同为
`products=55 rules=0 checked_days=973 ambiguous=52`。
stdout 里含审计阶段的全部数字：
`all_product_days=1098 in_pool_days=973 in_pool_ratio=0.886157 audit_universe_days=1098
audited_days=973 audited_ratio=0.886157 normalization_excluded_product_days=18`
—— 这些正是被拆分的那段代码算出来的。

## 覆盖范围（说准，不夸大）

**被这次运行走到的**：日线加载 → `build_audit`（代表合约索引、全局日历、
`build_audit_key_sets`、候选选择）→ `validate_capture_request` → 边界采集 →
`classify_authorized_boundaries` → `write_capture_diagnostics`。
**改动的每一行都在这个范围内。**

**没被走到的**：`publish_session_rules` 的原子写盘 —— 本次 `rules=0`，发布被阻止。

原因不是运行姿势不对，而是**设计使然**：52 条歧义全部是
`session_exception_unconsumed`（授权文件 `carry_minute_session_exceptions.csv`
覆盖全历史，而本次只采一个月，于是必有未被消费的例外）。
换句话说，**任何局部窗口的采集都不可能发布** —— 这正是
`validate_capture_request` 里 `repository_capture_start` 要求仓库资产必须由
2011-01-04 起的全历史整份重写的原因。

因此对未走到的那一段，改用**静态证据**：`git diff b95a74f..HEAD` 在
`scripts/carry/capture_minute_sessions.py` 上的全部 hunk 落在
546–697（审计拆分）、1603–1620（`validate_capture_request`）、
1941–1971 / 2007–2011 / 2074–2081（`_capture_and_publish_outcome` 的三个注入点）、
2222 / 2236（`capture_and_publish` 薄包装的参数转发）。
`classify_authorized_boundaries`(985)、`write_capture_diagnostics`(1879)、
`publish_session_rules` 调用点(2192) **一行未动**，且全部位于所有改动的下游。

## 另外两条

- `config/carry_minute_sessions.csv` 未被本分支任何提交触及：
  sha256 `e3aff444aba6b2f699a21051c1610da0c025461b81e9d649b1100b3690558c5e`，
  最后一次改动它的是 `3f89cff feat(carry): publish the session-rule asset`。
- `config/index_minute_sessions.csv`（股指线）本就由 `index_open_momentum/sessions.py`
  单独加载，与本次改动无交集。

## 结论

拆分对 Carry 的采集**逐字节无影响**：运行时证据覆盖了改动的每一行，
静态证据覆盖了改动下游那段未被本次窗口走到的发布代码。
