"""Unit tests for the library the package root exports.

The root's names are the Python half of the README's contract, so the failure
these guard is a caller's import breaking without the release notes saying so:
a name dropped from the root, or listed in `__all__` and never bound there.
"""

import unittest

import github_cron_trigger
from github_cron_trigger import (
    LOOKBACK,
    CronSchedule,
    GitHubError,
    MisconfiguredWorkflow,
    MissedLine,
    ScheduleEntry,
    UnsupportedCron,
    format_slot,
    latest_slot,
    missed_slots,
    problems,
    records,
    schedule_entries,
    scheduled_workflow,
    triggers,
    uses_slot_action,
    utc_schedule,
)

IMPORTED = {
    "LOOKBACK": LOOKBACK,
    "CronSchedule": CronSchedule,
    "GitHubError": GitHubError,
    "MisconfiguredWorkflow": MisconfiguredWorkflow,
    "MissedLine": MissedLine,
    "ScheduleEntry": ScheduleEntry,
    "UnsupportedCron": UnsupportedCron,
    "format_slot": format_slot,
    "latest_slot": latest_slot,
    "missed_slots": missed_slots,
    "problems": problems,
    "records": records,
    "schedule_entries": schedule_entries,
    "scheduled_workflow": scheduled_workflow,
    "triggers": triggers,
    "uses_slot_action": uses_slot_action,
    "utc_schedule": utc_schedule,
}


class PackageRootTest(unittest.TestCase):
    def test_the_listed_names_are_the_imported_ones(self) -> None:
        # The imports above fail on a name the root does not bind, and this on
        # one `__all__` lists without binding, which `import *` would raise on,
        # or binds without listing, which the README's contract leaves out.
        self.assertEqual(sorted(github_cron_trigger.__all__), sorted(IMPORTED))


if __name__ == "__main__":
    unittest.main()
