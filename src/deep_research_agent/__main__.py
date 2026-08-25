"""``python -m deep_research_agent <command>``.

A hand-rolled dispatcher rather than argparse subparsers, for one reason:
each command has to keep working on its own
(``python -m deep_research_agent.cli.ask``), which means each owns a complete
parser already. Subparsers would need those options declared twice, and the
copy that drifts is the one nobody runs.

So this peels off the command name and hands the rest of argv to that
module's ``main``. Imports are function-local so ``check`` does not pay for
loading the A2A stack, which matters when the thing you are checking is why
startup fails.
"""

from __future__ import annotations

import sys

COMMANDS = {
    "ask": "Research any question. No domain -- the face-to-user path.",
    "report": "Run the supply-chain sweep. What the schedule runs.",
    "serve": "A2A server + scheduler, in one process.",
    "check": "Verify configuration before spending tokens. Exits non-zero on problems.",
}

def usage() -> str:
    # `python -m ...` leaves argv[0] as the package's __main__ path; the
    # console script leaves the script name. Echoing back whichever the user
    # actually typed beats hardcoding one and being wrong for the other half.
    invoked = "python -m deep_research_agent"
    if sys.argv and not sys.argv[0].endswith("__main__.py"):
        invoked = sys.argv[0].rsplit("/", 1)[-1] or invoked
    return (
        f"usage: {invoked} <command> [options]\n\ncommands:\n"
        + "\n".join(f"  {name:<8} {help}" for name, help in COMMANDS.items())
        + "\n\nRun a command with --help for its options."
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0

    command, rest = argv[0], argv[1:]
    if command not in COMMANDS:
        print(f"unknown command {command!r}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2

    from importlib import import_module

    module = import_module(f"deep_research_agent.cli.{command}")
    return module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
