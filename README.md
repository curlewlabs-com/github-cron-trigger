# github-cron-trigger

Run each scheduled slot of a GitHub Actions workflow on time and exactly once,
with GitHub's own `schedule` trigger kept as the backstop.

## Why

GitHub starts a `schedule` run when its queue reaches it, which can be hours
after the cron instant, and it drops some runs outright when the queue is deep.
Moving the cron minute off the hour does not help once the queue is backed up.

The trigger GitHub starts on demand is an API dispatch, so a clock outside
GitHub can deliver each slot on time. But then every slot arrives twice: once
from the clock, and once, late, from GitHub's own schedule. For many scheduled
jobs a second run is not harmless. A backup exports again, a staged rollout
climbs another step, a report files another issue.

This project has three parts:

- **The clock** (`tick`) runs on any host with a scheduler. Every few minutes it
  sends each enrolled workflow its newest due slot as a `repository_dispatch`.
- **The slot action** makes every delivery of a slot agree on which slot a run
  belongs to. It reads a ledger of finished slots, so a delivery that arrives
  after the slot is done skips the work.
- **The done action** records a slot in the ledger once its work has succeeded.

A workflow whose repeated runs are harmless (a reconciler) needs only the clock.

## Concepts

- **Slot**: one firing of one `cron:` line, meaning the line and the UTC instant
  it names. Every scheduled run belongs to exactly one slot, however late it
  starts.
- **Delivery**: a slot reaches a workflow through GitHub's `schedule` event, or
  through a `repository_dispatch` whose event type is
  `github-cron-trigger/<workflow file>` and whose `client_payload` names the
  line as `cron` and the instant as `slot` (`YYYY-MM-DDTHH:MMZ`).
- **Ledger**: one git ref per finished slot,
  `refs/github-cron-trigger/<workflow file>/<cron line>/<YYYYMMDDTHHMMZ>`. A
  ref outside `refs/heads` and `refs/tags` is not a branch or a tag; nothing
  lists it in the UI or fetches it by default. `slot_ledger.py` says why a ref
  is the record and how the line is written into it.
- **Mark**: a clock's claim of a slot it sends, also a git ref, under
  `refs/github-cron-trigger-clock/`. Marks are how several clocks share the
  work without sending a slot twice ("The clock").
- **Part**: a run whose legs are recorded separately (one per environment, say)
  records `<slot>.<part>` for each leg, so another delivery redoes only a leg
  that failed.

## Enrolling a workflow

Keep the `schedule:` trigger, add the dispatch trigger, and split the workflow
into a slot job, the work, and a record job:

```yaml
on:
  schedule:
    - cron: "30 19 * * *"
  repository_dispatch:
    types: [github-cron-trigger/backup.yml]
  workflow_dispatch:

# Deliveries of one slot can be in flight at once. A shared group runs them
# one after the other, and `queue: max` keeps a third arrival from cancelling
# the one already waiting.
concurrency:
  group: backup
  queue: max
  cancel-in-progress: false

permissions: {}

jobs:
  slot:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    outputs:
      scheduled: ${{ steps.slot.outputs.scheduled }}
      slot: ${{ steps.slot.outputs.slot }}
      cron: ${{ steps.slot.outputs.cron }}
      run: ${{ steps.slot.outputs.run }}
    steps:
      - id: slot
        uses: curlewlabs-com/github-cron-trigger/slot@v0

  work:
    needs: slot
    if: needs.slot.outputs.run == 'true'
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v7.0.1
      - run: ./back-up.sh

  record:
    needs: [slot, work]
    if: needs.slot.outputs.scheduled == 'true' && needs.work.result == 'success'
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: curlewlabs-com/github-cron-trigger/done@v0
        with:
          slot: ${{ needs.slot.outputs.slot }}
          cron: ${{ needs.slot.outputs.cron }}
```

A workflow is **enrolled** when it declares `repository_dispatch` with the type
`github-cron-trigger/<its own file name>`. A copied workflow keeps the old file
name in its type, so the clock refuses it rather than sending its slots under a
type it does not listen to.

**The concurrency group makes a slot run once.** The slot check and the record
are separate calls, so deliveries of one slot running side by side could each
find the slot owed and each do the work. Serialized, a later delivery starts
only after the earlier one has recorded, and it skips.

**Why a separate record job.** Recording needs `contents: write`, a token that
can push to the repository. In its own job, holding nothing else, that grant is
never in reach of the work's steps or of anything they install.

