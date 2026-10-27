#!/bin/bash
# M02 futures nightly — Debian deployment + acceptance drill.
# Run FROM THE DEV MACHINE (repo root cwd not required). The Debian host is
# the production Market Monitor DB box; per the 2026-10-27 dispatch letter
# the crontab install itself stays with the user, but repo/venv/state setup
# and the 2026-10-26 recompute drill are this script's job. Every step is
# idempotent; nothing here touches the shared DB (read-only queries only).
#
#   deploy      push the repo to the Debian bare mirror, clone a pinned
#               release, build its venv, install the DB settings (600),
#               flip the `current` symlink
#   drill       acceptance standard: Debian recomputes the accepted
#               2026-10-26 interval from a rolled-back copy of the live
#               store and must reproduce the accepted values bit-exactly
#               (only available_at may differ -- it is a timestamp, not an
#               accounting value)
#   sync-store  copy the live store + feed into the Debian state dir
#               (run again after any final dev-side advance before the
#               first unattended night)
#   status      read-only read_status of the Debian store + last verdict
#
# Usage: scripts/deploy/m02_debian_deploy.sh {deploy|drill|sync-store|status}
set -euo pipefail

HOST=elfbob@100.65.111.79
BASE=/home/elfbob/jobs/m02-futures
BARE=/home/elfbob/repos/futures_strategies.git
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SHA=$(git -C "$REPO" rev-parse HEAD)
STATE_SRC=$REPO/output/multi_asset/m02_futures/account.sqlite
FEED_SRC=$REPO/output/multi_asset/returns

# the accepted 2026-10-26 interval (multi_asset_backtest_db acceptance of
# 2026-10-26; PnL 1,234,475.00 is nav - prev_nav, derived not stored)
EXPECTED_UNIT_NAV=1.01280538152025
EXPECTED_DAILY_RETURN=0.012339066237160745
EXPECTED_NAV=101280538.152025
EXPECTED_MARGIN=26722265.3

run_remote() { ssh -o BatchMode=yes "$HOST" "$@"; }

case ${1:?subcommand required: deploy|drill|sync-store|status} in

deploy)
  run_remote "[ -d $BARE ] || git init --bare $BARE" >&2
  git -C "$REPO" push -q ssh://$HOST$BARE master
  REL=$BASE/releases/$(date +%Y%m%dT%H%M)-${SHA:0:8}
  run_remote "git clone -q $BARE $REL && git -C $REL checkout -q $SHA"
  run_remote "cd $REL && /usr/bin/python3.13 -m venv .venv \
    && .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt"
  scp -q "$REPO/config/settings.yaml" "$HOST:$REL/config/settings.yaml"
  run_remote "chmod 600 $REL/config/settings.yaml \
    && mkdir -p $BASE/state/logs $BASE/state/verdicts $BASE/state/feed \
    && ln -sfn $REL $BASE/current \
    && $REL/.venv/bin/python -c 'import psycopg2, pandas, yaml; print(\"venv deps ok\")'"
  echo "deployed release -> $REL (current)"
  ;;

drill)
  run_remote "mkdir -p $BASE/drill"
  rsync -q "$STATE_SRC" "$HOST:$BASE/drill/account.drill.sqlite"
  # roll the drill copy back to the pre-2026-10-26 state (head 10-23).
  # seq>4 (not just seq=5) so the drill still works after the dev chain
  # advanced later intervals into the live store.
  run_remote "/usr/bin/python3.13 - <<'PY'
import sqlite3
con = sqlite3.connect(\"$BASE/drill/account.drill.sqlite\")
con.execute(\"UPDATE accounts SET head=4\")
con.execute(\"DELETE FROM ledger WHERE seq > 4\")
con.commit()
print(\"drill store rolled back to 2026-10-23 head\")
PY"
  # RETRY_AT=0000: a failing drill must exit immediately, not sleep to 21:35
  run_remote "M02_FUTURES_STATE_DIR=$BASE/drill M02_FUTURES_RETRY_AT=0000 M02_FUTURES_EXTRA_ARGS='--today 2026-10-26' \
    bash $BASE/current/scripts/m02_futures_nightly.sh"
  # bit-exact comparison against the accepted values
  run_remote "cd $BASE/current && .venv/bin/python - <<'PY'
import json, sys
v = json.load(open(\"$BASE/drill/verdicts/2026-10-26.json\"))
row = v[\"rows\"][0]
expected = {
    \"unit_nav\": $EXPECTED_UNIT_NAV,
    \"daily_return\": $EXPECTED_DAILY_RETURN,
    \"nav\": $EXPECTED_NAV,
    \"margin\": $EXPECTED_MARGIN,
}
bad = {k: (row[k], v) for k, v in expected.items() if row[k] != v}
print(json.dumps(row, indent=1))
if v[\"status\"] != \"PASS\" or bad:
    print(\"DRILL FAIL:\", v[\"status\"], bad)
    sys.exit(1)
pnl = round(row[\"nav\"] - (row[\"nav\"] / (1 + row[\"daily_return\"])), 2)
print(f\"DRILL PASS: bit-identical; derived PnL {pnl:,.2f}\" + \" ~= 1,234,475.00\" if abs(pnl - 1234475.00) < 0.01 else \"\")
PY"
  ;;

sync-store)
  rsync -q "$STATE_SRC" "$HOST:$BASE/state/account.sqlite"
  rsync -qa "$FEED_SRC/" "$HOST:$BASE/state/feed/"
  ;;

status)
  run_remote "cd $BASE/current && .venv/bin/python - <<'PY'
import json
from forward_account.store import read_status
s = read_status(\"$BASE/state/account.sqlite\", \"m02-futures-observation\")
print(json.dumps({
    \"next_interval\": s[\"next_interval\"],
    \"nav\": s[\"state\"][\"nav\"],
    \"positions\": len(s[\"state\"][\"positions\"]),
    \"engine\": s[\"engine_sha256\"][:16],
}, ensure_ascii=False, indent=1))
try:
    print(open(\"$BASE/state/verdicts/latest.json\").read())
except FileNotFoundError:
    print(\"(no verdict yet)\")
PY"
  ;;

*) echo "unknown subcommand: $1" >&2; exit 64 ;;
esac
