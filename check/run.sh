#!/bin/sh
# The check action's one step: run the package from this action's checkout on
# the workflow files named. Inputs arrive in the environment from action.yml,
# so the shell quotes them.
set -eu
exec "$GITHUB_ACTION_PATH/../lib/run-package.sh" check \
	--workflows "$WORKFLOWS"