A workflow that also runs on pull requests, where a PR run only verifies or
dry-runs, can key that run by its run id so it never waits in the shared queue:

```yaml
concurrency:
  group:
    ${{ github.event_name == 'pull_request' && format('backup-pr-{0}',
    github.run_id) || 'backup' }}
  queue: max
  cancel-in-progress: false
```

GitHub reads `queue` only as a literal and refuses `queue: max` beside
`cancel-in-progress: true`, so one group cannot both queue deliveries and drop
a push's superseded run. A PR run that touches what the deliveries guard, such
as a plan that reads a live state lock, stays in the shared group instead.

### The slot action's outputs

- `scheduled`: `true` for a `schedule` or `repository_dispatch` delivery, or a
  `workflow_run` from one (see "Chained workflows"). Every other event (a manual
  dispatch, a pull request) gives `false`, and such a run behaves exactly as it
  did before enrollment.
- `slot` and `cron`: the instant and the line of the slot the run belongs to,
  empty when not scheduled.
- `run`: `false` only when the ledger already records the slot, so gating the
  work on it skips a second delivery's work and nothing else.

The action also writes one line to the job summary: the slot, which delivery
brought it, and whether this run does the work.

### Once a day per line

For a `schedule` delivery the slot is the latest instant of the cron line that
fired at or before the run starts, because GitHub's event names the line but
not the instant. That is exact only while GitHub's delay is shorter than the
line's period, and GitHub's delay runs to hours. So **each `cron:` line of a
workflow that uses the slot action must fire at most once a day**: a single
minute and a single hour. A `schedule` delivery of a line that fires more often
is refused rather than attributed to whichever of its instants happens to be
latest, and the clock reports such a workflow instead of delivering it. A
dispatched slot is always exact.

A workflow can carry several lines, each with its own slots. A weekly line and
a daily line that fire in the same minute are separate slots with separate
records.

### Recording in parts

A job whose legs succeed or fail independently, such as a matrix over
environments, can record each leg as a part. Pass the same `part` to both
actions:

```yaml
- id: slot
  uses: curlewlabs-com/github-cron-trigger/slot@v0
  with:
    part: ${{ matrix.environment }}
```

A later delivery then redoes only the legs whose part is missing.

## Enrolling a reconciler

A workflow that reads the current state on every run and changes only what is
out of date is harmless to run twice, so it needs no slot action, no ledger
and no record job. Declaring the dispatch type is the whole enrollment:

```yaml
on:
  schedule:
    - cron: "17 * * * *"
  repository_dispatch:
    types: [github-cron-trigger/release-watch.yml]
```

Without the slot action, a line may fire more than once a day. Each dispatch
names its exact slot, so nothing has to be attributed by time.

## Chained workflows

A workflow that runs after a scheduled one through `workflow_run`, rather than
on a schedule of its own, uses the same slot action unchanged. A `workflow_run`
run whose parent was a scheduled delivery (a `schedule` or
`repository_dispatch` run) belongs to the parent's slot: the latest instant of
the parent workflow's cron line at or before the parent run was created. Each
delivery of the parent's slot then starts a chained run that resolves to the
same slot, so the chained workflow's own records make the later one skip. A
parent that was not a scheduled delivery leaves the chained run unscheduled.

The parent must carry exactly one `cron:` line, because a run does not say
which line fired it, and that line must fire at most once a day, for the same
reason as a `schedule` delivery.

Re-delivering a chained slot through its parent works only while that slot is
still the parent line's newest, because the chained run takes the parent
line's newest slot whatever the parent was sent.

## The clock

`tick` is the delivering half, run on a short interval by any scheduler outside
GitHub, on one host or several:

```sh
python3 -m github_cron_trigger tick --repo owner/name --send
```

Each tick reads the workflow files of the repository's default branch through
the API, in a single GraphQL query, so the host needs no clone. It works out
every enrolled `cron:` line's latest due slot in UTC and sends it to the
workflow as a `repository_dispatch`: the newest due slot per line only, and
never a slot the ledger already records. A merged change to a workflow reaches
the clock at its next tick.

### What it remembers, and where

The clock keeps its memory in the repository it serves, not on its host. Before
it sends a slot it claims it, by creating a git ref:

```text
refs/github-cron-trigger-clock/<workflow file>/<cron line>/<YYYYMMDDTHHMMZ>
```

Creating a ref is atomic: when several clocks claim the same slot, exactly one
create succeeds, and only that clock sends. So:

