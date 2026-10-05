"""Run each scheduled slot of a GitHub Actions workflow on time and exactly once,
with GitHub's own `schedule` trigger kept as the backstop. See the README."""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than a syntax error from a newer form.
if sys.version_info < (3, 10):  # noqa: UP036 - the point is to run on older ones
    sys.exit("github-cron-trigger needs Python 3.10 or newer")
