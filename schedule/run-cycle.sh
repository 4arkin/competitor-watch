#!/bin/sh
# One unattended cycle: fetch → plan → triage (headless agent from CW_TRIAGE_CMD) → apply → deliver.
# Point cron or launchd at this file. Output goes to stdout; redirect it to data/run.log.
cd "$(dirname "$0")/.." || exit 1
# Schedulers start with a minimal PATH; agent CLIs usually live in one of these.
PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH
echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) competitor-watch run"
python3 bin/cw run --deliver
rc=$?
echo "== exit $rc"
exit $rc
