#!/bin/sh
# The done action's one step: record this run's slot as finished. Inputs
# arrive in the environment from action.yml, so the shell quotes them.
set -eu
exec "$GITHUB_ACTION_PATH/../lib/run-package.sh" "done" \
	--repo "$GITHUB_REPOSITORY" \
	--workflow-ref "$GITHUB_WORKFLOW_REF" \
	--slot "$SLOT" \
	--cron "$CRON" \
	--part "$PART" \
	--sha "$GITHUB_SHA"
