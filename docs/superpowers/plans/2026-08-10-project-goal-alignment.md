# Project Goal Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the safe CTA price/volume path the honest mainline default, keep incomplete six-factor work fail-closed and isolated, fingerprint dirty Carry research runs, enforce the commodity boundary, and publish one authoritative project-status page.

**Architecture:** Implement four independently reversible mainline commits on an isolated `goal-alignment` worktree. Rebase the existing fundamentals branch once after the CTA safety checkpoint and once after the verified mainline fast-forward, but do not merge it or invent production evidence. Keep CLI validation at the orchestration boundary, Git provenance in a small pure module, and SQL filtering in the existing PostgreSQL adapter.

**Tech Stack:** Python 3.13, pandas, NumPy, argparse, subprocess/Git, hashlib, pytest, Ruff, Git worktrees

---

**Design reference:** `docs/superpowers/specs/2026-08-10-project-goal-alignment-design.md`

**Repository root:** `/home/elfbob/claude-code/futures_strategies`

**Shared interpreter:** `/home/elfbob/claude-code/futures_strategies/.venv/bin/python`

## File map

| File | Responsibility |
|---|---|
| `cta_gtja/__main__.py` | Parser construction and fail-closed source validation |
| `tests/test_cta_strategy.py` | CTA CLI safety and unchanged strategy behavior |
| `cta_carry/provenance.py` | Stable Git version/dirty/diff fingerprint capture |
| `cta_carry/__main__.py` | Add provenance fields to `run_config` |
| `tests/test_carry_provenance.py` | Real temporary-repository provenance tests |
| `tests/test_carry_report_cli.py` | CLI-to-workbook provenance wiring |
| `cta_gtja/pg_source.py` | Financial-futures exclusion in every non-opt-in query |
| `tests/test_cta_pg_source.py` | SQL and parameter regression for explicit symbols |
| `README.md` | Correct user-facing defaults and link current status |
| `CLAUDE.md` | Current module/ownership status instead of stale “next strategy” text |
| `docs/operations/carry-daily-research.md` | Explain clean/dirty provenance fields |
| `docs/ROADMAP.md` | Single authoritative current-status and boundary page |

### Task 0: Create the isolated implementation worktree and prove the baseline

**Files:**
- Verify: `.gitignore`
- Create worktree: `.worktrees/goal-alignment`

- [ ] **Step 1: Confirm the main checkout and fundamentals worktree are clean**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short --branch
git -C .worktrees/commodity-fundamentals status --short --branch
```

Expected: the main checkout shows only the committed spec/plan history and no path entries; the fundamentals worktree shows only its branch header and no path entries. Stop if either worktree has user changes.

- [ ] **Step 2: Verify the project-local worktree directory is ignored**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies
git check-ignore -q .worktrees
```

Expected: exit 0.

- [ ] **Step 3: Create the implementation branch and linked worktree**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies
git worktree add .worktrees/goal-alignment -b goal-alignment master
```

Expected: Git reports a new worktree on branch `goal-alignment` at the current `master` commit.

- [ ] **Step 4: Run the clean baseline**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/goal-alignment
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check .
```

Expected: 288 tests pass with the two known fastparquet deprecation warnings; Ruff prints `All checks passed!`.

### Task 1: Make incomplete CTA fundamentals fail closed on the mainline

**Files:**
- Modify: `cta_gtja/__main__.py`
- Modify: `tests/test_cta_strategy.py`

- [ ] **Step 1: Add failing CLI default and fail-closed tests**

Add `import cta_gtja.__main__ as cta_main` near the test imports, then add:

