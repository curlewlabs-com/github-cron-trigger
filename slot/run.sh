#!/bin/sh
# The slot action's one step: resolve which slot this run serves, from the
# event that started it. Inputs and event fields arrive in the environment from
# action.yml, so the shell quotes them.
set -eu
PYTHONPATH="$GITHUB_ACTION_PATH/../src" exec python3 -m github_cron_trigger slot \
	--repo "$GITHUB_REPOSITORY" \
	--workflow-ref "$GITHUB_WORKFLOW_REF" \
	--workflow-sha "$GITHUB_WORKFLOW_SHA" \
	--event-name "$EVENT_NAME" \
	--payload-slot "$PAYLOAD_SLOT" \
	--payload-cron "$PAYLOAD_CRON" \
	--schedule-cron "$SCHEDULE_CRON" \
	--parent-path "$PARENT_PATH" \
	--parent-sha "$PARENT_SHA" \
	--parent-event "$PARENT_EVENT" \
	--parent-created-at "$PARENT_CREATED_AT" \
	--part "$PART" \
	--github-output "$GITHUB_OUTPUT" \
	--step-summary "$GITHUB_STEP_SUMMARY"
