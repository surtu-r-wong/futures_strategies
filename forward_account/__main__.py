"""Offline prospective accounting CLI; no scheduler or production integration."""

import argparse
import json
from pathlib import Path
import sys

from .adapters import read_targets
from .contracts import require_keys
from .store import (
    advance,
    export_feed,
    initialize,
    read_config,
    read_state,
    read_status,
    record_failure,
    extend_references,
)


def read_json(path):
    return json.loads(Path(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="persistent LOCAL SQLite path")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="explicitly initialize a separate paper account"
    )
    init.add_argument("--config", required=True)
    init.add_argument("--calendar", required=True)
    step = commands.add_parser(
        "advance", help="commit exactly the next complete interval"
    )
    step.add_argument("--account", required=True)
    step.add_argument("--input", required=True)
    step.add_argument("--revise-reason")
    status = commands.add_parser("status")
    status.add_argument("--account", required=True)
    extend = commands.add_parser(
        "extend-references",
        help="append calendar sessions/contracts without resetting NAV",
    )
    extend.add_argument("--account", required=True)
    extend.add_argument("--calendar", required=True)
    extend.add_argument("--instruments", required=True)
    publish = commands.add_parser(
        "export", help="atomic export from successful committed state"
    )
    publish.add_argument("--output", required=True)
    publish.add_argument("--synthetic", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            state = initialize(
                args.state, read_json(args.config), read_json(args.calendar)
            )
        elif args.command == "status":
            state = read_status(args.state, args.account)
        elif args.command == "extend-references":
            extend_references(
                args.state,
                args.account,
                read_json(args.calendar),
                read_json(args.instruments),
            )
            state = read_state(args.state, args.account)
        elif args.command == "advance":
            config = read_config(args.state, args.account)
            path = Path(args.input)
            packet = read_json(path)
            require_keys(
                packet,
                (
                    "trade_date",
                    "target_csv",
                    "source_metadata",
                    "source_receipt",
                    "execution",
                    "settlement",
                ),
                "interval input file",
            )
            packet["target"] = read_targets(
                config,
                path.parent / packet.pop("target_csv"),
                path.parent / packet.pop("source_metadata"),
                read_json(path.parent / packet.pop("source_receipt")),
            )
            state = advance(
                args.state, args.account, packet, revise_reason=args.revise_reason
            )
        else:
            feed = export_feed(args.state, args.output, synthetic=args.synthetic)
            print(
                f"published {len(feed['series'])} series; generated_at={feed['generated_at']}"
            )
            return 0
        print(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        if args.command == "advance" and Path(args.state).is_file():
            record_failure(args.state, args.account, "CLI input/advance", exc)
        # Do not dump source files or runtime environment on failure.
        print(f"account operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
