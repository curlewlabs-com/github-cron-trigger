"""`python3 -m github_cron_trigger <command> ...`, or `github-cron-trigger` when
installed. Each command parses its own arguments; `<command> --help` lists them.
"""

import sys
from collections.abc import Callable, Sequence

from . import clock, freshness, steps

COMMANDS: dict[str, tuple[Callable[[Sequence[str]], int], str]] = {
    "slot": (steps.slot_main, "resolve a run's slot and whether it is owed"),
    "done": (steps.done_main, "record a run's slot done"),
    "tick": (clock.main, "deliver every enrolled workflow's due slots once"),
    "missed": (freshness.main, "name the due slots no delivery recorded done"),
}


def _usage() -> str:
    lines = ["usage: github-cron-trigger <command> [options]", "", "commands:"]
    lines += [f"  {name:<8}{summary}" for name, (_, summary) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        print(_usage())
        return 0 if arguments else 2
    command = COMMANDS.get(arguments[0])
    if command is None:
        print(f"unknown command {arguments[0]!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    return command[0](arguments[1:])


if __name__ == "__main__":
    sys.exit(main())
