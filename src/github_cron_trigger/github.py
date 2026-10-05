"""Calls to the GitHub API, through the `gh` CLI.

`gh` carries the token whichever way it arrives - a workflow step's GH_TOKEN,
or an operator's own `gh auth` login on a clock host - and every call here
retries a busy or unreachable API before giving up, because each caller turns a
failure into a red run or a skipped tick.
"""

import re
import subprocess
import time
from collections.abc import Callable, Sequence

# Bound on every `gh` call, so a stalled socket cannot hold a workflow step or an
# unattended clock tick open until an outer timeout kills it without a report.
GH_TIMEOUT_SECONDS = 60

# Waits between attempts on a transient failure, one attempt per entry plus a
# final one. The first two ride out a gateway blip; the last is 60s because that
# is GitHub's documented minimum wait after a secondary rate limit, so a shorter
# final wait would spend the attempt with no chance of clearing it.
RETRY_DELAYS_SECONDS = (5, 20, 60)

# `gh api`'s stderr line for a failed HTTP request. The response body goes to
# stdout and often carries the status too, but not every error body does, so
# the stderr line is what classification reads.
_GH_API_ERROR = re.compile(
    r"^gh: (?P<message>.*) \(HTTP (?P<status>\d{3})\)$", re.MULTILINE
)

# The failure that carries no status: `gh` never reached the host.
_CONNECT_FAILURE = "error connecting to "

# GitHub answers a secondary rate limit with a 403 whose message says so. Every
# other 403 is a refusal, so the message is the only way to tell them apart.
_SECONDARY_RATE_LIMIT = "secondary rate limit"

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class GitHubError(Exception):
    """A GitHub API call failed in a way the caller must not paper over."""


class HttpError(GitHubError):
    """GitHub answered with a non-success status after any retries."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def classify(stderr: str) -> tuple[int | None, str]:
    """The HTTP status and message of a failed `gh api` call, from its stderr.

    The status is None when `gh` reported no HTTP exchange (a connection
    failure); the message is then the whole stderr.
    """
    text = stderr.strip()
    match = _GH_API_ERROR.search(text)
    if match is None:
        return None, text
    return int(match.group("status")), match.group("message")


def transient(status: int | None, message: str) -> bool:
    """Whether a failure is worth retrying: the API was busy or unreachable."""
    if status is None:
        return message.startswith(_CONNECT_FAILURE)
    if status >= 500 or status == 429:
        return True
    return status == 403 and _SECONDARY_RATE_LIMIT in message.lower()


def gh_api(args: Sequence[str], sleep: Callable[[float], None] = time.sleep) -> str:
    """Run `gh api` with the given arguments and return its stdout.

    Retries a transient failure on RETRY_DELAYS_SECONDS; any other failure, or a
    transient one that outlasts the ladder, raises. A retried POST can find its
    own earlier attempt already applied - slot_ledger.record treats the
    resulting 422 as success for exactly that reason.
    """
    command = ["gh", "api", *args]
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                # gh writes UTF-8 whatever the host's locale, and a runner's
                # locale is often C, which would refuse a workflow file's
                # non-ASCII text.
                encoding="utf-8",
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if delay is None:
                raise GitHubError(
                    f"`{' '.join(command)}` timed out after {GH_TIMEOUT_SECONDS}s"
                ) from None
            sleep(delay)
            continue
        except FileNotFoundError:
            raise GitHubError("the `gh` CLI is not on PATH") from None
        if completed.returncode == 0:
            return completed.stdout
        status, message = classify(completed.stderr)
        if delay is None or not transient(status, message):
            if status is None:
                raise GitHubError(f"`{' '.join(command)}` failed: {message}")
            raise HttpError(status, message)
        sleep(delay)
    raise AssertionError("unreachable: the retry loop returns or raises")


def check_repo(repo: str) -> str:
    """`repo` itself, once it is known to be in the form owner/name: it becomes
    part of every API path, where a '/' or '..' would address another one."""
    if not _REPO.match(repo):
        raise ValueError(f"repository {repo!r} is not in the form owner/name")
    return repo
