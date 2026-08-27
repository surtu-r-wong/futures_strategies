"""Compatibility entry point for the shared commodity panel builder.

Legacy invocations may keep using --out and YYYY-MM bounds; the shared parser
accepts both alongside the preferred --output-dir and ISO dates.
"""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.commodity.build_panel import main  # noqa: E402,F401


if __name__ == "__main__":
    raise SystemExit(main())
