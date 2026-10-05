# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately via
[GitHub's security advisory feature](https://github.com/curlewlabs-com/github-cron-trigger/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

The actions run inside a user's workflow with that workflow's token, and the
clock holds a token that can dispatch to the repositories it serves. The
security surface is:

- **The record token.** The done action creates git refs, so its job holds
  `contents: write`. It writes only under `refs/github-cron-trigger/`: the
  workflow file name and the part become ref path segments, and each is
  matched against a narrow pattern first, so a name cannot address another
  workflow's records or anything outside the namespace. The README recommends a
  separate record job, so that grant is never in reach of the work's steps.
- **Dispatch payloads.** Anyone who can send a `repository_dispatch` to the
  repository can already write to it, but the slot action still treats the
  payload as untrusted. The line must be one of the workflow's own UTC
  `schedule:` entries, the slot must be one of that line's instants, and a slot
  far in the future or older than the ledger's retention is refused. Payload
  values reach the tool as environment variables, never interpolated into a
  shell script.
- **Paths and commits from the event.** A chained run reads its parent's
  workflow file at a path and commit taken from the event. The path must name a
  file directly in `.github/workflows/`, and the commit must be a full object
  name, before either becomes part of an API request.
- **The yq it runs.** The tool runs the `yq` on the runner's or host's PATH,
  which it trusts as it trusts `python3` and `gh`, and downloads nothing. It
  first checks that the version line is mikefarah's at the minimum version, so
  a different program called `yq` is never handed arguments. Workflow text
  reaches yq on standard input, never as a path taken from the event.
- **The clock's token, and what it writes.** The clock uses whatever `gh` is
  logged in as, or `GH_TOKEN`. The README lists the narrowest fine-grained
  grant it needs. Besides dispatches, it writes only its marks: refs under
  `refs/github-cron-trigger-clock/`, named from a workflow file name, a cron
  line and a slot, each matched against a narrow pattern first. A mark's blob
  names the slot and nothing about the host, since anyone can read a public
  repository's refs.
