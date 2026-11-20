"""sightqa · 鼠标指题答疑 —— 鼠标停在哪，答案念到哪。

启动：``python cursor_qa.py``（或 ``pip install .`` 后用 ``sightqa``）。
完整的参数说明见 ``cursorqa/cli.py`` 与 README。
"""

from __future__ import annotations

import sys

from cursorqa.cli import main

if __name__ == "__main__":
    sys.exit(main())
