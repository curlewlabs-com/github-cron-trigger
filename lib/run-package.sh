#!/bin/sh
# Every action's launcher: run the package beside this file with the command
# line given, on an import path of the package and the standard library alone.
# An action runs in the consumer's checkout, and `python3 -m` would put that
# directory first on the path, so a file there named like the package or a
# module it imports would run in its place. -I leaves out that directory, the
# user's site-packages and every PYTHON* variable, PYTHONPATH included, on every
# python3 the package supports; -P, which leaves out the directory alone, is
# missing from python3.10, which the oldest supported runner image ships. As
# PYTHONPATH is ignored, the program puts the package's directory on the path
# itself.
set -eu
src="$(dirname "$0")/../src"
exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from github_cron_trigger.__main__ import main; sys.exit(main())' "$src" "$@"
