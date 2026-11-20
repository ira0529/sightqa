"""调用 Windows PowerShell 的公共封装。

统一两件事：脚本一律用 UTF-16LE + base64 传（``-EncodedCommand``），
输出一律按 UTF-8 解码——这样中文、引号、路径都不会被命令行吃掉。
"""

from __future__ import annotations

import base64
import os
import subprocess

POWERSHELL = os.environ.get("CURSOR_QA_POWERSHELL", "powershell.exe")
IS_WINDOWS = os.name == "nt"


def encode_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_script(
    script: str,
    *,
    timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """执行一段 PowerShell，返回 (返回码, 标准输出, 标准错误)。"""
    if not IS_WINDOWS:
        raise RuntimeError("该功能依赖 Windows PowerShell")
    completed = subprocess.run(  # noqa: S603 - 固定调用 powershell.exe
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encode_command(script),
        ],
        capture_output=True,
        timeout=timeout,
        creationflags=creation_flags(),
        check=False,
        env=env,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    return completed.returncode, stdout, stderr
