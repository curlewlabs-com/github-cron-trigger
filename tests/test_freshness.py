"""Unit tests for the missed-slot check: which due slots have no ledger record.

Every slot the check names asks someone to act, so it can fail by naming too
much or too little: a slot named that was never owed (from before enrollment,
still in flight, or done in parts) is noise that trains people to ignore the
report, and a missed slot left out is the silence the check exists to break. A
workflow whose records cannot be read must be reported as unread rather than
judged, or an empty read would name every slot as missed - and the command must
not pass on such a read, or an outage would look like a clean ledger.
"""

import contextlib
import io
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from github_cron_trigger import workflow_files, yq
from github_cron_trigger.cron_slots import UTC, parse_cron
from github_cron_trigger.freshness import (
    EXIT_INCOMPLETE,
    EXIT_MISSED,
    GRACE,
    LOOKBACK,
    LedgeredLine,
    check,
    ledgered_lines,
    main,
    missed_lines,
)
from github_cron_trigger.github import GitHubError

SLOT_ACTION = "curlewlabs-com/github-cron-trigger/slot@v1"

DAILY = parse_cron("30 19 * * *")
BACKUP = [LedgeredLine(DAILY, "backup.yml")]
# 2026-09-29 12:00 UTC: the 28th's 19:30 slot is 16h30m old, past GRACE; the
# 29th's has not come yet.
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def records(*days: int, part: str | None = None) -> list[str]:
    """Records of the daily 19:30 line for these September 2026 days."""
    leaf = "" if part is None else f".{part}"
    return [f"30_19_x_x_x/202609{day:02d}T1930Z{leaf}" for day in days]


def document(
    enrolled: bool = True,
    slot_action: bool = True,
    cron: str = "30 19 * * *",
    workflow_file: str = "backup.yml",
) -> dict[str, object]:
    """A parsed workflow, in the shape workflow_files hands the check."""
    on: dict[str, object] = {"schedule": [{"cron": cron}], "workflow_dispatch": None}
    if enrolled:
        on["repository_dispatch"] = {"types": [f"github-cron-trigger/{workflow_file}"]}
    uses = SLOT_ACTION if slot_action else "actions/checkout@v7"
    return {"on": on, "jobs": {"slot": {"steps": [{"uses": uses}]}}}


