"""The workflow files on a repository's default branch, read so that an idle
tick costs one small request.

A clock ticks every few minutes and workflow files change a few times a day,
so nearly every tick would re-read what it read last time. A repository's
workflow files can run to megabytes, and each would need a yq run to parse.
Git names every tree and file version by a hash of its content, so a reader
can ask for that name first and reuse everything it already has under it:

1. One query asks for the id of the default branch's `.github/workflows` tree.
   That id changes only when a workflow file changes.
2. If the tree is new to this host, a second query lists its entries - names
   and blob ids - by the tree's id, so they describe exactly that tree.
3. A file whose blob was parsed before reuses the parse. Otherwise its text
   comes from the cache, or one batched query fetches every missing text, and
   yq parses only those files.

THE CACHE IS ONLY A CACHE. Everything in it is keyed by a content id, so it is
never stale: a changed file has a new id and misses. A cached text is checked
against its id by re-hashing it, and anything unreadable is a miss. Deleting
the cache costs one full read and changes no decision, so a host still keeps
nothing that matters (clock_marks.py). Entries unused for CACHE_MAX_AGE are
pruned whenever a read had to add something.

A parse is keyed by blob id and PARSE_FORMAT, not by yq's version: the yq
floor (yq.MINIMUM_VERSION) is set where every supported release reads workflow
files alike, so an idle tick runs no yq at all.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import workflow_files, yq
from .github import GitHubError, check_repo, gh_api

# Bumped whenever the parse rules in workflow_files.parse change, or the yq
# floor moves, so every earlier parse misses.
PARSE_FORMAT = "1"

# How long a cache entry nobody used is kept. A file version a repository no
# longer has is dead weight; one it still has is touched on every read.
CACHE_MAX_AGE = 30 * 24 * 3600

# Texts per batched query: well inside GitHub's GraphQL node limits.
_BLOB_BATCH = 50

_OID = re.compile(r"^[0-9a-f]{40}$")

# Runs one GraphQL query: (query, variables) -> its `data`. Replaced in tests.
GraphQL = Callable[[str, Mapping[str, str]], Any]

# Reads one repository: owner/name -> the parsed workflows by file name, and
# the files that could not be read or parsed, with the reason.
Loader = Callable[[str], tuple[dict[str, object], dict[str, str]]]

_TREE_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: "HEAD:.github/workflows") { oid }
  }
}
"""

_ENTRIES_QUERY = """
query($owner: String!, $name: String!, $tree: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    object(oid: $tree) { ... on Tree { entries { name type oid } } }
  }
}
"""


