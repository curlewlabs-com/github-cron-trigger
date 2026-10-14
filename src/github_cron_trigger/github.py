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
# GitHub ends any request that takes it more than 10 seconds to process, and
# these calls are single requests or a page or two, so a healthy call finishes
# well inside this. It is spent once per attempt, which makes it the larger part
# of how long a failing call can take; a caller sizing a job timeout around a
# call counts every attempt RETRY_DELAYS_SECONDS and RATE_LIMIT_DELAY_SECONDS
# allow.
GH_TIMEOUT_SECONDS = 30

# Waits between attempts on a busy or unreachable API, one attempt per entry
# plus a final one. Most such failures are a single lost answer that the next
# attempt gets, so the first waits are short; they level off so that an outage
# still fails the call in under a minute of waiting.
RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 10, 10, 10)

# The wait before the one retry of a rate-limited call: GitHub's documented
# minimum wait after a secondary rate limit. A shorter wait spends the retry
# with no chance of clearing the limit, and GitHub warns that requests sent
# while limited can get an integration banned.
RATE_LIMIT_DELAY_SECONDS = 60

# `gh api`'s stderr line for a failed HTTP request: the error body's message
# and the status, or the status alone when the body held no message gh could
# read, as a 5xx with an empty or non-JSON body does. The response body goes to
# stdout and often carries the status too, but not every error body does, so
# the stderr line is what classification reads.
_GH_API_ERROR = re.compile(
    r"^gh: (?:(?P<message>.*) \(HTTP (?P<status>\d{3})\)|HTTP (?P<bare>\d{3}))$",
    re.MULTILINE,
)

# The failures that carry no status, because `gh` got no answer. A failed name
# lookup prints the first. Any other request left unanswered - a dial or TLS
# handshake that timed out, a reset connection, a proxy that refused - prints
# Go's error for it, which opens with the method and the quoted URL. The shape
# is matched rather than a list of causes, which Go words many ways; a cause no
# retry clears, such as a refused certificate, costs only the ladder's waits
# before the same failure.
_CONNECT_FAILURE = "error connecting to "
_NO_ANSWER = re.compile(r'^(?:Get|Head|Post|Put|Patch|Delete) "[^"]*": ')

# What gh prints when the answer it read was empty or cut off: Go's JSON decoder
# ran out of input. gh decodes an answer before it reports a status whenever it
# filters one with `--jq`, reads a GraphQL answer, or reads a JSON error body, so
# this carries no status, and the request may have applied, as with one that got
# no answer at all.
_CUT_OFF_ANSWER = "unexpected end of JSON input"

# GitHub answers a secondary rate limit with a 403 whose message says so. Every
# other 403 is a refusal, so the message is the only way to tell them apart.
_SECONDARY_RATE_LIMIT = "secondary rate limit"

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class GitHubError(Exception):
    """A GitHub API call failed in a way the caller must not paper over."""


class HttpError(GitHubError):
    """GitHub answered with a non-success status after any retries."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status
        self.message = message


def classify(stderr: str) -> tuple[int | None, str]:
    """The HTTP status and message of a failed `gh api` call, from its stderr.

    The status is None when `gh` reported none (a connection failure, an
    answer it could not decode, or a failure before any request); the message
    is then the whole stderr. A status reported without a message has an empty
    one.
    """
    text = stderr.strip()
    match = _GH_API_ERROR.search(text)
    if match is None:
        return None, text
    status = match.group("status") or match.group("bare")
    return int(status), match.group("message") or ""


def transient(status: int | None, message: str) -> bool:
    """Whether a failure is worth retrying soon: the API was busy or
    unreachable, or its answer was lost."""
    if status is None:
        return (
            message.startswith(_CONNECT_FAILURE)
            or bool(_NO_ANSWER.match(message))
            or message == _CUT_OFF_ANSWER
        )
    return status >= 500


def rate_limited(status: int | None, message: str) -> bool:
    """Whether GitHub refused a call for its rate limit, which waiting can clear."""
    if status == 429:
        return True
    return status == 403 and _SECONDARY_RATE_LIMIT in message.lower()


def _run_gh(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        # gh writes UTF-8 whatever the host's locale, and a runner's locale is
        # often C, which would refuse a workflow file's non-ASCII text.
        encoding="utf-8",
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )


def gh_api(
    args: Sequence[str],
    sleep: Callable[[float], None] = time.sleep,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run_gh,
) -> str:
    """Run `gh api` with the given arguments and return its stdout.

    Retries a busy or unreachable API on RETRY_DELAYS_SECONDS, and a rate limit
    once, after RATE_LIMIT_DELAY_SECONDS; any other failure, or one that
    outlasts its retries, raises. A retried POST can find its own earlier
    attempt already applied - slot_ledger.record treats the resulting 422 as
    success for exactly that reason.
    """
    command = ["gh", "api", *args]
    busy_waits = iter(RETRY_DELAYS_SECONDS)
    rate_limit_waits = iter((RATE_LIMIT_DELAY_SECONDS,))
    while True:
        try:
            completed = run(command)
        except subprocess.TimeoutExpired:
            delay = next(busy_waits, None)
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
        if rate_limited(status, message):
            delay = next(rate_limit_waits, None)
        elif transient(status, message):
            delay = next(busy_waits, None)
        else:
            delay = None
        if delay is None:
            if status is None:
                raise GitHubError(f"`{' '.join(command)}` failed: {message}")
            raise HttpError(status, message)
        sleep(delay)


def check_repo(repo: str) -> str:
    """`repo` itself, once it is known to be in the form owner/name: it becomes
    part of every API path, where a '/' or '..' would address another one."""
    if not _REPO.match(repo):
        raise ValueError(f"repository {repo!r} is not in the form owner/name")
    return repo
