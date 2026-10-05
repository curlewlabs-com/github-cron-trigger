"""Unit tests for reading a workflow file as the document GitHub reads.

Every decision downstream - which line a delivery names, whether a workflow is
enrolled, which parent a chain follows - is made on the parsed document, so a
file read differently from GitHub, or half-read, would turn into a wrong slot
rather than an error. The risks are a file that is not one workflow passing as
one (a second document, a repeated key whose last value would silently win, an
empty file read as "declares nothing"), and YAML features GitHub honors being
read some other way: anchors, merge keys, and the `on` key, which YAML 1.1
parsers read as the boolean true. Every case runs the real yq.
"""

import unittest

from github_cron_trigger import yq
from github_cron_trigger.workflow_files import (
    UnreadableWorkflow,
    check_workflow_path,
    parse,
    parse_all,
)
from github_cron_trigger.workflow_triggers import (
    ScheduleEntry,
    schedule_entries,
    triggers,
)


def entries(text: str) -> list[ScheduleEntry]:
    return schedule_entries(
        "x.yml", triggers(parse(text, yq.executable())).get("schedule")
    )


class ParseTest(unittest.TestCase):
    def test_the_on_key_is_read_as_on(self) -> None:
        document = parse(
            'on:\n  schedule:\n    - cron: "0 1 * * *"\njobs: {}\n', yq.executable()
        )
        self.assertIn("on", document)
        self.assertEqual(
            entries('on:\n  schedule:\n    - cron: "0 1 * * *"\n'),
            [ScheduleEntry("0 1 * * *", None)],
        )

    def test_anchors_and_merge_keys_are_resolved_as_github_does(self) -> None:
        # The entry's own `timezone` wins over the merged one: a reader that
        # let the merge win would take a zoned entry for a UTC one.
        text = (
            "x-base: &base\n"
            '  cron: "30 19 * * *"\n'
            "  timezone: UTC\n"
            "on:\n"
            "  schedule:\n"
            "    - *base\n"
            "    - <<: *base\n"
            '      timezone: "America/New_York"\n'
        )
        self.assertEqual(
            entries(text),
            [
                ScheduleEntry("30 19 * * *", "UTC"),
                ScheduleEntry("30 19 * * *", "America/New_York"),
            ],
        )

    def test_what_is_not_one_workflow_document_is_refused(self) -> None:
        for label, text in {
            "a second document": 'on: push\n---\non: {schedule: [{cron: "0 1 * * *"}]}\n',
            "a repeated key": 'on: push\non:\n  schedule:\n    - cron: "0 1 * * *"\n',
            "a repeated nested key": "on:\n  push: {}\n  push: {}\n",
            "an empty file": "",
            "only a comment": "# nothing here\n",
            "a top-level list": "- on: push\n",
            "a scalar": "on\n",
            "broken YAML": "on: [push\n",
            "an undefined alias": "on: *missing\n",
        }.items():
            with self.subTest(label), self.assertRaises(UnreadableWorkflow):
                parse(text, yq.executable())

    def test_one_unreadable_file_does_not_stop_the_rest(self) -> None:
        documents, unreadable = parse_all(
            {"good.yml": "on: push\n", "bad.yml": "on: [push\n"}, yq.executable()
        )
        self.assertEqual(list(documents), ["good.yml"])
        self.assertEqual(list(unreadable), ["bad.yml"])


class WorkflowPathTest(unittest.TestCase):
    def test_only_a_file_directly_in_the_workflow_directory_is_a_workflow(self) -> None:
        # The path becomes part of an API path; anything else could read a file
        # that is not a workflow, or one outside the directory.
        self.assertEqual(
            check_workflow_path(".github/workflows/x.yaml"), ".github/workflows/x.yaml"
        )
        for path in (
            "",
            "x.yml",
            ".github/workflows/x.txt",
            ".github/workflows/../x.yml",
            ".github/workflows/sub/x.yml",
            ".github/workflows/.hidden.yml",
            "/.github/workflows/x.yml",
            ".github/workflows/x.yml?ref=main",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_workflow_path(path)


if __name__ == "__main__":
    unittest.main()