def graphql(query: str, variables: Mapping[str, str]) -> Any:
    """Run one GraphQL query through `gh` and return its `data`."""
    arguments = ["graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        arguments += ["-f", f"{name}={value}"]
    answer = json.loads(gh_api(arguments))
    if not isinstance(answer, dict) or not isinstance(answer.get("data"), dict):
        raise GitHubError(f"GraphQL answered without data: {answer!r}"[:500])
    return answer["data"]


def git_blob_id(data: bytes) -> str:
    """The id git gives a blob holding `data`."""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One workflow file in a tree: its name and its blob's id."""

    name: str
    oid: str


class Cache:
    """Tree listings, file texts and parses, on disk, keyed by content id.
    Every failure here is a miss, never an error: nothing depends on it."""

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.wrote = False

    def _path(self, kind: str, oid: str) -> Path | None:
        if self.root is None or not _OID.match(oid):
            return None
        return self.root / kind / oid[:2] / oid

    def _read(self, path: Path | None) -> bytes | None:
        if path is None:
            return None
        try:
            data = path.read_bytes()
            # Touched so pruning keeps what is still in use.
            os.utime(path)
        except OSError:
            return None
        return data

    def _write(self, path: Path | None, data: bytes) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        except OSError:
            return
        self.wrote = True

    def entries(self, tree: str) -> list[Entry] | None:
        data = self._read(self._path("trees", tree))
        try:
            listed = json.loads(data) if data is not None else None
            if not isinstance(listed, list):
                return None
            entries = [Entry(str(name), str(oid)) for name, oid in listed]
        except (ValueError, TypeError):
            return None
        if not all(_OID.match(entry.oid) for entry in entries):
            return None
        return entries

    def put_entries(self, tree: str, entries: Sequence[Entry]) -> None:
        listed = [[entry.name, entry.oid] for entry in entries]
        self._write(self._path("trees", tree), json.dumps(listed).encode("utf-8"))

    def text(self, oid: str) -> str | None:
        data = self._read(self._path("blobs", oid))
        if data is None or git_blob_id(data) != oid:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def put_text(self, oid: str, text: str) -> None:
        data = text.encode("utf-8")
        # A text that does not hash to its id would be refused on every read.
        if git_blob_id(data) == oid:
            self._write(self._path("blobs", oid), data)

    def parsed(self, oid: str) -> tuple[object, str | None] | None:
        """(document, None) or (None, the reason it did not parse)."""
        data = self._read(self._path(f"parsed-{PARSE_FORMAT}", oid))
        try:
            stored = json.loads(data) if data is not None else None
        except ValueError:
            return None
        if not isinstance(stored, dict):
            return None
        if "error" in stored and isinstance(stored["error"], str):
            return None, stored["error"]
        if "document" in stored:
            return stored["document"], None
        return None

    def put_parsed(self, oid: str, document: object, error: str | None) -> None:
        stored = {"error": error} if error is not None else {"document": document}
        path = self._path(f"parsed-{PARSE_FORMAT}", oid)
        self._write(path, json.dumps(stored).encode("utf-8"))

    def prune(self, now: float) -> None:
        """Remove entries unused for CACHE_MAX_AGE."""
        if self.root is None:
            return
        try:
            files = [path for path in self.root.rglob("*") if path.is_file()]
        except OSError:
            return
        for path in files:
            try:
                if now - path.stat().st_mtime > CACHE_MAX_AGE:
                    path.unlink()
            except OSError:
                continue


def default_cache() -> Cache:
    """The user cache directory's cache, shared by every repository a host
    reads: content ids are the same in every repository."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".cache"
    return Cache(root / "github-cron-trigger")


def _tree_entries(answer: Any) -> list[Entry]:
    entries: list[Entry] = []
    for entry in answer["entries"]:
        name, kind, oid = entry["name"], entry["type"], entry["oid"]
        if kind != "blob" or not name.endswith(workflow_files.WORKFLOW_SUFFIXES):
            continue
        if not _OID.match(oid):
            raise GitHubError(f"workflow file {name!r} answered blob id {oid!r}")
        entries.append(Entry(name, oid))
    return sorted(entries, key=lambda entry: entry.name)


def _fetch_texts(
    owner: str, name: str, oids: Sequence[str], call: GraphQL
) -> dict[str, str | None]:
    """Each blob's text, or None where GitHub returned no complete text."""
    texts: dict[str, str | None] = {}
    for start in range(0, len(oids), _BLOB_BATCH):
        batch = oids[start : start + _BLOB_BATCH]
        # The ids are interpolated, so each was matched against _OID first.
        fields = " ".join(
            f'b{index}: object(oid: "{oid}") {{ ... on Blob {{ text isBinary isTruncated }} }}'
            for index, oid in enumerate(batch)
        )
        query = (
            "query($owner: String!, $name: String!) {"
            f" repository(owner: $owner, name: $name) {{ {fields} }} }}"
        )
        node = call(query, {"owner": owner, "name": name})["repository"]
        for index, oid in enumerate(batch):
            blob = node.get(f"b{index}")
            complete = (
                isinstance(blob, dict)
                and isinstance(blob.get("text"), str)
                and not blob.get("isBinary")
                and not blob.get("isTruncated")
            )
            texts[oid] = blob["text"] if complete else None
    return texts


def load(
    repo: str,
    cache: Cache | None = None,
    call: GraphQL = graphql,
    executable: Callable[[], str] = yq.executable,
    now: Callable[[], float] = time.time,
) -> tuple[dict[str, object], dict[str, str]]:
    """Every workflow on the default branch, parsed, and the files that could
    not be read or parsed, by name, with the reason."""
    store = cache if cache is not None else default_cache()
    owner, name = check_repo(repo).split("/")
    variables = {"owner": owner, "name": name}
    try:
        tree = call(_TREE_QUERY, variables)["repository"]["object"]
        # A repository with no workflow directory has nothing to read.
        if tree is None:
            return {}, {}
        tree_oid = tree["oid"]
        if not isinstance(tree_oid, str) or not _OID.match(tree_oid):
            raise GitHubError(f"the workflow tree answered id {tree_oid!r}")
        entries = store.entries(tree_oid)
        if entries is None:
            listed = call(_ENTRIES_QUERY, {**variables, "tree": tree_oid})
            entries = _tree_entries(listed["repository"]["object"])
            store.put_entries(tree_oid, entries)
        documents: dict[str, object] = {}
        unreadable: dict[str, str] = {}
        unparsed: list[Entry] = []
        for entry in entries:
            parsed = store.parsed(entry.oid)
            if parsed is None:
                unparsed.append(entry)
            elif parsed[1] is not None:
                unreadable[entry.name] = parsed[1]
            else:
                documents[entry.name] = parsed[0]
        if unparsed:
            texts = {entry.oid: store.text(entry.oid) for entry in unparsed}
            missing = [oid for oid, text in texts.items() if text is None]
            if missing:
                texts.update(_fetch_texts(owner, name, missing, call))
            parser = executable()
            for entry in unparsed:
                text = texts.get(entry.oid)
                if text is None:
                    # Not cached: GitHub may return the whole text next time.
                    unreadable[entry.name] = "GitHub returned no complete text for it"
                    continue
                store.put_text(entry.oid, text)
                try:
                    document: object = workflow_files.parse(text, parser)
                except workflow_files.UnreadableWorkflow as exc:
                    store.put_parsed(entry.oid, None, str(exc))
                    unreadable[entry.name] = str(exc)
                    continue
                store.put_parsed(entry.oid, document, None)
                documents[entry.name] = document
    except (KeyError, TypeError) as exc:
        raise GitHubError(
            f"reading {repo}'s workflow files: an answer in an unexpected shape: {exc!r}"
        ) from exc
    if store.wrote:
        store.prune(now())
    return documents, unreadable
