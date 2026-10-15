"""Read existing target CSVs without using their lots, notional or vol_scale."""

import csv
import hashlib
import io
import json
from pathlib import Path

from .contracts import number, require_keys, text, timestamp


def read_targets(config, csv_path, metadata_path, receipt):
    """Receipt is evidence of capture, not an inferred file modification time.

    Carry: original workbook/run_config (read-only). 027: original config JSON.
    A pinned parameter JSON extraction is also accepted for offline transports.
    The receipt binds both exact files, row count, source release and availability.
    """
    require_keys(
        receipt,
        (
            "source_version",
            "available_at",
            "csv_sha256",
            "metadata_sha256",
            "row_count",
            "signal_date",
            "synthetic",
        ),
        "source receipt",
    )
    csv_bytes = Path(csv_path).read_bytes()
    meta_bytes = Path(metadata_path).read_bytes()
    for data, key in ((csv_bytes, "csv_sha256"), (meta_bytes, "metadata_sha256")):
        if hashlib.sha256(data).hexdigest() != receipt[key]:
            raise ValueError("source receipt file hash mismatch")
    if (
        receipt["source_version"] != config["source_version"]
        or receipt["synthetic"] is not config["synthetic"]
    ):
        raise ValueError("source version/synthetic mismatch")
    timestamp(receipt["available_at"])
    if Path(metadata_path).suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(meta_bytes), read_only=True, data_only=True)
        try:
            rows = iter(wb["run_config"].values)
            header = next(rows)
            ki, vi = header.index("key"), header.index("value")
            params = {}
            for row in rows:
                key, value = row[ki], row[vi]
                if key in params:
                    raise ValueError("duplicate run_config key")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                params[key] = value
        finally:
            wb.close()
    else:
        params = json.loads(meta_bytes)
    expected = config["source_parameters"]
    if any(params.get(k) != v for k, v in expected.items()):
        raise ValueError("source metadata parameters mismatch")
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
    if not {"signal_date", "contract", "target_weight"}.issubset(
        reader.fieldnames or []
    ):
        raise ValueError("native target CSV fields missing")
    weights = {}
    for row in reader:
        contract = text(row["contract"], "contract")
        if contract in weights or row["signal_date"] != receipt["signal_date"]:
            raise ValueError("duplicate contract or inconsistent signal date")
        weights[contract] = number(row["target_weight"], "target_weight")
    if type(receipt["row_count"]) is not int or receipt["row_count"] != len(weights):
        raise ValueError("target row count mismatch")
    return {**receipt, "parameters": expected, "weights": weights}
