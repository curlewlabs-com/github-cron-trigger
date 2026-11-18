"""Unit tests for reading the `on:` block, and the slot-action check, off a
parsed document that may be any shape.

A workflow file is outside input: a hand-edited one can put a list where a
mapping belongs, or a string where a list does. Each reader must answer or name
the file with a MisconfiguredWorkflow, never raise something else partway, since
a stray AttributeError would end a tick for every other workflow too.
"""

import unittest

from github_cron_trigger.clock import uses_slot_action
from github_cron_trigger.workflow_triggers import (
    MisconfiguredWorkflow,
    ScheduleEntry,
    dispatch_types,
    schedule_entries,
    triggers,
)

SLOT = "curlewlabs-com/github-cron-trigger/slot@v0"


class TriggersTest(unittest.TestCase):
    def test_each_form_of_the_on_block_reads_as_a_mapping(self) -> None:
        self.assertEqual(triggers({"on": "push"}), {"push": None})
        self.assertEqual(
            triggers({"on": ["push", "schedule"]}), {"push": None, "schedule": None}
        )
        self.assertEqual(triggers({"on": {"push": {}}}), {"push": {}})

    def test_anything_else_declares_no_triggers(self) -> None:
        documents: list[object] = [None, [], "on", {}, {"on": 3}, {"on": None}]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(triggers(document), {})


class DispatchTypesTest(unittest.TestCase):
    def test_a_string_or_a_list_of_strings_is_read(self) -> None:
        self.assertEqual(dispatch_types("a.yml", {"types": "x"}), ["x"])
        self.assertEqual(dispatch_types("a.yml", {"types": ["x", "y"]}), ["x", "y"])

    def test_no_types_is_empty(self) -> None:
        values: list[object] = [None, [], {}, {"other": 1}]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(dispatch_types("a.yml", value), [])

    def test_types_that_are_not_strings_are_refused(self) -> None:
        shapes: list[object] = [["x", 1], 3, None, {"x": 1}]
        for types in shapes:
            with (
                self.subTest(types=types),
                self.assertRaisesRegex(MisconfiguredWorkflow, "a.yml"),
            ):
                dispatch_types("a.yml", {"types": types})


class ScheduleEntriesTest(unittest.TestCase):
    def test_entries_are_read_with_their_timezone(self) -> None:
        self.assertEqual(
            schedule_entries(
                "a.yml", [{"cron": "0 1 * * *"}, {"cron": "0 2 * * *", "timezone": "X"}]
            ),
            [ScheduleEntry("0 1 * * *", None), ScheduleEntry("0 2 * * *", "X")],
        )

    def test_no_schedule_is_no_entries(self) -> None:
        self.assertEqual(schedule_entries("a.yml", None), [])

    def test_a_schedule_of_another_shape_is_refused_by_file(self) -> None:
        shapes: list[object] = [
            "0 1 * * *",
            {"cron": "0 1 * * *"},
            [3],
            [{}],
            [{"cron": 1}],
        ]
        for entries in shapes:
            with (
                self.subTest(entries=entries),
                self.assertRaisesRegex(MisconfiguredWorkflow, "a.yml"),
            ):
                schedule_entries("a.yml", entries)

    def test_a_timezone_that_is_not_a_string_is_refused(self) -> None:
        with self.assertRaisesRegex(MisconfiguredWorkflow, "timezone"):
            schedule_entries("a.yml", [{"cron": "0 1 * * *", "timezone": 5}])


class UsesSlotActionTest(unittest.TestCase):
    def test_a_step_that_runs_the_action_is_found(self) -> None:
        document = {"jobs": {"j": {"steps": [{"run": "x"}, {"uses": SLOT}]}}}
        self.assertTrue(uses_slot_action(document))

    def test_a_document_of_another_shape_runs_nothing(self) -> None:
        documents: list[object] = [
            None,
            [],
            {"jobs": []},
            {"jobs": {"j": "x"}},
            {"jobs": {"j": {"steps": "x"}}},
            {"jobs": {"j": {"steps": ["x", {"uses": 3}, {"uses": "other/action@v1"}]}}},
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertFalse(uses_slot_action(document))


if __name__ == "__main__":
    unittest.main()
