"""python -m autonomy → continuous loop (default) or bootstrap via subcommand."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"bootstrap", "boot"}:
        from autonomy.bootstrap import main as boot_main

        return boot_main(sys.argv[2:])
    from autonomy.continuous import main as cont_main

    return cont_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
