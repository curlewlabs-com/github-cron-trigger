#!/bin/sh
# The check action's one step: run the package from this action's checkout on
# the workflow files named. Inputs arrive in the environment from action.yml,
# so the shell quotes them.
set -eu
PYTHONPATH="$GITHUB_ACTION_PATH/../src" exec python3 -m github_cron_trigger check \
	--workflows "$WORKFLOWS"