class MissedLinesTest(unittest.TestCase):
    def test_a_day_with_no_record_is_named(self) -> None:
        missed = missed_lines(
            "backup.yml", BACKUP, records(22, 23, 24, 26, 27, 28), NOW
        )
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0].workflow_file, "backup.yml")
        self.assertEqual(missed[0].slots, (utc(2026, 9, 25, 19, 30),))

    def test_every_slot_recorded_names_nothing(self) -> None:
        self.assertEqual(
            missed_lines("backup.yml", BACKUP, records(*range(22, 29)), NOW), []
        )

    def test_a_slot_inside_the_grace_is_still_in_flight(self) -> None:
        # The 28th's slot is 16h30m old at NOW; eleven hours after it, it is
        # still inside GRACE and not yet missed.
        in_flight = utc(2026, 9, 28, 19, 30) + GRACE - timedelta(hours=1)
        self.assertEqual(
            missed_lines("backup.yml", BACKUP, records(*range(22, 28)), in_flight), []
        )
        # Past GRACE with no record, it is.
        missed = missed_lines("backup.yml", BACKUP, records(*range(22, 28)), NOW)
        self.assertEqual(missed[0].slots, (utc(2026, 9, 28, 19, 30),))

    def test_slots_before_the_oldest_record_are_not_misses(self) -> None:
        # Enrolled on the 26th: the 22nd to the 25th were never owed a record.
        self.assertEqual(
            missed_lines("backup.yml", BACKUP, records(26, 27, 28), NOW), []
        )

    def test_a_line_that_never_recorded_is_not_checked(self) -> None:
        # Nothing tells a line that just enrolled from one whose every delivery
        # failed, so neither is reported yet.
        self.assertEqual(missed_lines("backup.yml", BACKUP, [], NOW), [])

    def test_a_slot_recorded_in_parts_counts(self) -> None:
        # One leg done is a delivered slot; its failed leg is a failing run.
        names = records(*range(22, 29)) + records(25, part="production")
        names.remove("30_19_x_x_x/20260925T1930Z")
        self.assertEqual(missed_lines("backup.yml", BACKUP, names, NOW), [])

    def test_the_window_reaches_back_lookback_at_most(self) -> None:
        # Records from long ago, then a gap: only the slots inside LOOKBACK are
        # named, and a recorded slot older than it still opens the line.
        names = [f"30_19_x_x_x/{day}T1930Z" for day in ("20260801", "20260802")]
        missed = missed_lines("backup.yml", BACKUP, names, NOW)
        self.assertEqual(
            missed[0].slots,
            tuple(
                slot
                for slot in (utc(2026, 9, day, 19, 30) for day in range(22, 29))
                if slot > NOW - LOOKBACK
            ),
        )

    def test_another_lines_records_do_not_count(self) -> None:
        # A schedule that moved from 19:30 to 20:00 keeps the old line's
        # records; they say nothing about the new line.
        moved = [LedgeredLine(parse_cron("0 20 * * *"), "backup.yml")]
        self.assertEqual(
            missed_lines("backup.yml", moved, records(*range(22, 29)), NOW), []
        )

    def test_now_in_a_zone_on_another_date_is_the_same_instant(self) -> None:
        # 12:00 UTC on the 29th is 01:00 on the 30th at UTC+13, and the window's
        # far edge, 12:00 UTC on the 22nd, is 01:00 on the 23rd there. The slots
        # are UTC instants, so the 22nd's slot, the first inside the window, is
        # still named: a walk over local dates would start on the 23rd.
        ahead = NOW.astimezone(timezone(timedelta(hours=13)))
        names = records(21, 23, 24, 26, 27, 28)
        missed = missed_lines("backup.yml", BACKUP, names, ahead)
        self.assertEqual(missed, missed_lines("backup.yml", BACKUP, names, NOW))
        self.assertEqual(
            missed[0].slots, (utc(2026, 9, 22, 19, 30), utc(2026, 9, 25, 19, 30))
        )

    def test_a_key_naming_no_instant_is_not_a_record(self) -> None:
        # ledger_key never writes a 31 February, so the ref is not the ledger's.
        # Taken as the oldest record it would stop the whole check on an
        # unparseable date; it opens no line and holds no slot instead.
        names = records(22, 23, 24, 26, 27, 28) + ["30_19_x_x_x/20260231T1930Z"]
        missed = missed_lines("backup.yml", BACKUP, names, NOW)
        self.assertEqual(missed[0].slots, (utc(2026, 9, 25, 19, 30),))


def chained(parent_name: str = "Backup") -> dict[str, object]:
    """A parsed workflow chained through workflow_run off `parent_name`, running
    the slot action: an audit that reads what a backup wrote, say."""
    return {
        "on": {"workflow_run": {"workflows": [parent_name], "types": ["completed"]}},
        "jobs": {"slot": {"steps": [{"uses": SLOT_ACTION}]}},
    }


def named(name: str, doc: dict[str, object]) -> dict[str, object]:
    return {"name": name, **doc}