- **Run it on as many hosts as you like.** Each slot is still sent once. While
  one host is asleep or down, another delivers on time.
- **A host keeps nothing.** One can join, leave or be rebuilt with nothing to
  copy, and what has been sent can be read from the repository.
- **A failed send releases its claim**, as does a failed ledger read before it,
  so the next tick, on any host, sends the slot again. A claim that GitHub
  applied but answered as failed or refused is still recognized as the clock's
  own: each claim's ref points at a note unique to it. A clock stopped between
  its claim and its send leaves that slot to GitHub's backstop: late, but still
  delivered.

These marks are not the ledger. The ledger records slots whose work finished,
and the slot action reads it; marks record slots a clock sent, for every
enrolled workflow, reconcilers included, and only clocks read them. A tick keeps
each line's two newest marks and removes the rest.

### How it behaves

- **Baselines.** A line no clock has marked is baselined - its latest slot is
  marked without sending - because that slot may have run before anything could
  record it. A workflow that stops being enrolled has its marks removed, so
  enrolling it again baselines it again.
- **Sleep.** After a gap with no clock up, the next tick sends one delivery per
  line, for the newest due slot, never a burst. Slots no clock was up for are
  left to GitHub's backstop: late, but still exactly once.
- **Dry run first.** Without `--send`, a tick reads everything and reports what
  it would do - baseline, send, or remove old marks - and writes nothing.
  Removing `--send` later is the off switch.
- **What an idle tick costs.** Two small requests: the id of the default
  branch's `.github/workflows` tree, which changes only when a workflow file
  does, and the clock's marks. Workflow files are downloaded and parsed only
  when they change, and then only the changed ones; everything read before is
  reused from a cache under `$XDG_CACHE_HOME/github-cron-trigger` (or
  `~/.cache/github-cron-trigger`), keyed by content id. The cache is only a
  cache: deleting it costs one full read and changes nothing the clock decides.
- **Exit status.** A tick that could not read a workflow, the marks or the
  ledger, or could not send a slot, logs the problem and exits 1, so the
  scheduler running it can report it.

### Hosting it

Any host that can reach `api.github.com` will do, and more than one is better.
Install a release, which puts `github-cron-trigger` on PATH:

```sh
pip install "github-cron-trigger @ git+https://github.com/curlewlabs-com/github-cron-trigger@v0.2.1"
```

Or check out a release tag and run it as `PYTHONPATH=src python3 -m
github_cron_trigger`. Then run `tick` every few minutes, once per repository
served. A crontab line:

```crontab
*/5 * * * * github-cron-trigger tick --repo owner/name --send >> "$HOME/github-cron-trigger.log" 2>&1
```

On macOS, a launchd agent with `StartInterval` 300 does the same. On Linux, a
systemd timer with `OnUnitActiveSec=5min` does.

Ticking every few minutes, rather than scheduling one entry per slot, is
deliberate. Slots are UTC instants, and a host scheduler's calendar entries are
local times, which shift against UTC at every daylight saving change.

### The clock's token

The clock calls the API through the `gh` CLI, so it uses whatever `gh` is
logged in as, or `GH_TOKEN` when that is set. A fine-grained personal access
token limited to the enrolled repositories needs:

- **Contents: Read and write.** Sending a `repository_dispatch` requires it,
  and so does creating and removing the clock's marks. Reading the workflow
  files, the marks and the ledger is the read half.
- **Metadata: Read**, which every fine-grained token carries.

## Missed slots

A dropped delivery leaves no run to go red, so a slot that neither the clock
nor GitHub's backstop completed is reported by nothing else. `missed` reads the
ledger against each line's due slots and names the gaps:

```sh
python3 -m github_cron_trigger missed --repo owner/name
```

It checks each workflow that runs the slot action, on the lines it is enrolled
for and, when it chains through `workflow_run`, on each enrolled parent's line.

- A slot counts as due once `GRACE` (12 hours) has passed since it, so one
  still in flight is not named.
- A line is checked only from its oldest record on, so slots from before it
  enrolled are not misses, and a line that has never recorded is not checked
  yet.
- A record of any part of the slot counts as delivered.
- The window reaches back `LOOKBACK` (7 days).

`freshness.py`'s header says why each bound is where it is.

A workflow whose records, schedule or parent's line cannot be read is reported
as not judged, never as missing every slot, and the run does not pass on it.
The exit status is a bit set, so neither finding hides the other: 1 when a due
slot has no record, 2 when anything could not be read, 0 when everything was
read and no slot was missed. Run it daily, from the clock's host or from a
scheduled workflow, and alert on a non-zero exit.

