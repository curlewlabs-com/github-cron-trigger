"""Unit tests for reading the default branch's workflow files through a cache.

The risks: an idle tick that downloads or parses anything, which is the load
this reader exists to avoid; a cached text or parse served for content it does
not belong to, which would deliver from a stale or tampered file; a cache whose
loss or corruption changes what the clock decides, which would make it state
rather than a cache; and a tree read in two queries that describes two
different trees. GitHub's GraphQL answers are replaced by canned ones in the
shape CI's live check exercises; parsing runs the real yq.
"""

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from github_cron_trigger import default_branch, yq
from github_cron_trigger.default_branch import Cache, Entry, git_blob_id
from github_cron_trigger.github import GitHubError
from github_cron_trigger.workflow_triggers import schedule_entries, triggers

ENROLLED = (
    'on:\n  schedule:\n    - cron: "30 19 * * *"\n'
    "  repository_dispatch:\n    types: [github-cron-trigger/backup.yml]\njobs: {}\n"
)
PUSH = "on: push\njobs: {}\n"
TREE = "1" * 40
NEW_TREE = "2" * 40


def oid(text: str) -> str:
    return git_blob_id(text.encode("utf-8"))


class FakeGitHub:
    """Answers the tree id from `tree`, a tree listing from `trees`, and blob
    queries from `texts`, recording every call by kind."""

    def __init__(
        self, tree: str | None, trees: Mapping[str, Mapping[str, str]]
    ) -> None:
        self.tree = tree
        self.trees = trees
        self.texts = {
            oid(text): text for files in trees.values() for text in files.values()
        }
        self.calls: list[str] = []
        self.fetched: list[str] = []

    def __call__(self, query: str, variables: Mapping[str, str]) -> Any:
        if "HEAD:.github/workflows" in query:
            self.calls.append("tree")
            return {
                "repository": {
                    "object": None if self.tree is None else {"oid": self.tree}
                }
            }
        if "$tree" in query:
            self.calls.append("entries")
            files = self.trees[variables["tree"]]
            entries = [
                {"name": n, "type": "blob", "oid": oid(t)} for n, t in files.items()
            ]
            entries.append({"name": "README.md", "type": "blob", "oid": oid("readme")})
            entries.append({"name": "nested", "type": "tree", "oid": "3" * 40})
            return {"repository": {"object": {"entries": entries}}}
        self.calls.append("texts")
        answers: dict[str, Any] = {}
        for index, part in enumerate(query.split('object(oid: "')[1:]):
            asked = part[:40]
            self.fetched.append(asked)
            text = self.texts.get(asked)
            answers[f"b{index}"] = {
                "text": text,
                "isBinary": False,
                "isTruncated": False,
            }
        return {"repository": answers}


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.parses = 0

    def executable(self) -> str:
        self.parses += 1
        return yq.executable()

    def load(
        self, github: FakeGitHub, root: Path | None = None
    ) -> tuple[dict[str, object], dict[str, str]]:
        return default_branch.load(
            "owner/name", Cache(root or self.root), github, self.executable, lambda: 0.0
        )

    def test_a_first_read_reads_and_parses_every_workflow_file(self) -> None:
        github = FakeGitHub(TREE, {TREE: {"backup.yml": ENROLLED, "ci.yml": PUSH}})
        documents, unreadable = self.load(github)
        self.assertEqual(sorted(documents), ["backup.yml", "ci.yml"])
        self.assertEqual(unreadable, {})
        self.assertEqual(github.calls, ["tree", "entries", "texts"])
        self.assertEqual(sorted(github.fetched), sorted([oid(ENROLLED), oid(PUSH)]))

    def test_an_idle_read_asks_for_the_tree_id_and_nothing_else(self) -> None:
        github = FakeGitHub(TREE, {TREE: {"backup.yml": ENROLLED, "ci.yml": PUSH}})
        first = self.load(github)
        github.calls.clear()
        self.parses = 0
        self.assertEqual(self.load(github), first)
        self.assertEqual(github.calls, ["tree"])
        # Not even yq's version is checked: no process starts on an idle tick.
        self.assertEqual(self.parses, 0)

    def test_a_changed_file_is_the_only_one_fetched_and_parsed(self) -> None:
        changed = ENROLLED.replace("19", "20")
        github = FakeGitHub(
            TREE,
            {
                TREE: {"backup.yml": ENROLLED, "ci.yml": PUSH},
                NEW_TREE: {"backup.yml": changed, "ci.yml": PUSH},
            },
        )
        self.load(github)
        github.tree = NEW_TREE
        github.calls.clear()
        github.fetched.clear()
        documents, _ = self.load(github)
        self.assertEqual(github.calls, ["tree", "entries", "texts"])
        self.assertEqual(github.fetched, [oid(changed)])
        entries = schedule_entries(
            "backup.yml", triggers(documents["backup.yml"]).get("schedule")
        )
        self.assertEqual([entry.cron for entry in entries], ["30 20 * * *"])

    def test_losing_the_cache_costs_a_full_read_and_changes_nothing(self) -> None:
        github = FakeGitHub(TREE, {TREE: {"backup.yml": ENROLLED}})
        with_cache = self.load(github)
        github.calls.clear()
        self.assertEqual(self.load(github, root=self.root / "elsewhere"), with_cache)
        self.assertEqual(github.calls, ["tree", "entries", "texts"])

    def test_no_workflow_directory_reads_nothing(self) -> None:
        github = FakeGitHub(None, {})
        self.assertEqual(self.load(github), ({}, {}))
        self.assertEqual(github.calls, ["tree"])

    def test_a_file_that_does_not_parse_is_reported_every_time(self) -> None:
        github = FakeGitHub(TREE, {TREE: {"broken.yml": "on: [push\n"}})
        for _ in range(2):
            documents, unreadable = self.load(github)
            self.assertEqual((documents, list(unreadable)), ({}, ["broken.yml"]))

    def test_an_answer_in_another_shape_is_a_github_error(self) -> None:
        def odd(query: str, variables: Mapping[str, str]) -> Any:
            return {"repository": {"object": {"id": "not an oid"}}}

        with self.assertRaises(GitHubError):
            default_branch.load("owner/name", Cache(self.root), odd, self.executable)


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name))

    def test_git_blob_ids_match_git(self) -> None:
        # The ids `git hash-object` gives these contents.
        self.assertEqual(git_blob_id(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(
            git_blob_id(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a"
        )

    def test_a_text_is_kept_only_under_its_own_id(self) -> None:
        self.cache.put_text(oid(ENROLLED), ENROLLED)
        self.assertEqual(self.cache.text(oid(ENROLLED)), ENROLLED)
        self.cache.put_text(oid(PUSH), ENROLLED)
        self.assertIsNone(self.cache.text(oid(PUSH)))

    def test_a_changed_cache_file_is_a_miss(self) -> None:
        self.cache.put_text(oid(ENROLLED), ENROLLED)
        assert self.cache.root is not None
        stored = self.cache.root / "blobs" / oid(ENROLLED)[:2] / oid(ENROLLED)
        stored.write_text(ENROLLED.replace("19", "20"), encoding="utf-8")
        self.assertIsNone(self.cache.text(oid(ENROLLED)))

    def test_damaged_listings_and_parses_are_misses(self) -> None:
        assert self.cache.root is not None
        for kind in ("trees", f"parsed-{default_branch.PARSE_FORMAT}"):
            path = self.cache.root / kind / TREE[:2] / TREE
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.cache.entries(TREE))
        self.assertIsNone(self.cache.parsed(TREE))

    def test_entries_round_trip(self) -> None:
        entries = [Entry("a.yml", oid("a")), Entry("b.yaml", oid("b"))]
        self.cache.put_entries(TREE, entries)
        self.assertEqual(self.cache.entries(TREE), entries)

    def test_unused_entries_are_pruned_and_used_ones_kept(self) -> None:
        self.cache.put_text(oid(ENROLLED), ENROLLED)
        self.cache.put_text(oid(PUSH), PUSH)
        assert self.cache.root is not None
        unused = self.cache.root / "blobs" / oid(ENROLLED)[:2] / oid(ENROLLED)
        os.utime(unused, (0, 0))
        # Pruned as of a moment just past the unused entry's maximum age; the
        # other was written, so touched, just now.
        self.cache.prune(default_branch.CACHE_MAX_AGE + 1)
        self.assertFalse(unused.exists())
        self.assertEqual(self.cache.text(oid(PUSH)), PUSH)

    def test_no_directory_or_an_unwritable_one_only_misses(self) -> None:
        none = Cache(None)
        none.put_text(oid(ENROLLED), ENROLLED)
        self.assertIsNone(none.text(oid(ENROLLED)))
        with tempfile.NamedTemporaryFile() as blocker:
            blocked = Cache(Path(blocker.name))
            blocked.put_text(oid(ENROLLED), ENROLLED)
            self.assertIsNone(blocked.text(oid(ENROLLED)))


if __name__ == "__main__":
    unittest.main()
