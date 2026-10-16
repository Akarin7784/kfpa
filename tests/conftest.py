"""pytest 共享夹具：把仓库 src/ 挂到 sys.path，使 `import kfpa` 可用。

镜鉴（p0/verify）维护；不依赖 pytest 配置，说明：P0 阶段 pyproject.toml 由磐石维护，
这里不越界改它，所以用 conftest 注入路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