```python
def test_cli_defaults_to_price_volume_factor_set():
    args = cta_main.build_parser().parse_args([])

    assert args.factor_set == "price_volume"


def test_public_pg_six_factor_fails_before_loading(monkeypatch):
    def unexpected_load(**kwargs):
        pytest.fail(f"database load must not run: {kwargs}")

    monkeypatch.setattr(cta_main, "load_public_cta_data", unexpected_load)

    with pytest.raises(
        SystemExit,
        match="published conservative fundamentals build",
    ):
        cta_main.main(
            ["--source", "public-pg", "--factor-set", "six_factor"]
        )


def test_file_six_factor_requires_finite_fundamentals():
    incomplete = _single_symbol_data(np.linspace(100.0, 120.0, 80))

    with pytest.raises(SystemExit, match="basis.*inventory.*profit"):
        cta_main._validate_six_factor_request(
            source="files",
            factor_set="six_factor",
            data=incomplete,
        )

    complete = _sample_cta_data()
    cta_main._validate_six_factor_request(
        source="files",
        factor_set="six_factor",
        data=complete,
    )
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_cta_strategy.py::test_cli_defaults_to_price_volume_factor_set \
  tests/test_cta_strategy.py::test_public_pg_six_factor_fails_before_loading \
  tests/test_cta_strategy.py::test_file_six_factor_requires_finite_fundamentals -q
```

Expected: FAIL because `build_parser` / `_validate_six_factor_request` do not exist and `main` does not accept an argv list.

- [ ] **Step 3: Extract the parser and set the safe default**

In `cta_gtja/__main__.py`, move the existing `ArgumentParser` construction into this function without changing any existing argument except the factor-set default/help:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cta_gtja")
    parser.add_argument(
        "--source",
        choices=["public-pg", "files"],
        default="public-pg",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory containing prices.csv and optional fundamentals.csv",
    )
    parser.add_argument(
        "--strategy",
        choices=["medium_equal_weight", "high_composite", "both"],
        default="both",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--symbols",
        default=None,
        help=(
            "Comma-separated commodity symbols; default = all symbols "
            "in prices"
        ),
    )
    parser.add_argument(
        "--rule-type",
        default="standard",
        help="continuous_contract_ohlc.rule_type when --source public-pg",
    )
    parser.add_argument(
        "--include-financial",
        action="store_true",
        help="include stock-index and treasury futures in public-pg mode",
    )
    parser.add_argument("--cost-bps", type=float, default=1.0)
    parser.add_argument(
        "--adjustment-policy",
        choices=["recommended"],
        default="recommended",
        help="price-lineage policy for public-pg source",
    )
    parser.add_argument(
        "--allow-raw-fallback",
        action="store_true",
        help=(
            "allow raw prices only for symbols whose adjusted lineages "
            "are both corrupt"
        ),
    )
    parser.add_argument(
        "--factor-set",
        choices=["six_factor", "price_volume"],
        default="price_volume",
        help=(
            "CTA factor set; price_volume is the safe default until a "
            "published conservative fundamentals build is available"
        ),
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Output prefix without suffix; defaults under output/",
    )
    return parser
```

Change the entry point signature and first line to:

```python
def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
```

- [ ] **Step 4: Implement the mainline-only six-factor guard**

Add `import numpy as np` and `import pandas as pd`, then add:

```python
def _has_finite_fundamental(data: CTADataSet, column: str) -> bool:
    if column not in data.fundamentals.columns:
        return False
    values = pd.to_numeric(data.fundamentals[column], errors="coerce")
    return bool(np.isfinite(values.to_numpy(dtype=float)).any())


def _validate_six_factor_request(
    *,
    source: str,
    factor_set: str,
    data: CTADataSet | None,
) -> None:
    if factor_set != "six_factor":
        return
    if source == "public-pg":
        raise SystemExit(
            "six_factor requires a published conservative fundamentals build; "
            "use --factor-set price_volume on master"
        )
    if data is None:
        raise SystemExit("six_factor file validation requires loaded data")

    missing: list[str] = []
    if not any(
        _has_finite_fundamental(data, column)
        for column in ("basis_rate", "spot")
    ):
        missing.append("basis")
    for column in ("inventory", "profit"):
        if not _has_finite_fundamental(data, column):
            missing.append(column)
    if missing:
        raise SystemExit(
            "six_factor files require finite fundamentals: "
            + ", ".join(missing)
        )
