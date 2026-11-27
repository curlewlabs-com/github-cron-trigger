"""Run each scheduled slot of a GitHub Actions workflow on time and exactly once,
with GitHub's own `schedule` trigger kept as the backstop. See the README.

The names `__all__` lists are the Python library the README's "Versioning"
counts in the contract: what a caller needs to read the schedule, the ledger
and the missed slots in-process. Import them from here; the modules they live
in may move between releases.
"""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than a syntax error from a newer form.
if sys.version_info < (3, 10):  # noqa: UP036 - the point is to run on older ones
    sys.exit("github-cron-trigger needs Python 3.10 or newer")

# Imported only once the check above has passed, for the reason it gives.
from .check import problems
from .clock import scheduled_workflow, uses_slot_action
from .cron_slots import (
    CronSchedule,
    UnsupportedCron,
    format_slot,
    latest_slot,
)
from .github import GitHubError
from .missed import LOOKBACK, MissedLine, missed_slots
from .slot_ledger import records
from .workflow_triggers import (
    MisconfiguredWorkflow,
    ScheduleEntry,
    schedule_entries,
    triggers,
    utc_schedule,
)

__all__ = [
    "LOOKBACK",
    "CronSchedule",
    "GitHubError",
    "MisconfiguredWorkflow",
    "MissedLine",
    "ScheduleEntry",
    "UnsupportedCron",
    "format_slot",
    "latest_slot",
    "missed_slots",
    "problems",
    "records",
    "schedule_entries",
    "scheduled_workflow",
    "triggers",
    "uses_slot_action",
    "utc_schedule",
]
