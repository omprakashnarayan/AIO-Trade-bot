from __future__ import annotations

from .db import Database
from .reporting import generate_report


def main() -> int:
    snapshot, markdown, path = generate_report(Database(), save=True)
    print(markdown)
    if path:
        print(f"\nSaved report to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
