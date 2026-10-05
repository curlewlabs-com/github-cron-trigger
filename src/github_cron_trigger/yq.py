"""The yq every workflow file is parsed with: mikefarah's, from PATH.

Python's standard library has no YAML parser, and a hand-rolled reader of
GitHub's workflow syntax would misread some valid file sooner or later. yq
(github.com/mikefarah/yq) is a single static binary that turns YAML into JSON,
and GitHub's hosted runners already carry it. A self-hosted runner or a clock
host installs it once, like any other tool it runs; nothing here downloads it.

Two things on PATH can be called `yq`. Debian's and Ubuntu's package of that
name is a different program, a Python wrapper around jq with another command
line, so the version line is checked for mikefarah's before anything is passed
to it.
"""

import re
import shutil
import subprocess

# The oldest release that reads every workflow the way GitHub does:
# --yaml-fix-merge-anchor-to-spec (which workflow_files passes) arrived in
# v4.47.1, and v4.53.4 fixed how `explode` rebuilds a merge anchor's parent.
MINIMUM_VERSION = (4, 53, 4)

_VERSION = re.compile(r"mikefarah/yq/?\)? version v?(\d+)\.(\d+)\.(\d+)")

_INSTALL_HINT = (
    "install mikefarah's yq v{}.{}.{} or newer from"
    " https://github.com/mikefarah/yq/releases (Homebrew's `yq` is it; the apt"
    " package named `yq` is a different program)"
).format(*MINIMUM_VERSION)


class ParserUnavailable(Exception):
    """No usable yq is on PATH, so nothing can be parsed. Named in the message."""


def version_of(executable: str) -> tuple[int, int, int] | None:
    """The version a mikefarah yq reports, or None for anything else."""
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION.search(completed.stdout)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def executable() -> str:
    """The path of the yq on PATH, once it is mikefarah's at MINIMUM_VERSION or
    newer within the same major version."""
    found = shutil.which("yq")
    if found is None:
        raise ParserUnavailable(f"no yq on PATH; {_INSTALL_HINT}")
    version = version_of(found)
    if version is None:
        raise ParserUnavailable(f"{found} is not mikefarah's yq; {_INSTALL_HINT}")
    # A different major version may read YAML or take arguments differently.
    if version[0] != MINIMUM_VERSION[0] or version < MINIMUM_VERSION:
        shown = ".".join(str(part) for part in version)
        raise ParserUnavailable(f"{found} is yq v{shown}; {_INSTALL_HINT}")
    return found