```

Call it before any PostgreSQL query and after file loading:

```python
    requested_symbols = _parse_symbols(args.symbols)
    if args.source == "files":
        if not args.data_dir:
            raise SystemExit("--data-dir is required when --source files")
        data = CTADataSet.from_dir(args.data_dir).slice(
            symbols=requested_symbols,
            start=args.start,
            end=args.end,
        )
        _validate_six_factor_request(
            source=args.source,
            factor_set=args.factor_set,
            data=data,
        )
    else:
        _validate_six_factor_request(
            source=args.source,
            factor_set=args.factor_set,
            data=None,
        )
        data = load_public_cta_data(
            start=args.start,
            end=args.end,
            symbols=requested_symbols,
            rule_type=args.rule_type,
            include_financial=args.include_financial,
            adjustment_policy=args.adjustment_policy,
            allow_raw_fallback=args.allow_raw_fallback,
        )
```

- [ ] **Step 5: Run focused and CTA regression tests to verify GREEN**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_cta_strategy.py tests/test_cta_pg_source.py tests/test_cta_data_quality.py -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check cta_gtja tests/test_cta_strategy.py
```

Expected: all selected tests pass; Ruff passes.

- [ ] **Step 6: Commit the safety checkpoint**

Run:

```bash
git add cta_gtja/__main__.py tests/test_cta_strategy.py
git commit -m "fix: fail closed on incomplete CTA fundamentals"
```

### Task 2: Rebase the fundamentals branch onto the CTA safety checkpoint

**Files:**
- Rebase worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals`
- Conflict targets if Git stops: `cta_gtja/__main__.py`, `tests/test_cta_strategy.py`

- [ ] **Step 1: Confirm both branches are clean and record their tips**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git -C ../commodity-fundamentals status --short --branch
git -C ../commodity-fundamentals rev-parse HEAD
```

Expected: no path entries in either status.

- [ ] **Step 2: Rebase the feature branch onto `goal-alignment`**

Run:

```bash
git -C ../commodity-fundamentals rebase goal-alignment
```

If Git stops in `cta_gtja/__main__.py`, resolve to this feature-branch behavior:

```python
def _resolve_fundamentals_source(factor_set: str, requested: str) -> str:
    if requested == "auto":
        resolved = "standard" if factor_set == "six_factor" else "none"
    else:
        resolved = requested
    if factor_set == "six_factor" and resolved == "none":
        raise SystemExit("fundamentals are required for six_factor")
    return resolved
```

The feature parser must keep `--fundamentals-source`, but its `--factor-set` default must remain `price_volume`. Remove the mainline-only `public-pg` rejection call from the feature branch: explicit `six_factor` must resolve to `standard` and then fail inside the standard reader if no complete build exists.

If Git stops in `tests/test_cta_strategy.py`, keep the new `price_volume` parser-default assertion and the feature branch's existing `_resolve_fundamentals_source` / pilot-symbol tests. Remove the mainline-only assertion that all `public-pg + six_factor` requests fail before loading.

Continue after each conflict:

```bash
git -C ../commodity-fundamentals add cta_gtja/__main__.py tests/test_cta_strategy.py
GIT_EDITOR=true git -C ../commodity-fundamentals rebase --continue
```

Expected: rebase completes without creating an “acceptance complete” commit.