class LedgeredLinesTest(unittest.TestCase):
    def test_only_an_enrolled_workflow_running_the_slot_action_is_checked(
        self,
    ) -> None:
        lines, problems = ledgered_lines("backup.yml", document(), {})
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "backup.yml")],
        )
        self.assertEqual(problems, [])
        # Not enrolled: GitHub's schedule alone delivers it, which the check does
        # not answer for, though the slot action records those deliveries.
        self.assertEqual(
            ledgered_lines("backup.yml", document(enrolled=False), {}), ([], [])
        )
        # Enrolled without the slot action (a reconciler): it records nothing.
        self.assertEqual(
            ledgered_lines("backup.yml", document(slot_action=False), {}), ([], [])
        )

    def test_a_chained_workflow_is_checked_on_its_parents_line(self) -> None:
        # The audit's records are keyed by the backup's line, under the audit's
        # own file: that line is the one to check them against.
        documents = {
            "backup.yml": named("Backup", document()),
            "audit.yml": chained(),
        }
        lines, problems = ledgered_lines("audit.yml", documents["audit.yml"], documents)
        # Delivered through the parent: re-delivering the audit's missed slot
        # means dispatching the backup's.
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "backup.yml")],
        )
        self.assertEqual(problems, [])

    def test_a_parent_with_no_name_is_matched_by_its_path(self) -> None:
        # GitHub names a workflow without `name:` by its path, and that is what
        # a workflow_run trigger must name to follow it.
        documents = {
            "backup.yml": document(),
            "audit.yml": chained(".github/workflows/backup.yml"),
        }
        lines, problems = ledgered_lines("audit.yml", documents["audit.yml"], documents)
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "backup.yml")],
        )
        self.assertEqual(problems, [])

    def test_an_own_line_and_a_chained_line_are_each_checked(self) -> None:
        # A cleanup with its own daily line, and a weekly pass chained off the
        # tool update's line.
        own_and_chained: dict[str, object] = {
            "on": {
                "schedule": [{"cron": "17 9 * * *"}],
                "repository_dispatch": {"types": ["github-cron-trigger/cleanup.yml"]},
                "workflow_run": {"workflows": ["Update tools"]},
            },
            "jobs": {"slot": {"steps": [{"uses": SLOT_ACTION}]}},
        }
        documents = {
            "tools-update.yml": named(
                "Update tools",
                document(cron="0 3 * * 1", workflow_file="tools-update.yml"),
            ),
            "cleanup.yml": own_and_chained,
        }
        lines, problems = ledgered_lines("cleanup.yml", own_and_chained, documents)
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("17 9 * * *", "cleanup.yml"), ("0 3 * * 1", "tools-update.yml")],
        )
        self.assertEqual(problems, [])

    def test_each_parent_a_chain_names_is_checked_on_its_line(self) -> None:
        # The slot action resolves a chained run by the parent that started it,
        # so a chain off more than one parent records on each parent's line.
        documents = {
            "backup.yml": named("Backup", document()),
            "logs.yml": named(
                "Logs", document(cron="0 1 * * *", workflow_file="logs.yml")
            ),
            "audit.yml": {
                "on": {"workflow_run": {"workflows": ["Backup", "Logs"]}},
                "jobs": chained()["jobs"],
            },
        }
        lines, problems = ledgered_lines("audit.yml", documents["audit.yml"], documents)
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "backup.yml"), ("0 1 * * *", "logs.yml")],
        )
        self.assertEqual(problems, [])

    def test_a_line_reached_more_than_once_is_checked_once(self) -> None:
        # Records are keyed by the line alone, so judging it again would name
        # each of its missed slots again. Its own enrollment delivers it.
        documents = {
            "backup.yml": named("Backup", document()),
            "logs.yml": named("Logs", document(workflow_file="logs.yml")),
            "audit.yml": {
                "on": {
                    "schedule": [{"cron": "30 19 * * *"}],
                    "repository_dispatch": {"types": ["github-cron-trigger/audit.yml"]},
                    "workflow_run": {"workflows": ["Backup", "Logs"]},
                },
                "jobs": chained()["jobs"],
            },
        }
        lines, problems = ledgered_lines("audit.yml", documents["audit.yml"], documents)
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "audit.yml")],
        )
        self.assertEqual(problems, [])

    def test_a_parent_with_no_cron_line_adds_nothing(self) -> None:
        # A run after CI's push runs is never a scheduled delivery, so the chain
        # records nothing through it. Reported, it would fail every run of the
        # check over a workflow the slot action handles as designed.
        documents = {
            "ci.yml": {"name": "CI", "on": {"push": None}},
            "backup.yml": {
                "on": {
                    "schedule": [{"cron": "30 19 * * *"}],
                    "repository_dispatch": {
                        "types": ["github-cron-trigger/backup.yml"]
                    },
                    "workflow_run": {"workflows": ["CI"]},
                },
                "jobs": chained()["jobs"],
            },
        }
        lines, problems = ledgered_lines(
            "backup.yml", documents["backup.yml"], documents
        )
        self.assertEqual(
            [(line.schedule.canonical, line.delivered_by) for line in lines],
            [("30 19 * * *", "backup.yml")],
        )
        self.assertEqual(problems, [])

    def test_a_parent_that_is_not_enrolled_adds_nothing(self) -> None:
        # GitHub alone delivers such a parent, as it does a line the workflow
        # itself is not enrolled for, and no dispatch could re-deliver a slot
        # missed through it.
        documents = {
            "backup.yml": named("Backup", document(enrolled=False)),
            "audit.yml": chained(),
        }
        self.assertEqual(
            ledgered_lines("audit.yml", documents["audit.yml"], documents), ([], [])
        )

    def test_a_parent_that_cannot_be_pinned_down_is_reported(self) -> None:
        # Each of these chains can never record: no workflow is the parent, or
        # the slot action refuses every scheduled run chained off it. The line
        # is reported rather than guessed.
        def parent(*entries: dict[str, str]) -> dict[str, object]:
            return {
                "name": "Backup",
                "on": {
                    "schedule": list(entries),
                    "repository_dispatch": {
                        "types": ["github-cron-trigger/backup.yml"]
                    },
                },
            }

        cases: dict[str, dict[str, object]] = {
            "no parent named": {
                "audit.yml": {
                    "on": {"workflow_run": {"types": ["completed"]}},
                    "jobs": chained()["jobs"],
                },
            },
            "no such parent": {"audit.yml": chained("Missing")},
            "parent with two lines": {
                "backup.yml": parent({"cron": "30 19 * * *"}, {"cron": "0 7 * * *"}),
                "audit.yml": chained(),
            },
            "parent firing more than once a day": {
                "backup.yml": parent({"cron": "30 7,19 * * *"}),
                "audit.yml": chained(),
            },
            "parent that is zoned": {
                "backup.yml": parent(
                    {"cron": "30 19 * * *", "timezone": "America/New_York"}
                ),
                "audit.yml": chained(),
            },
        }
        for label, documents in cases.items():
            with self.subTest(label=label):
                lines, problems = ledgered_lines(
                    "audit.yml", documents["audit.yml"], documents
                )
                self.assertEqual(lines, [])
                self.assertEqual(len(problems), 1)


