# Contributing

Thanks for your interest in improving `github-cron-trigger`. The bar for a
change is whether it keeps every scheduled slot running on time and exactly
once. Bug fixes, sharper refusals of input the tool cannot resolve exactly,
support for another host or runner, and clearer docs are all welcome.

## Before a large change

Open an issue describing the problem first. The slot rules carry the
exactly-once guarantee: which delivery a run is, how a late `schedule` run is
attributed, when the ledger is read and written, and what is refused. A short
discussion up front saves rework.

## Running the checks

These are the checks CI runs on every push and pull request:

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy
```

The tests parse real YAML with the yq on PATH, which must be mikefarah's at the
version the README's "Requirements" names; Homebrew's `yq` is it. CI also
runs `tests/live_check.py` against GitHub's API, with a token that can write
refs; it cleans up everything it writes.

## Conventions

- **Tests ship with the change.** A bug fix or a feature includes a test in the
  same pull request. A test that guards a failure mode says which one.
- **Real inputs over hand-built ones.** Workflow files in tests are YAML text
  parsed by the real parser, not dictionaries shaped like what a parser might
  return. Only the network calls are replaced.
- **No clock in the tests.** Code that decides on "now" takes it as an
  argument, and tests pass hardcoded UTC instants, including ones whose local
  date differs from their UTC date.
- **Fail closed.** A case the tool cannot resolve exactly fails the run with a
  message. It never guesses, because a wrong guess does owed work twice or
  skips it silently.
- **Standard library only at runtime.** The actions run with a token that can
  write to the user's repository, so the code they run stays small and has no
  dependencies to install. Beyond `python3` and `gh`, the one tool it runs is
  the host's yq, and nothing downloads it.
- **Pin dependencies exactly.** Action references and the tools in
  `requirements-dev.txt` are exact versions, so a resolver cannot move them.
- **Comments explain why, not what.**
- **Keep tracked text ASCII.** Use plain hyphens and words instead of Unicode
  punctuation or decorative symbols.

## Compatibility

The dispatch event type and payload keys, the ledger's ref layout, the actions'
inputs and outputs, and the command-line interface are the contract. A clock
and the actions in a repository are upgraded separately, and a ledger outlives
any one release, so from 1.0.0 on a change to any of them is a major version.
Before 1.0.0 it is a minor version, and its release notes say how to move.

## Submitting

Keep each pull request focused on one change, make sure the checks pass, and
describe the why in the pull request body. CI must be green before merge.

## Releasing

Every release gets a fixed `vMAJOR.MINOR.PATCH` tag. The repository blocks
updates and deletions of those tags, and publishing the GitHub Release makes
the tag and its assets immutable. The floating `vMAJOR` tag is moved to each new
release in that major series, so workflows on `@v0` get fixes. Never attach a
release to the floating tag, because immutability would stop the next move.

1. Bump `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and push the tag.
3. Move `vMAJOR` to the same commit and push it.
4. Publish the fixed tag: `gh release create vMAJOR.MINOR.PATCH`.