- [ ] **Step 3: Verify feature routing and the full feature branch**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_cta_strategy.py tests/test_cta_pg_source.py \
  tests/test_cta_fundamental_coverage.py tests/test_cta_fundamental_pit.py \
  -q -p no:cacheprovider
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check .
rg -n "状态：PENDING|尚无已发布的 conservative build" docs/cta-fundamentals.md
git status --short --branch
```

Expected: all tests and Ruff pass; the two PENDING statements are present; status has no path entries.

### Task 3: Fingerprint dirty Carry research runs

**Files:**
- Create: `cta_carry/provenance.py`
- Create: `tests/test_carry_provenance.py`
- Modify: `cta_carry/__main__.py`
- Modify: `tests/test_carry_report_cli.py`

- [ ] **Step 1: Write real-repository provenance tests**

Create `tests/test_carry_provenance.py`:

```python
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from cta_carry.provenance import GitState, capture_git_state


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_capture_git_state_reports_clean_commit(tmp_path):
    repo = _repo(tmp_path)

    state = capture_git_state(repo)

    assert state == GitState(
        version=_git(repo, "rev-parse", "HEAD"),
        dirty=False,
        diff_sha256="",
    )


@pytest.mark.parametrize("staged", [False, True])
def test_capture_git_state_hashes_tracked_changes(tmp_path, staged):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    if staged:
        _git(repo, "add", "tracked.txt")

    state = capture_git_state(repo)

    assert state.dirty is True
    assert len(state.diff_sha256) == 64


def test_capture_git_state_hashes_untracked_names_and_contents(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "new.txt"
    path.write_text("first\n", encoding="utf-8")
    first = capture_git_state(repo)
    path.write_text("second\n", encoding="utf-8")
    second = capture_git_state(repo)

    assert first.dirty is True
    assert first.diff_sha256 != second.diff_sha256


def test_capture_git_state_is_explicit_when_git_is_unavailable(
    monkeypatch,
    tmp_path,
):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing_git)

    assert capture_git_state(tmp_path) == GitState(
        version="unknown",
        dirty="unknown",
        diff_sha256="unknown",
    )
```

- [ ] **Step 2: Run the provenance tests to verify RED**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_provenance.py -q
```

Expected: collection fails with `ModuleNotFoundError: cta_carry.provenance`.

- [ ] **Step 3: Implement stable Git state capture**

Create `cta_carry/provenance.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess


_GIT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class GitState:
    version: str
    dirty: bool | str
    diff_sha256: str


def _git(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def capture_git_state(repo_root: Path) -> GitState:
    try:
        version = _git(repo_root, "rev-parse", "HEAD").decode().strip()
        status = _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if not status:
            return GitState(version=version, dirty=False, diff_sha256="")

        digest = hashlib.sha256()
        digest.update(b"status\0")
        digest.update(status)
        digest.update(b"tracked\0")
        digest.update(
            _git(
                repo_root,
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--binary",
                "HEAD",
                "--",
            )
        )
        untracked = _git(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split(b"\0")
        for encoded_path in sorted(path for path in untracked if path):
            path = repo_root / os.fsdecode(encoded_path)
            digest.update(b"untracked\0")
            digest.update(encoded_path)
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return GitState(
            version=version,
            dirty=True,
            diff_sha256=digest.hexdigest(),
        )
    except (
        OSError,
        UnicodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return GitState(
            version="unknown",
            dirty="unknown",
            diff_sha256="unknown",
        )
```

- [ ] **Step 4: Run provenance tests to verify GREEN**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_provenance.py -q
```

Expected: all provenance tests pass.

- [ ] **Step 5: Add failing `run_config` wiring assertions**

In `tests/test_carry_report_cli.py`, remove the now-unused `import subprocess`
and `from types import SimpleNamespace`, add
`from cta_carry.provenance import GitState`, then replace the two
`_git_version` tests with:

```python
def test_runtime_config_includes_git_provenance(monkeypatch):
    state = GitState(
        version="abc123",
        dirty=True,
        diff_sha256="f" * 64,
    )
    monkeypatch.setattr(carry_cli, "capture_git_state", lambda root: state)

    runtime = dict(
        carry_cli._runtime_config(
            source="files",
            products=["CU"],
            data=make_carry_panel(),
        )[["key", "value"]].itertuples(index=False)
    )

    assert runtime["code_version"] == "abc123"
    assert runtime["code_dirty"] is True
    assert runtime["code_diff_sha256"] == "f" * 64
