"""Workflow files, read from GitHub and parsed into the document GitHub reads.

The slot action reads one file at the commit its run executed (fetch_file),
never from a checkout, so it needs no checkout of the repository it runs in.
The clock's and the missed-slot check's reads of the default branch are
default_branch.py's.

PARSING goes through mikefarah's yq on PATH (yq.py), whose JSON output Python reads. A
file is refused rather than half-read when it is not one workflow GitHub would
run: a parse error, a second YAML document, a repeated mapping key - which yq
passes through to its JSON, where the last value would silently win - or a top
level that is not a mapping, such as an empty file.
"""

import json
import re
import subprocess
from collections.abc import Mapping
from typing import Any

from . import yq
from .github import check_repo, gh_api

WORKFLOW_SUFFIXES = (".yml", ".yaml")
WORKFLOW_DIRECTORY = ".github/workflows"

# Where a workflow file may live. Anchored because the path arrives in an event
# payload or a workflow ref, and becomes part of an API path.
_WORKFLOW_PATH = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

# yq evaluates every document of the input together (`eval-all`) and emits
# them as one JSON array, so a second document shows up as a second element
# instead of passing unseen. `explode` resolves anchors and aliases, and the
# flag merges `<<` keys as the YAML spec says, with the mapping's own keys
# winning, which is how GitHub reads them.
_YQ_ARGUMENTS = (
    "eval-all",
    "--yaml-fix-merge-anchor-to-spec=true",
    "--output-format=json",
    "--indent=0",
    "[explode(.)]",
    "-",
)

# A bound on one parse, so a wedged yq cannot hold a run open.
_YQ_TIMEOUT_SECONDS = 60


class UnreadableWorkflow(ValueError):
    """A workflow file that cannot be read as one workflow document."""


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise UnreadableWorkflow(f"mapping key {repeated[0]!r} is repeated")
    return dict(pairs)


def parse(text: str, executable: str) -> Mapping[str, object]:
    """One workflow file's text as the document GitHub reads.

    Raises UnreadableWorkflow when the text is not one workflow document.
    """
    try:
        completed = subprocess.run(
            [executable, *_YQ_ARGUMENTS],
            input=text,
            capture_output=True,
            encoding="utf-8",
            timeout=_YQ_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise yq.ParserUnavailable(f"could not run {executable}: {exc}") from exc
    if completed.returncode != 0:
        reason = completed.stderr.strip().splitlines()
        raise UnreadableWorkflow(reason[-1] if reason else "yq refused it")
    try:
        documents = json.loads(
            completed.stdout, object_pairs_hook=_refuse_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise UnreadableWorkflow(
            f"yq answered something other than JSON: {exc}"
        ) from exc
    if not isinstance(documents, list) or len(documents) != 1:
        count = len(documents) if isinstance(documents, list) else "no"
        raise UnreadableWorkflow(f"holds {count} YAML documents, not one")
    document = documents[0]
    if not isinstance(document, dict):
        raise UnreadableWorkflow("its top level is not a mapping")
    return document


def parse_all(
    texts: Mapping[str, str], executable: str
) -> tuple[dict[str, object], dict[str, str]]:
    """Every file parsed, keyed by file name, and the files that could not be,
    with the reason - kept apart so one unreadable file does not stop the rest
    being read."""
    documents: dict[str, object] = {}
    unreadable: dict[str, str] = {}
    for name, text in sorted(texts.items()):
        try:
            documents[name] = parse(text, executable)
        except UnreadableWorkflow as exc:
            unreadable[name] = str(exc)
    return documents, unreadable


def check_workflow_path(path: str) -> str:
    """`path` itself, once it is known to name a workflow file."""
    if not _WORKFLOW_PATH.match(path):
        raise ValueError(f"path {path!r} is not a workflow file")
    return path


def fetch_file(repo: str, path: str, commit: str) -> str:
    """The text of one workflow file at a commit."""
    if not _COMMIT.match(commit):
        raise ValueError(f"commit {commit!r} is not a full object name")
    return gh_api(
        [
            "-H",
            "Accept: application/vnd.github.raw+json",
            f"repos/{check_repo(repo)}/contents/{check_workflow_path(path)}?ref={commit}",
        ]
    )
