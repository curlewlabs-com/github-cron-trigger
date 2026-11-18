#!/bin/sh
# The done action's one step: record this run's slot as finished. Inputs
# arrive in the environment from action.yml, so the shell quotes them.
set -eu
PYTHONPATH="$GITHUB_ACTION_PATH/../src" exec python3 -m github_cron_trigger "done" \
	--repo "$GITHUB_REPOSITORY" \
	--workflow-ref "$GITHUB_WORKFLOW_REF" \
	--slot "$SLOT" \
	--cron "$CRON" \
	--part "$PART" \
	--sha "$GITHUB_SHA"