```

Extend `test_file_cli_runs_writes_outputs_and_runtime_metadata` with:

```python
    assert str(runtime["code_dirty"]).lower() in {"true", "false"}
    if str(runtime["code_dirty"]).lower() == "true":
        assert len(str(runtime["code_diff_sha256"])) == 64
```

- [ ] **Step 6: Run the wiring tests to verify RED**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_report_cli.py::test_runtime_config_includes_git_provenance \
  tests/test_carry_report_cli.py::test_file_cli_runs_writes_outputs_and_runtime_metadata -q
```

Expected: FAIL because `capture_git_state` is not yet imported by the CLI and
the new rows are absent.

- [ ] **Step 7: Wire provenance into the Carry CLI**

In `cta_carry/__main__.py`, remove the local `_git_version` function and its `subprocess` import. Add:

```python
from cta_carry.provenance import capture_git_state
```

At the start of `_runtime_config`, capture once and write all three fields:

```python
def _runtime_config(
    *,
    source: str,
    products: list[str] | None,
    data: CarryDataSet,
) -> pd.DataFrame:
    dates = data.dates
    git_state = capture_git_state(_REPO_ROOT)
    return pd.DataFrame(
        [
            {"key": "source", "value": source},
            {
                "key": "products",
                "value": ",".join(products) if products else "ALL",
            },
            {"key": "code_version", "value": git_state.version},
            {"key": "code_dirty", "value": git_state.dirty},
            {
                "key": "code_diff_sha256",
                "value": git_state.diff_sha256,
            },
            {
                "key": "data_start_date",
                "value": dates[0] if dates else None,
            },
            {
                "key": "data_end_date",
                "value": dates[-1] if dates else None,
            },
            {"key": "data_rows", "value": len(data.prices)},
        ],
        columns=["key", "value"],
    )
```

- [ ] **Step 8: Run focused, smoke, and Carry regression tests**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_provenance.py tests/test_carry_report_cli.py -q -p no:cacheprovider
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_carry_*.py -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check cta_carry tests/test_carry_provenance.py tests/test_carry_report_cli.py
```

Expected: all selected tests pass; the file CLI test creates a temporary small workbook whose `run_config` contains the three provenance keys; Ruff passes.

- [ ] **Step 9: Commit provenance support**

Run:

```bash
git add cta_carry/provenance.py cta_carry/__main__.py \
  tests/test_carry_provenance.py tests/test_carry_report_cli.py
git commit -m "fix: fingerprint dirty Carry research runs"
```

### Task 4: Enforce the commodity-futures boundary for explicit symbols

**Files:**
- Modify: `cta_gtja/pg_source.py`
- Modify: `tests/test_cta_pg_source.py`

- [ ] **Step 1: Write the failing SQL regression**

Import `_load_prices`, then add:

```python
def test_explicit_symbols_still_exclude_financial_futures(monkeypatch):
    captured = {}

    def fake_read_sql(sql, conn, *, params):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr("cta_gtja.pg_source._read_sql", fake_read_sql)

    _load_prices(
        object(),
        start=None,
        end=None,
        symbols=["IF", "CU"],
        rule_type="standard",
        include_financial=False,
        adjustment_policy="recommended",
        allow_raw_fallback=False,
    )

    assert "base_symbol = ANY(%(symbols)s)" in captured["sql"]
    assert "NOT (base_symbol = ANY(%(excluded_symbols)s))" in captured["sql"]
    assert captured["params"]["symbols"] == ["IF", "CU"]
    assert set(captured["params"]["excluded_symbols"]) == {
        "IF", "IC", "IH", "IM", "T", "TF", "TL", "TS"
    }
```

- [ ] **Step 2: Run the regression to verify RED**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py::test_explicit_symbols_still_exclude_financial_futures -q
```

