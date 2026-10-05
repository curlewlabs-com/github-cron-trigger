"""Unit tests for finding the yq to parse with.

Every workflow decision rests on the parsed file, so the yq the tool runs must
be one that reads YAML the way GitHub does. The risks are running something
else called `yq` - the apt package of that name is a different program, which
would be handed arguments it reads differently - and running a mikefarah
release too old to read merge anchors as the YAML spec says. Each case puts a
real executable on PATH that answers `--version` the way that program does.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from github_cron_trigger import yq


class ExecutableTest(unittest.TestCase):
    def on_path(self, version_line: str | None) -> str:
        """A PATH holding one `yq` that prints `version_line`, or none."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        if version_line is not None:
            program = Path(directory.name) / "yq"
            program.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
            program.chmod(program.stat().st_mode | stat.S_IXUSR)
        return directory.name

    def run_with(self, version_line: str | None) -> str:
        path = self.on_path(version_line)
        saved = os.environ["PATH"]
        os.environ["PATH"] = path
        try:
            return yq.executable()
        finally:
            os.environ["PATH"] = saved

    def test_mikefarahs_at_or_past_the_minimum_is_used(self) -> None:
        for line in (
            "yq (https://github.com/mikefarah/yq/) version v4.53.4",
            "yq (https://github.com/mikefarah/yq/) version v4.54.1",
            "yq (https://github.com/mikefarah/yq/) version v4.60.0",
        ):
            with self.subTest(line=line):
                self.assertTrue(self.run_with(line).endswith("/yq"))

    def test_anything_else_is_refused_with_how_to_get_the_right_one(self) -> None:
        for label, line in {
            "no yq at all": None,
            "the apt package's yq": "yq 3.4.3",
            "a mikefarah release before the minimum": (
                "yq (https://github.com/mikefarah/yq/) version v4.47.1"
            ),
            "another major version": (
                "yq (https://github.com/mikefarah/yq/) version v5.0.0"
            ),
        }.items():
            with self.subTest(label), self.assertRaises(yq.ParserUnavailable) as caught:
                self.run_with(line)
            self.assertIn("mikefarah", str(caught.exception))


class InstalledTest(unittest.TestCase):
    def test_this_hosts_yq_is_usable(self) -> None:
        # The rest of the suite parses real YAML with it; a host without one
        # fails here with the reason rather than in every parsing test.
        self.assertIsNotNone(yq.version_of(yq.executable()))


if __name__ == "__main__":
    unittest.main()
