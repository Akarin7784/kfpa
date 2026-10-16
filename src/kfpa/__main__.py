"""`python -m kfpa` 入口（docs/dev/p0-plan.md §2 / §3.8）。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