Expected: FAIL because `excluded_symbols` is absent when `symbols` is provided.

- [ ] **Step 3: Make the exclusion independent of the symbols filter**

Replace the conditional block in `_load_prices` with:

```python
    if symbols:
        clauses.append("base_symbol = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    if not include_financial:
        clauses.append(
            "NOT (base_symbol = ANY(%(excluded_symbols)s))"
        )
        params["excluded_symbols"] = sorted(FINANCIAL_FUTURES)
```

- [ ] **Step 4: Run CTA tests and Ruff to verify GREEN**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest \
  tests/test_cta_pg_source.py tests/test_cta_strategy.py -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check cta_gtja tests/test_cta_pg_source.py
```

Expected: all tests pass; Ruff passes.

- [ ] **Step 5: Commit the boundary fix**

Run:

```bash
git add cta_gtja/pg_source.py tests/test_cta_pg_source.py
git commit -m "fix: enforce commodity futures boundary"
```

### Task 5: Publish the authoritative project status and correct stale docs

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/operations/carry-daily-research.md`
- Create: `docs/ROADMAP.md`

- [ ] **Step 1: Correct README and link the roadmap**

Change the Carry limitation sentence to:

```markdown
本版是日线研究近似：原研报的 15 分钟止损改为日收盘触发，下一 5 分钟 VWAP 改为下一交易日开盘，ATR 默认 20 个合约交易日，成本固定为单边 4.0 bps，不换算张数、乘数和保证金，也不模拟涨跌停与容量。
```

After the opening project paragraph, add:

```markdown
当前交付状态、外部阻塞与仓库边界以 [`docs/ROADMAP.md`](docs/ROADMAP.md) 为准。
```

- [ ] **Step 2: Replace stale `CLAUDE.md` module status**

Replace the layout paragraph with:

```markdown
- 布局：`common/`（PG 连接 / settings 加载 / 净值指标，拷贝自 stock_selector 后独立演化）+
  `cta_gtja/`（国君 CTA：量价 guarded 路径可用；完整六因子等待标准基本面 build）+
  `cta_carry/`（国信 Carry 日线研究版已交付，实验参数默认保持基线兼容）。当前状态见
  `docs/ROADMAP.md`。
```

- [ ] **Step 3: Document Carry clean/dirty provenance**

Replace the `run_config` paragraph in `docs/operations/carry-daily-research.md` with:

```markdown
`run_config` 记录全部参数、实际查询范围、实际绩效范围、`code_version`、`code_dirty`、
`code_diff_sha256`、`report_start_date`、`signal_ready_date`、`vol_ready_date` 和数据覆盖。
`code_dirty=false` 表示结果来自记录的干净提交；`code_dirty=true` 时必须同时保存并核对
`code_diff_sha256`，它只记录摘要、不把源码 diff 写入工作簿；`unknown` 表示 Git 不可用，
该产物不能作为可重建证据。复核任何结论都先看这张表。
```

- [ ] **Step 4: Create the authoritative roadmap**

Create `docs/ROADMAP.md`:

```markdown
# Futures Strategies Roadmap

更新日期：2026-08-10

本页是当前交付状态的唯一摘要；历史设计和实施细节保留在 `docs/plans/`、
`docs/specs/` 和 `docs/superpowers/`。

## 已交付

- `cta_gtja`：三因子价格/量价 guarded 路径、连续合约复权选择与 Data Quality V2。
- `cta_carry`：分合约 Carry 日线研究版、账户对账、报告与 CLI。
- 两条路径都默认排除股指和国债期货；`--include-financial` 只保留为历史诊断例外。

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
```

- [ ] **Step 5: Verify documentation facts and links**

Run:

```bash
rg -n "13 bps|下一个策略" README.md CLAUDE.md
rg -n "4\.0 bps|ROADMAP|code_dirty|code_diff_sha256" \
  README.md CLAUDE.md docs/operations/carry-daily-research.md docs/ROADMAP.md
git diff --check
```

