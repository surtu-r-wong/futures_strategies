#!/bin/bash
# M02 v2 (daily-follow) — Debian deployment, genesis and manual runs.
# Run FROM THE DEV MACHINE. Separate root from v1: v1's store is bound to the
# engine fingerprint of ~/jobs/m02-futures/current, so v2 must never flip
# that symlink. Crontab installation stays with the user (letter R7/R9);
# nothing here touches the shared DB beyond read-only queries.
#
#   deploy                      push master to the Debian bare mirror, clone a
#                               pinned release, build its venv, install DB
#                               settings (600), flip $BASE/current, refresh
#                               $BASE/config/targets-calendar.json
#   genesis <variant>           initialize state-<variant>/account.sqlite from
#                               the release's build/<variant> config and walk
#                               the gate chain (scripts/m02_v2_genesis.py)
#   morning [YYYY-MM-DD]        run the morning target job once (manual)
#   nightly <variant> [args..]  run the nightly once (manual; extra driver args)
#   status <variant>            read-only store status + latest verdict
#
# variant: production | shadow | rehearsal
set -euo pipefail

HOST=elfbob@100.65.111.79
BASE=/home/elfbob/jobs/m02-futures-v2
BARE=/home/elfbob/repos/futures_strategies.git
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SHA=$(git -C "$REPO" rev-parse HEAD)
BUILD=docs/requests/2026-10-28-m02-futures-daily-follow/build

run_remote() { ssh -o BatchMode=yes "$HOST" "$@"; }

account_of() {
  case $1 in
    production) echo m02-futures-observation-v2 ;;
    shadow|rehearsal) echo "m02-futures-observation-v2-$1" ;;
    *) echo "unknown variant: $1" >&2; exit 64 ;;
  esac
}

first_of() {
  case $1 in
    production) echo 2026-11-16 ;;
    shadow) echo 2026-11-09 ;;
    rehearsal) echo 2026-10-30 ;;
  esac
}

case ${1:?subcommand required: deploy|genesis|morning|nightly|status} in

deploy)
  if [ -n "$(git -C "$REPO" status --porcelain -- forward_account forward_inputs scripts config citic_index cta_carry common)" ]; then
    echo "uncommitted changes in deployed paths; commit first" >&2; exit 65
  fi
  git -C "$REPO" push -q ssh://$HOST$BARE master
  REL=$BASE/releases/$(date +%Y%m%dT%H%M)-${SHA:0:8}
  run_remote "mkdir -p $BASE/releases && git clone -q $BARE $REL && git -C $REL checkout -q $SHA"
  # Debian has no reliable PyPI route. When requirements.txt is byte-identical
  # to the v1 release's, copy v1's venv (read-only source; v1 is not touched).
  V1=/home/elfbob/jobs/m02-futures/current
  run_remote "cd $REL && if cmp -s requirements.txt $V1/requirements.txt; then \
      rm -rf .venv && cp -a $V1/.venv .venv && echo 'venv copied from v1 release'; \
    else /usr/bin/python3.13 -m venv .venv \
      && .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt; fi"
  scp -q "$REPO/config/settings.yaml" "$HOST:$REL/config/settings.yaml"
  run_remote "chmod 600 $REL/config/settings.yaml \
    && mkdir -p $BASE/targets $BASE/logs $BASE/config \
    && cp $REL/$BUILD/shadow/calendar.json $BASE/config/targets-calendar.json \
    && ln -sfn $REL $BASE/current \
    && $REL/.venv/bin/python -c 'import psycopg2, pandas, yaml; print(\"venv deps ok\")'"
  echo "deployed release -> $REL (v2 current; v1 untouched)"
  ;;

genesis)
  V=${2:?variant required}
  ACC=$(account_of "$V")
  run_remote "mkdir -p $BASE/state-$V && cd $BASE/current && .venv/bin/python -B scripts/m02_v2_genesis.py \
    --state $BASE/state-$V/account.sqlite \
    --config $BUILD/$V/account.config.json --calendar $BUILD/$V/calendar.json \
    --expect-first $(first_of "$V")"
  echo "genesis $V ($ACC) done"
  ;;

morning)
  DAY=${2:-}
  run_remote "M02_V2_ROOT=$BASE M02_V2_CALENDAR=$BASE/config/targets-calendar.json \
    M02_V2_RETRY_AT=0000 ${DAY:+M02_V2_TODAY=$DAY} \
    bash $BASE/current/scripts/m02_v2_morning.sh; rc=\$?; \
    tail -3 $BASE/logs/morning-\$(date +%F).log; exit \$rc"
  ;;

nightly)
  V=${2:?variant required}; shift 2
  ACC=$(account_of "$V")
  FEED=""
  [ "$V" = production ] && FEED="M02_V2_FEED=$BASE/state-production/feed/latest.json"
  run_remote "M02_V2_STATE_DIR=$BASE/state-$V M02_V2_ACCOUNT=$ACC M02_V2_TARGETS=$BASE/targets \
    $FEED M02_V2_RETRY_AT=0000 M02_V2_EXTRA_ARGS='$*' \
    bash $BASE/current/scripts/m02_v2_nightly.sh; rc=\$?; \
    cat $BASE/state-$V/verdicts/latest.json | head -c 4000; exit \$rc"
  ;;

status)
  V=${2:?variant required}
  ACC=$(account_of "$V")
  run_remote "cd $BASE/current && .venv/bin/python - <<'PY'
import json
from forward_account.store import read_status
s = read_status(\"$BASE/state-$V/account.sqlite\", \"$ACC\")
print(json.dumps({
    \"next_interval\": s[\"next_interval\"],
    \"nav\": s[\"state\"][\"nav\"],
    \"positions\": len(s[\"state\"][\"positions\"]),
    \"engine\": s[\"engine_sha256\"][:16],
    \"last_failure\": s[\"last_failure\"],
}, ensure_ascii=False, indent=1))
PY
ls -1 $BASE/targets | tail -5; cat $BASE/state-$V/verdicts/latest.json 2>/dev/null | head -c 1500 || echo '(no verdict yet)'"
  ;;

*) echo "unknown subcommand: $1" >&2; exit 64 ;;
esac
