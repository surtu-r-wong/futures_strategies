from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/continuous/build_panel.py"


@pytest.fixture(scope="module")
def build_panel_script():
    spec = importlib.util.spec_from_file_location("continuous_build_panel_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator(module):
    validator = getattr(module, "_validate_existing_shard", None)
    assert validator is not None, "build_panel script must validate existing shard schemas"
    return validator


def _assert_schema_mismatch(error: ValueError, path: Path) -> None:
    message = str(error)
    assert message.startswith("panel_shard_schema_mismatch:")
    assert str(path) in message
    assert "continuity_segment" in message
    assert "int64" in message


def test_validate_existing_shard_accepts_int64_segment(build_panel_script, tmp_path):
    shard = tmp_path / "panel-2023-01.parquet"
    pd.DataFrame(
        {"continuity_segment": pd.Series([0, 1], dtype="int64")}
    ).to_parquet(shard, index=False)

    _validator(build_panel_script)(shard)


def test_validate_existing_shard_rejects_missing_segment(build_panel_script, tmp_path):
    shard = tmp_path / "panel-2023-01.parquet"
    pd.DataFrame({"adj_factor": [1.0]}).to_parquet(shard, index=False)

    with pytest.raises(ValueError) as caught:
        _validator(build_panel_script)(shard)

    _assert_schema_mismatch(caught.value, shard)
    assert caught.value.__cause__ is not None


def test_validate_existing_shard_rejects_wrong_segment_dtype(
    build_panel_script, tmp_path
):
    shard = tmp_path / "panel-2023-01.parquet"
    pd.DataFrame(
        {"continuity_segment": pd.Series([0, 1], dtype="int32")}
    ).to_parquet(shard, index=False)

    with pytest.raises(ValueError) as caught:
        _validator(build_panel_script)(shard)

    _assert_schema_mismatch(caught.value, shard)
    assert "observed=int32" in str(caught.value)


def test_validate_existing_shard_rejects_corrupt_file(build_panel_script, tmp_path):
    shard = tmp_path / "panel-2023-01.parquet"
    shard.write_bytes(b"not parquet")

    with pytest.raises(ValueError) as caught:
        _validator(build_panel_script)(shard)

    _assert_schema_mismatch(caught.value, shard)
    assert caught.value.__cause__ is not None


def test_existing_shard_is_validated_before_skip(build_panel_script, tmp_path):
    shard = tmp_path / "panel-2023-01.parquet"
    pd.DataFrame(
        {"continuity_segment": pd.Series([0], dtype="int32")}
    ).to_parquet(shard, index=False)
    can_skip = getattr(build_panel_script, "_existing_shard_can_be_skipped", None)
    assert can_skip is not None, "the resume decision must include schema validation"

    with pytest.raises(ValueError) as caught:
        can_skip(shard)

    _assert_schema_mismatch(caught.value, shard)