Expected: the first command returns no matches; the second finds every new fact/key; `git diff --check` returns no output.

- [ ] **Step 6: Run the full mainline regression and commit docs**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check .
git add README.md CLAUDE.md docs/operations/carry-daily-research.md docs/ROADMAP.md
git commit -m "docs: align project status with delivered scope"
```

Expected: all tests pass with only the two known fastparquet warnings; Ruff passes; the documentation commit succeeds.

### Task 6: Verify the candidate mainline and fast-forward `master`

**Files:**
- Verify branch: `goal-alignment`
- Update branch ref: `master`

- [ ] **Step 1: Run fresh final verification in the isolated worktree**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/goal-alignment
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check .
git diff --check
git status --short --branch
```

Expected: all tests pass; Ruff passes; diff check is empty; status has no path entries.

- [ ] **Step 2: Confirm `master` can be fast-forwarded without overwriting work**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies
git status --short --branch
git merge-base --is-ancestor master goal-alignment
```

Expected: the main checkout has no path entries and the ancestor check exits 0. If not, stop and reconcile the newly arrived work instead of forcing the branch.

- [ ] **Step 3: Fast-forward `master`**

Run:

```bash
git merge --ff-only goal-alignment
```

Expected: `master` advances by the four implementation commits; no merge commit is created.

- [ ] **Step 4: Verify the updated main checkout**

Run:

```bash
git status --short --branch
git log --oneline --decorate -n 8
```

Expected: `master` is clean and contains, in order, the CTA safety, Carry provenance, financial-boundary, and status-doc commits.

### Task 7: Perform the final fundamentals rebase and leave it explicitly pending

**Files:**
- Rebase worktree: `/home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals`
- Conflict target if Git stops: `cta_gtja/pg_source.py`

- [ ] **Step 1: Rebase the feature branch onto final `master`**

Run:

```bash
cd /home/elfbob/claude-code/futures_strategies/.worktrees/commodity-fundamentals
git status --short --branch
git rebase master
```

If `cta_gtja/pg_source.py` conflicts, keep the feature branch's standard/legacy fundamentals readers and use this independent filtering block in `_load_prices`:

```python
    if symbols:
        clauses.append("base_symbol = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    if not include_financial:
        clauses.append(
            "NOT (base_symbol = ANY(%(excluded_symbols)s))"
        )
        params["excluded_symbols"] = sorted(FINANCIAL_FUTURES)
```

Then run:

```bash
git add cta_gtja/pg_source.py
GIT_EDITOR=true git rebase --continue
```

Expected: rebase completes; the branch remains separate from `master`.

- [ ] **Step 2: Run final feature-branch verification**

Run:

```bash
PYTHONPATH=. /home/elfbob/claude-code/futures_strategies/.venv/bin/python -m pytest -q -p no:cacheprovider
/home/elfbob/miniconda3/bin/ruff check .
rg -n "状态：PENDING|尚无已发布的 conservative build" docs/cta-fundamentals.md
git diff --check
git status --short --branch
```

Expected: all tests and Ruff pass; PENDING statements remain; no diff or status paths remain.

- [ ] **Step 3: Prove branch ancestry without merging**

Run:

```bash
git merge-base --is-ancestor master feature/commodity-fundamentals
git rev-list --left-right --count master...feature/commodity-fundamentals
```

Expected: the ancestor check exits 0; the count begins with `0` and has a positive feature-only count. Do not merge the branch and do not fill the evidence table.

- [ ] **Step 4: Record final repository state for handoff**

Run:

```bash
git -C /home/elfbob/claude-code/futures_strategies status --short --branch
git -C /home/elfbob/claude-code/futures_strategies/.worktrees/goal-alignment status --short --branch
git status --short --branch
```

Expected: all three worktrees have no path entries; `master` contains the delivered fixes, and `feature/commodity-fundamentals` remains pending real upstream data.
