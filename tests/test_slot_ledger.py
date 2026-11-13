"""Unit tests for which ledger records pruning removes.

The failure these guard is a line made to look as if it never succeeded:
the missed-slot check reports a line with no record as not judged, so a
pruning pass that removes a line's newest record - a yearly line's, say,
when a daily sibling's delivery prunes the workflow - would raise a false
alarm for most of the year. Every instant is hardcoded.
"""

import unittest
from datetime import datetime

from github_cron_trigger.cron_slots import UTC
from github_cron_trigger.slot_ledger import prunable

CUTOFF = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
DAILY = "30_19_x_x_x"
YEARLY = "0_0_1_1_x"


class PrunableTest(unittest.TestCase):
    def test_old_records_go_and_recent_ones_stay(self) -> None:
        names = [f"{DAILY}/20260830T1930Z", f"{DAILY}/20260902T1930Z"]
        self.assertEqual(prunable(names, CUTOFF), [f"{DAILY}/20260830T1930Z"])

    def test_a_lines_newest_record_stays_however_old(self) -> None:
        # The yearly line's only record is months old; it is what says the line
        # has ever succeeded, so it stays while the daily line's old one goes.
        names = [
            f"{YEARLY}/20260101T0000Z",
            f"{DAILY}/20260830T1930Z",
            f"{DAILY}/20260902T1930Z",
        ]
        self.assertEqual(prunable(names, CUTOFF), [f"{DAILY}/20260830T1930Z"])

    def test_every_part_of_the_newest_slot_stays(self) -> None:
        names = [
            f"{YEARLY}/20250101T0000Z.production",
            f"{YEARLY}/20260101T0000Z.production",
            f"{YEARLY}/20260101T0000Z.staging",
        ]
        self.assertEqual(
            prunable(names, CUTOFF), [f"{YEARLY}/20250101T0000Z.production"]
        )

    def test_a_name_the_ledger_did_not_write_is_left_alone(self) -> None:
        self.assertEqual(prunable([f"{DAILY}/notes", "stray"], CUTOFF), [])


if __name__ == "__main__":
    unittest.main()