class CheckTest(unittest.TestCase):
    def test_a_missed_slot_is_reported_across_workflows(self) -> None:
        report = check(
            "owner/name",
            {"backup.yml": document(), "other.yml": document(enrolled=False)},
            NOW,
            records=lambda _repo, _file: records(22, 23, 24, 26, 27, 28),
        )
        self.assertEqual(report.checked, ("backup.yml",))
        self.assertEqual(
            [line.slots for line in report.missed], [(utc(2026, 9, 25, 19, 30),)]
        )
        self.assertEqual(report.unreadable, ())

    def test_unreadable_records_are_reported_not_judged(self) -> None:
        # An empty read would name every slot as missed; the workflow is set
        # aside with its reason instead.
        def failing(_repo: str, _file: str) -> list[str]:
            raise GitHubError("gh: Server Error (HTTP 502)")

        report = check("owner/name", {"backup.yml": document()}, NOW, records=failing)
        self.assertEqual(report.missed, ())
        self.assertEqual(report.checked, ())
        self.assertEqual(
            report.unreadable, (("backup.yml", "gh: Server Error (HTTP 502)"),)
        )

    def test_a_chained_workflows_missed_slot_is_reported(self) -> None:
        documents = {
            "backup.yml": named("Backup", document()),
            "audit.yml": chained(),
        }
        record_names = {
            "backup.yml": records(*range(22, 29)),
            # The audit's legs record parts; the 25th has none.
            "audit.yml": records(22, 23, 24, 26, 27, 28, part="production"),
        }
        report = check(
            "owner/name",
            documents,
            NOW,
            records=lambda _repo, workflow_file: record_names[workflow_file],
        )
        self.assertEqual(report.checked, ("audit.yml", "backup.yml"))
        self.assertEqual(
            [
                (line.workflow_file, line.delivered_by, line.slots)
                for line in report.missed
            ],
            [("audit.yml", "backup.yml", (utc(2026, 9, 25, 19, 30),))],
        )

    def test_judged_lines_are_only_those_whose_records_were_read(self) -> None:
        # A caller treats a judged line with nothing missed as recovered, so a
        # line refused, never recorded, or behind an unread ledger must not be
        # among them.
        with_bad_chain = {
            "on": {
                "schedule": [{"cron": "30 19 * * *"}],
                "repository_dispatch": {"types": ["github-cron-trigger/backup.yml"]},
                "workflow_run": {"workflows": ["Missing"]},
            },
            "jobs": chained()["jobs"],
        }
        documents: dict[str, object] = {
            "backup.yml": with_bad_chain,
            "fresh.yml": document(workflow_file="fresh.yml", cron="0 7 * * *"),
            "logs.yml": document(workflow_file="logs.yml"),
        }

        def read(_repo: str, workflow_file: str) -> list[str]:
            if workflow_file == "logs.yml":
                raise GitHubError("gh: Server Error (HTTP 502)")
            return records(*range(22, 29)) if workflow_file == "backup.yml" else []

        report = check("owner/name", documents, NOW, records=read)
        self.assertEqual(report.judged, (("backup.yml", DAILY),))

    def test_an_enrolled_workflow_that_cannot_be_read_is_reported(self) -> None:
        # Enrolled with a line firing twice a day and the slot action: the
        # clock refuses it, and so does the check, by name.
        report = check(
            "owner/name",
            {"backup.yml": document(cron="0 1,13 * * *")},
            NOW,
            records=lambda _repo, _file: [],
        )
        self.assertEqual(report.checked, ())
        self.assertEqual(len(report.unreadable), 1)
        self.assertEqual(report.unreadable[0][0], "backup.yml")


