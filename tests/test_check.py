"""Unit tests for checking a checkout's workflow files before they merge.

The failure these guard is a workflow that merges green and then can never be
delivered: an hourly line in a workflow that runs the slot action, which the
clock refuses on every tick and the slot action refuses on every scheduled
run. So the cases are each shape the clock or the slot action refuses, the
same workflows written the way they accept (so the check is not simply
refusing everything that runs the slot action), and the shapes that are fine
without the slot action. Workflow files are real YAML in a real directory, read
through the same parser the clock uses.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from github_cron_trigger import check, workflow_files, yq
from github_cron_trigger.cron_slots import parse_cron

SLOT_STEP = "      - uses: curlewlabs-com/github-cron-trigger/slot@v0\n"


def workflow(file_name: str, crons: list[str], enrolled: bool, step: str) -> str:
    """`file_name`, firing on `crons`, with one job whose one step is `step`."""
    lines = "".join(f'    - cron: "{cron}"\n' for cron in crons)
    enrollment = (
        f"  repository_dispatch:\n    types: [github-cron-trigger/{file_name}]\n"
        if enrolled
        else ""
    )
    return (
        f"name: {file_name}\non:\n  schedule:\n{lines}{enrollment}"
        "jobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n" + step
    )


def chained(parent_name: str) -> str:
    return (
        "name: Chained\non:\n  workflow_run:\n"
        f"    workflows: [{parent_name}]\n    types: [completed]\n"
        "jobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n" + SLOT_STEP
    )


def problems(files: dict[str, str]) -> list[str]:
    documents, unreadable = workflow_files.parse_all(files, yq.executable())
    if unreadable:
        raise AssertionError(f"fixture did not parse: {unreadable}")
    return check.problems(documents)


def run(files: dict[str, str]) -> tuple[int, str]:
    """`check` run on a directory holding `files`, its exit status and output."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = check.main(["--workflows", tmp])
    return status, output.getvalue()


HOURLY = workflow("probe.yml", ["17 * * * *"], True, SLOT_STEP)
SPLIT = workflow(
    "probe.yml", list(parse_cron("17 * * * *").once_a_day_lines()), True, SLOT_STEP
)


class ProblemsTest(unittest.TestCase):
    def test_an_hourly_slot_workflow_is_named_with_the_lines_to_write(self) -> None:
        (problem,) = problems({"probe.yml": HOURLY})
        self.assertIn("probe.yml: runs the slot action", problem)
        self.assertIn("('17 0 * * *', '17 1 * * *', ..., '17 23 * * *')", problem)

    def test_the_lines_it_names_pass(self) -> None:
        # Positive control: writing what the refusal says is the whole fix.
        self.assertEqual(problems({"probe.yml": SPLIT}), [])

    def test_a_slot_workflow_the_clock_does_not_deliver_is_still_checked(
        self,
    ) -> None:
        # Not enrolled, so the clock never reads it; GitHub's schedule still
        # delivers it, and the slot step refuses every one of those runs.
        text = workflow("probe.yml", ["0 1,13 * * *"], False, SLOT_STEP)
        (problem,) = problems({"probe.yml": text})
        self.assertIn("('0 1 * * *', '0 13 * * *')", problem)
        zoned = text.replace('"0 1,13 * * *"\n', '"0 1 * * *"\n      timezone: UTC\n')
        (problem,) = problems({"probe.yml": zoned})
        self.assertIn("sets timezone", problem)

    def test_a_workflow_without_the_slot_action_may_fire_more_often(self) -> None:
        # A reconciler: every dispatch names its exact slot, and a repeated
        # schedule run is harmless, so nothing has to be attributed by time.
        checkout = "      - uses: actions/checkout@v7.0.1\n"
        self.assertEqual(
            problems(
                {
                    "watch.yml": workflow(
                        "watch.yml", ["*/20 * * * *"], True, checkout
                    ),
                    "plain.yml": workflow("plain.yml", ["17 * * * *"], False, checkout),
                }
            ),
            [],
        )

    def test_what_the_clock_refuses_in_an_enrolled_workflow_is_named(self) -> None:
        copied = workflow("backup.yml", ["30 19 * * *"], True, SLOT_STEP)
        (problem,) = problems({"restore.yml": copied})
        self.assertIn("are not 'github-cron-trigger/restore.yml'", problem)
        # A refused line names its file too, though the refusal comes from
        # the line alone.
        macro = workflow("backup.yml", ["@daily"], True, SLOT_STEP)
        (problem,) = problems({"backup.yml": macro})
        self.assertTrue(problem.startswith("backup.yml: cron '@daily'"), problem)

    def test_a_chain_off_a_split_parent_is_named(self) -> None:
        # The split gives the parent one line per instant, and a chained run
        # cannot tell which of them fired its parent.
        (problem,) = problems({"probe.yml": SPLIT, "chained.yml": chained("probe.yml")})
        self.assertIn("parent probe.yml carries more than one cron line", problem)
        daily = workflow("probe.yml", ["17 3 * * *"], True, SLOT_STEP)
        self.assertEqual(
            problems({"probe.yml": daily, "chained.yml": chained("probe.yml")}), []
        )


class MainTest(unittest.TestCase):
    def test_a_problem_fails_the_run(self) -> None:
        status, output = run({"probe.yml": HOURLY})
        self.assertEqual(status, 1)
        self.assertIn("check: problem: probe.yml: runs the slot action", output)

    def test_a_clean_directory_passes(self) -> None:
        # Only workflow files are read: GitHub runs nothing else in the
        # directory, so a stray note is no problem.
        status, output = run({"probe.yml": SPLIT, "NOTES.md": "not: [yaml"})
        self.assertEqual(status, 0, output)
        self.assertIn("1 workflow file(s)", output)

    def test_an_unreadable_workflow_fails_the_run(self) -> None:
        status, output = run({"probe.yml": SPLIT, "broken.yaml": "on: [push\n"})
        self.assertEqual(status, 1)
        self.assertIn("check: problem: broken.yaml: unreadable:", output)

    def test_a_missing_directory_fails_the_run(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = check.main(["--workflows", "/nonexistent/.github/workflows"])
        self.assertEqual(status, 1)
        self.assertIn("check: cannot read the workflows", output.getvalue())


if __name__ == "__main__":
    unittest.main()
