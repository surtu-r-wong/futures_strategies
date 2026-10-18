"""D-1 rehearsal: drive the exact D-day code path on 2026-10-16 real data.

    .venv/bin/python scripts/m02_d1_rehearsal.py

Everything the D-day run does, against a throwaway state and a shifted
one-interval calendar whose trade_date is 2026-10-16:

  1. rehearsal config/calendar (single interval 2026-10-16, same pinned batch,
     same fee table; calendar_id/sha rewritten, everything else identical);
  2. synthetic CZCE receipt from public.futures_daily 2026-10-16 opens
     (night-session open, NOT a 09:30 price — labeled synthetic, rehearsal
     only; the official D-day path uses the WindPy capture receipt);
  3. python -m forward_account init on a temp state;
  4. m02_futures_advance.py --czce-receipt ... via subprocess (same CLI as
     D-day), exporting to a temp feed.

Validates: the futures_minute 09:30 SQL for all 48 non-CZCE contracts, the
receipt path, the futures_daily settlement query, and the engine end to end
on the real 67-contract fee table (fees, margin, unit NAV row). Production
state in output/multi_asset/m02_futures/ is never touched.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from forward_account.contracts import digest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "docs/requests/2026-10-17-m02-initialization/build"
REHEARSAL_DATE = "2026-10-16"
SIGNAL_DATE = "2026-10-15"

sys.path.insert(0, str(REPO / "scripts"))
from m02_czce_wind_capture import CZCE_CONTRACTS, build_receipt  # noqa: E402


def _conn():
    return get_connection(pg_config_from(load_config(resolve_settings_path())))


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def build_rehearsal_inputs(workdir):
    config = json.loads((BUILD / "account.config.json").read_text())
    # The engine only accepts backdated intervals for synthetic accounts
    # (prospective-accounting rule), so the rehearsal copy flips the whole
    # synthetic identity chain: config + pinned declaration + every quote.
    config["synthetic"] = True
    config["initial_at"] = f"{SIGNAL_DATE}T15:00:00+08:00"
    config["target_policy"]["pinned"]["synthetic"] = True
    config["label"] = "REHEARSAL " + config["label"]
    calendar = {
        "calendar_id": "m02-futures-cn-rehearsal-2026-10-16",
        "timezone": "Asia/Shanghai",
        "source": "REHEARSAL single interval shifted to 2026-10-16 real data",
        "complete": True,
        "coverage_start": REHEARSAL_DATE,
        "coverage_end": REHEARSAL_DATE,
        "intervals": [
            {
                "trade_date": REHEARSAL_DATE,
                "signal_date": SIGNAL_DATE,
                "start_at": f"{SIGNAL_DATE}T15:00:00+08:00",
                "execution_at": f"{REHEARSAL_DATE}T09:30:00+08:00",
                "end_at": f"{REHEARSAL_DATE}T15:00:00+08:00",
            }
        ],
    }
    config["calendar_id"] = calendar["calendar_id"]
    config["calendar_sha256"] = digest(calendar)
    _write(workdir / "rehearsal.config.json", config)
    _write(workdir / "rehearsal.calendar.json", calendar)
    return config


def build_synthetic_czce_receipt(workdir):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, open FROM public.futures_daily "
                "WHERE trade_date = %s AND symbol = ANY(%s)",
                (REHEARSAL_DATE, CZCE_CONTRACTS),
            )
            rows = {r[0]: float(r[1]) for r in cur.fetchall()}
    missing = [c for c in CZCE_CONTRACTS if rows.get(c) is None or rows[c] <= 0]
    if missing:
        raise ValueError(f"futures_daily CZCE open missing: {missing}")
    payload = {
        "status": "ok",
        "tool": "REHEARSAL synthetic (futures_daily night-session open)",
        "trade_date": REHEARSAL_DATE,
        "window": ["synthetic"],
        "captured_at": f"{REHEARSAL_DATE}T21:00:00",
        "codes": {
            c: {
                "errorcode": 0,
                "fields": ["open"],
                "bars": [
                    {"time": f"{REHEARSAL_DATE} 09:30:00", "open": rows[c]}
                ],
            }
            for c in CZCE_CONTRACTS
        },
    }
    receipt = build_receipt(
        payload, REHEARSAL_DATE, "rehearsal-czce-synthetic.json", synthetic=True
    )
    receipt["source"] = (
        "REHEARSAL SYNTHETIC: public.futures_daily 2026-10-16 open (night "
        "session open, NOT the 09:30 minute-bar open); pipeline rehearsal only"
    )
    receipt_path = workdir / f"czce-{REHEARSAL_DATE}.json"
    _write(receipt_path, receipt)
    return receipt_path


def main():
    sys.path.insert(0, str(REPO))
    import m02_futures_advance as daily  # noqa: E402  same code path as D-day
    from forward_account import advance, export_feed, initialize  # noqa: E402
    from forward_account.store import read_status  # noqa: E402
    from forward_account.adapters import read_targets  # noqa: E402

    with tempfile.TemporaryDirectory(prefix="m02-rehearsal-") as tmp:
        workdir = Path(tmp)
        config = build_rehearsal_inputs(workdir)
        receipt_path = build_synthetic_czce_receipt(workdir)
        state = workdir / "account.sqlite"
        initialize(state, config, json.loads(
            (workdir / "rehearsal.calendar.json").read_text()
        ))

        interval = read_status(state, config["account_id"])["next_interval"]
        target = read_targets(
            config,
            daily.FROZEN_TARGETS_CSV,
            daily.FROZEN_METADATA,
            dict(config["target_policy"]["pinned"]),
        )
        opens = daily._minute_opens(config["instruments"], REHEARSAL_DATE)
        czce, receipt = daily._czce_opens(receipt_path, config, REHEARSAL_DATE)
        opens.update(czce)
        packet = {
            "trade_date": REHEARSAL_DATE,
            "target": target,
            "execution": {
                "kind": "execution",
                "at": interval["execution_at"],
                "available_at": f"{REHEARSAL_DATE}T21:00:00+08:00",
                "source": "REHEARSAL: futures_minute 09:30 open (48) + "
                          "synthetic futures_daily CZCE open (19)",
                "synthetic": True,
                "prices": dict(sorted(opens.items())),
                "tradable": dict.fromkeys(opens, True),
            },
            "settlement": {
                "kind": "settlement",
                "at": interval["end_at"],
                "available_at": f"{REHEARSAL_DATE}T21:30:00+08:00",
                "source": "REHEARSAL: public.futures_daily settle",
                "synthetic": True,
                "prices": daily._settlement(
                    config, REHEARSAL_DATE, set(config["instruments"])
                ),
            },
        }
        result = advance(state, config["account_id"], packet)
        feed = workdir / "latest.json"
        export_feed(state, feed, synthetic=True)  # rehearsal store is synthetic
        row = result["row"]
        nonzero = {k: v for k, v in result["positions"].items() if v}
        print(
            f"REHEARSAL {REHEARSAL_DATE}: unit_nav={row['unit_nav']:.6f} "
            f"daily_return={row['daily_return']:+.6%} fees={result['fees']:.2f} "
            f"margin_ratio={row['margin_ratio']:.4f} "
            f"positions={len(nonzero)} trades={len(result['audit']['trades'])}"
        )
        print("feed head:", feed.read_text()[:300])
        print(f"REHEARSAL OK (synthetic rehearsal copy; production state untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