## Failure behavior

Every uncertain case fails the run rather than guessing, because a wrong guess
either does owed work twice or skips it silently:

- **The slot action cannot read the ledger or the workflow file, or a
  delivery's slot is missing or malformed.** The slot step fails and the work
  does not run. The other delivery of the slot still gets its turn.
- **A delivery's line is not one of the workflow's own `schedule:` entries, or
  its entry is one this tool refuses (see "Accepted cron grammar").** The slot
  step fails for every delivery of that line until the workflow file changes.
- **A dispatched slot is more than ten minutes in the future, or older than the
  ledger keeps records.** It is refused.
- **The record step cannot write.** The run fails and the slot stays
  unrecorded, so the other delivery does the work again. A visible red run and a
  repeat beat a slot silently marked done.
- **Pruning old records fails.** A warning only, because the record itself
  already landed.

The record step prunes its workflow's records older than 60 days
(`RECORD_RETENTION` in `steps.py`), a window far longer than any delivery delay
it has to absorb.

## Accepted cron grammar

GitHub's own: minute, hour, day of month, month (1-12 or JAN-DEC) and day of
week (0-6 or SUN-SAT). Each field is a `*`, a value, a range `a-b`, or a comma
list of those, and any of them can be stepped with `/n`. A step on a single
value runs to the end of the field, so `20/15` is minutes 20, 35 and 50. A line
is resolved exactly or refused, never approximated. `cron_slots.py` says why
each refusal is one:

- A line restricting day of month and day of week together, where neither is a
  bare `*`. Cron implementations disagree on how such a line fires, and GitHub
  does not say which way it goes.
- A line that can never fire, such as `0 0 30 2 *`.
- A `schedule:` entry that sets `timezone:`. Slots are UTC instants, and GitHub
  does not document when a zoned schedule fires across a daylight saving
  fall-back.
- Anything outside the grammar. That includes the `@daily`-style macros GitHub
  itself does not support; a day of week 7, which some crons read as Sunday but
  GitHub does not document; and a month or weekday name in lower or mixed case,
  where GitHub documents only upper case.

The clock leaves out a workflow that is not enrolled and has a refused line,
and reports an enrolled one that has one. The slot action fails a delivery of a
refused line, GitHub's own `schedule` run included.

GitHub does not run a schedule more often than its documented minimum
interval, and this tool deliberately does not hold a line to that floor. The
clock sends a line's newest due slot on each tick, so how often a reconciler on
an every-minute line runs is set by how often the clock ticks.

## Requirements

- **Runners** (the slot action): Linux or macOS, on x86_64 or arm64, with
  `python3` 3.10 or newer, the `gh` CLI, and
  [mikefarah's yq](https://github.com/mikefarah/yq) v4.53.4 or newer on PATH.
  The done action needs only `python3` and `gh`. GitHub's hosted runners have
  all three, and neither action needs a checkout.
- **yq, on a self-hosted runner.** Install it once, as you would any tool the
  runner uses: Homebrew's `yq` formula is mikefarah's, and the project's
  releases page has a binary for every platform. The apt package named `yq` on
  Debian and Ubuntu is a different program, and the slot action refuses it by
  name. `MINIMUM_VERSION` in `yq.py` says why the floor sits where it does.
- **The clock's hosts**: `python3`, `gh` and yq as above, a scheduler, and a
  token as described under "The clock's token". Nothing else; a host keeps no
  state.

## Versioning

Releases are tagged `vMAJOR.MINOR.PATCH`, and those tags never move. The
`vMAJOR` tag follows the newest release of that major version. Pin an action to
a full commit SHA, or to a fixed tag, for exact reproducibility.

These are the project's contract:

- the dispatch event type and payload keys;
- the ledger's ref layout;
- the actions' inputs and outputs;
- the command-line interface.

From 1.0.0 on, they stay compatible within a major version, so a clock and the
actions can be upgraded separately. Before 1.0.0, a minor release may change
them, and its release notes say how; pin a fixed tag if you need them to hold
still.

## Development

```sh
python3 -m unittest discover -s tests -t .   # the unit tests
ruff format --check . && ruff check . && mypy
```

`CONTRIBUTING.md` has the conventions. The live check against GitHub's API
(`tests/live_check.py`) runs in CI.

## License

MIT. See `LICENSE`.
