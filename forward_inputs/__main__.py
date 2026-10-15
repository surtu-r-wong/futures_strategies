"""Stage existing input artifacts into a new isolated directory (no account writes)."""

import argparse
import sys

from .adapter import stage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        report = stage(args.plan, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError):
        # Never echo malformed source content, connection strings or env values.
        print(
            "input adapter failed: invalid plan, source or staging destination",
            file=sys.stderr,
        )
        return 2
    print(f"input adapter: {report['status']}; blockers={','.join(report['blockers'])}")
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
