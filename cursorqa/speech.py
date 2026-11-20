"""Windows 本地朗读（SAPI）。

不引入第三方依赖：把 PowerShell 脚本用 UTF-16LE + base64 传给
``powershell.exe -EncodedCommand``，这样中文和引号都不会被命令行吃掉。
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass

from .config import SPEAK_RATE_MAX, SPEAK_RATE_MIN
from .powershell import IS_WINDOWS, POWERSHELL, creation_flags, encode_command, run_script

MAX_SPEECH_CHARS = 600
CHINESE_LCIDS = {"804", "404", "c04", "1004", "1404", "7804", "7c04", "20804"}
CHINESE_NAME_HINTS = (
    "chinese",
    "zh-cn",
    "zh_cn",
    "中文",
    "普通话",
    "huihui",
    "yaoyao",
    "kangkang",
    "xiaoxiao",
)

_SELECT_VOICE = (
    "$voice = New-Object -ComObject SAPI.SpVoice\n"
    "$chosen = $null\n"
    "if ('{voice_id}') {{\n"
    "  $chosen = $voice.GetVoices() | Where-Object {{ $_.Id -eq '{voice_id}' }} | Select-Object -First 1\n"
    "}}\n"
    "if (!$chosen) {{\n"
    "  foreach ($item in $voice.GetVoices()) {{\n"
    "    $lang = ''\n"
    "    try {{ $lang = [string]$item.GetAttribute('Language') }} catch {{ }}\n"
    "    $desc = ''\n"
    "    try {{ $desc = [string]$item.GetDescription() }} catch {{ }}\n"
    "    if ('{lcids}'.Contains(';' + $lang.ToLower() + ';')) {{ $chosen = $item; break }}\n"
    "    if ($desc -match '(?i)Chinese|zh-CN|Huihui|Yaoyao|Kangkang|Xiaoxiao') {{ $chosen = $item; break }}\n"
    "  }}\n"
    "}}\n"
)


@dataclass(frozen=True)
class VoiceInfo:
    available: bool
    name: str = ""
    voice_id: str = ""
    detail: str = ""


def prepare_for_speech(text: str) -> str:
    """去掉朗读时会念出来的 Markdown 符号，压缩空白。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"```.*?```", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"[*_`#>|~]+", "", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", cleaned)
    cleaned = re.sub(r"\s*\n+\s*", "。", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"。{2,}", "。", cleaned)
    cleaned = cleaned.strip(" 。")
    if len(cleaned) > MAX_SPEECH_CHARS:
        cleaned = cleaned[:MAX_SPEECH_CHARS].rstrip() + "……"
    return cleaned


def run_powershell(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """执行一段 PowerShell，返回 (返回码, 标准输出, 标准错误)。"""
    return run_script(script, timeout=timeout)


def detect_voice() -> VoiceInfo:
    """列出系统语音，挑一个中文的。找不到就返回不可用。"""
    if not IS_WINDOWS:
        return VoiceInfo(False, detail="当前系统不是 Windows，朗读功能不可用")

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  $voice = New-Object -ComObject SAPI.SpVoice\n"
        "} catch { Write-Error 'SAPI 不可用'; exit 3 }\n"
        "foreach ($item in $voice.GetVoices()) {\n"
        "  $lang = ''\n"
        "  try { $lang = [string]$item.GetAttribute('Language') } catch { }\n"
        "  $desc = [string]$item.GetDescription()\n"
        "  Write-Output ('{0}|{1}|{2}' -f $item.Id, $lang, $desc)\n"
        "}\n"
    )
    try:
        code, stdout, stderr = run_powershell(script, timeout=15.0)
    except (OSError, subprocess.SubprocessError) as error:
        return VoiceInfo(False, detail=f"调用 PowerShell 失败：{error}")

    if code != 0:
        return VoiceInfo(False, detail=stderr.strip()[:160] or "SAPI 初始化失败")

    voices: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3:
            voices.append((parts[0], parts[1].strip().lower(), parts[2].strip()))

    for voice_id, lang, desc in voices:
        if lang and lang.split(";")[0] in CHINESE_LCIDS:
            return VoiceInfo(True, name=desc, voice_id=voice_id)
    for voice_id, _lang, desc in voices:
        lowered = desc.lower()
        if any(hint.lower() in lowered for hint in CHINESE_NAME_HINTS):
            return VoiceInfo(True, name=desc, voice_id=voice_id)
    if voices:
        return VoiceInfo(
            False,
            detail="系统里没有中文语音，可在 Windows 设置 → 时间和语言 → 语音 里添加",
        )
    return VoiceInfo(False, detail="系统没有安装任何 SAPI 语音")


class Speaker:
    """把朗读包装成后台进程，方便界面轮询、随时打断。"""

    def __init__(self, voice_id: str = "", rate: int = 0) -> None:
        self.voice_id = voice_id or ""
        self.rate = max(SPEAK_RATE_MIN, min(SPEAK_RATE_MAX, int(rate)))
        self._process: subprocess.Popen[bytes] | None = None
        self._error = ""

    def busy(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def speak(self, text: str) -> None:
        payload = prepare_for_speech(text)
        if not payload:
            raise RuntimeError("没有可朗读的内容")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "try { $voice = New-Object -ComObject SAPI.SpVoice }\n"
            "catch { Write-Error 'SAPI 不可用'; exit 3 }\n"
            + _SELECT_VOICE.format(
                voice_id=self.voice_id.replace("'", "''"),
                lcids=";" + ";".join(sorted(CHINESE_LCIDS)) + ";",
            )
            + "if ($chosen) { $voice.Voice = $chosen }\n"
            f"$voice.Rate = {self.rate}\n"
            "try {\n"
            f"  $voice.Speak([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(payload)}')))\n"
            "} catch { Write-Error ('朗读失败: ' + $_.Exception.Message); exit 4 }\n"
        )
        try:
            self._process = subprocess.Popen(  # noqa: S603 - 固定调用 powershell.exe
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encode_command(script),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creation_flags(),
            )
        except OSError as error:
            raise RuntimeError(f"无法启动 Windows 语音：{error}") from error
        self._error = ""

    def poll(self) -> int | None:
        """进程结束时返回返回码，并收集 stderr 供界面提示。"""
        if self._process is None:
            return None
        code = self._process.poll()
        if code is None:
            return None
        stream = self._process.stderr
        if stream is not None:
            try:
                self._error = stream.read().decode("utf-8", "replace").strip()
            except OSError:
                self._error = ""
            finally:
                stream.close()
        self._process = None
        return code

    def last_error(self) -> str:
        return self._error

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