def enrolled_yaml(workflow_file: str) -> str:
    """An enrolled workflow file on the daily 19:30 line, running the slot
    action."""
    return f"""\
on:
  schedule:
    - cron: "30 19 * * *"
  repository_dispatch:
    types: [github-cron-trigger/{workflow_file}]
jobs:
  slot:
    runs-on: ubuntu-latest
    steps:
      - uses: curlewlabs-com/github-cron-trigger/slot@v1
"""


class MainTest(unittest.TestCase):
    """The command's exit status is what a run of it turns red on, so a read
    that reached only part of what it had to check must not pass as one that
    found nothing missed, and a missed slot must not hide behind it. Workflow
    files are real YAML parsed as the command parses them."""

    def run_main(
        self, files: Mapping[str, str], ledger: Mapping[str, list[str] | None]
    ) -> int:
        def records(_repo: str, workflow_file: str) -> list[str]:
            names = ledger[workflow_file]
            if names is None:
                raise GitHubError("gh: Server Error (HTTP 502)")
            return names

        def load(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            return workflow_files.parse_all(files, yq.executable())

        with contextlib.redirect_stdout(io.StringIO()):
            return main(["--repo", "owner/name"], NOW, records, load)

    def test_every_slot_recorded_passes(self) -> None:
        files = {"backup.yml": enrolled_yaml("backup.yml")}
        ledger = {"backup.yml": records(*range(22, 29))}
        self.assertEqual(self.run_main(files, ledger), 0)

    def test_a_missed_slot_fails(self) -> None:
        files = {"backup.yml": enrolled_yaml("backup.yml")}
        ledger = {"backup.yml": records(22, 23, 24, 26, 27, 28)}
        self.assertEqual(self.run_main(files, ledger), EXIT_MISSED)

    def test_a_ledger_that_could_not_be_read_does_not_pass(self) -> None:
        files = {"backup.yml": enrolled_yaml("backup.yml")}
        self.assertEqual(self.run_main(files, {"backup.yml": None}), EXIT_INCOMPLETE)

    def test_workflows_that_could_not_be_read_at_all_do_not_pass(self) -> None:
        def unreachable(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            raise GitHubError("gh: Server Error (HTTP 502)")

        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["--repo", "owner/name"], NOW, load=unreachable)
        self.assertEqual(status, EXIT_INCOMPLETE)

    def test_a_workflow_file_that_did_not_parse_does_not_pass(self) -> None:
        # Nothing is missed among what was read, but the file that did not
        # parse may be an enrolled workflow whose slots nobody checked.
        files = {"backup.yml": enrolled_yaml("backup.yml"), "broken.yml": "on: [push\n"}
        ledger = {"backup.yml": records(*range(22, 29))}
        self.assertEqual(self.run_main(files, ledger), EXIT_INCOMPLETE)

    def test_a_missed_slot_and_an_unread_ledger_each_set_their_status(self) -> None:
        files = {
            "backup.yml": enrolled_yaml("backup.yml"),
            "logs.yml": enrolled_yaml("logs.yml"),
        }
        ledger = {"backup.yml": records(22, 23, 24, 26, 27, 28), "logs.yml": None}
        self.assertEqual(self.run_main(files, ledger), EXIT_MISSED | EXIT_INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
