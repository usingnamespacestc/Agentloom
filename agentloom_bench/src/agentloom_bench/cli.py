"""CLI entry point. PR 1 ships a placeholder; runner lands in PR 4."""
import sys


def main() -> int:
    print(
        "agentloom-bench: runner not implemented yet — see "
        "docs/design-tau-bench-integration.md PR 4 for the planned CLI surface.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
