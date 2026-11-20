"""图片转文字（OCR）——直接用 Windows 10/11 自带的 WinRT OCR 引擎。

不装任何第三方包、不联网，走 ``powershell.exe`` 调
``Windows.Media.Ocr``。识别结果带每行的矩形，用来判断鼠标指到哪一行，
再拼成“文本 + 指向标记”的提示词交给模型（这样纯文本模型也能答题）。

用法::

    ok, detail = ocr_support()
    result = recognize(image)          # PIL.Image
    prompt = compose_prompt(result, cursor=(x, y))
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .powershell import IS_WINDOWS, run_script

# 每行一条：文本 + 包围盒（像素）
_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
function Await($WinRtTask, $ResultType) {
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { throw 'NO_OCR_ENGINE' }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:CURSOR_QA_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$items = @()
foreach ($line in @($result.Lines)) {
  $words = @($line.Words)
  if ($words.Count -eq 0) { continue }
  $x1 = 1e9; $y1 = 1e9; $x2 = 0.0; $y2 = 0.0
  foreach ($w in $words) {
    $r = $w.BoundingRect
    if ($r.X -lt $x1) { $x1 = $r.X }
    if ($r.Y -lt $y1) { $y1 = $r.Y }
    if (($r.X + $r.Width) -gt $x2) { $x2 = $r.X + $r.Width }
    if (($r.Y + $r.Height) -gt $y2) { $y2 = $r.Y + $r.Height }
  }
  $items += [pscustomobject]@{
    t = [string]$line.Text
    x = [int]$x1
    y = [int]$y1
    w = [int]($x2 - $x1)
    h = [int]($y2 - $y1)
  }
}
$json = [pscustomobject]@{
  lang = [string]$engine.RecognizerLanguage.LanguageTag
  lines = @($items)
} | ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
"""

_SUPPORT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Output 'NONE'; exit 0 }
Write-Output ([string]$engine.RecognizerLanguage.LanguageTag)
"""

OCR_PROMPT = (
    "下面这段文字是鼠标附近截图的 OCR 识别结果，按行排列，"
    "其中以「▶」开头的那一行是鼠标指向的位置。\n"
    "只解答 ▶ 这一行对应的题目，可以结合它前后的题干与选项。"
    "OCR 结果可能存在多余空格、错字或漏字，请结合上下文自行理解。"
    "用适合朗读的简短中文回答：先给答案，再用一到两句话说明理由。"
    "不要复述题目，不要使用 Markdown 或代码块。"
    "如果 ▶ 这一行不是题目、或信息不足以作答，只回复 SKIP。\n\n"
    "--- OCR 文字开始 ---\n{body}\n--- OCR 文字结束 ---"
)

_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
_SPACE_BEFORE_CJK = re.compile(rf"\s+(?=[{_CJK}])")
_SPACE_AFTER_CJK = re.compile(rf"(?<=[{_CJK}])\s+")
_SPACES = re.compile(r"[ \t]+")

_support_lock = threading.Lock()
_support_cache: tuple[bool, str] | None = None


@dataclass(frozen=True)
class OcrLine:
    text: str
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...] = ()
    language: str = ""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def normalize_text(text: str) -> str:
    """Windows OCR 会把中文逐字拆开再用空格连接，这里收掉多余空格。"""
    cleaned = (text or "").replace("\u200b", "")
    cleaned = _SPACES.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_CJK.sub("", cleaned)
    cleaned = _SPACE_AFTER_CJK.sub("", cleaned)
    return cleaned.strip()


def nearest_line(lines: tuple[OcrLine, ...], cursor: tuple[int, int]) -> int | None:
    """找出离光标最近的一行（鼠标落在行内时距离为 0）。"""
    if not lines:
        return None
    x, y = cursor
    best_index, best_distance = 0, float("inf")
    for index, line in enumerate(lines):
        dx = max(line.left - x, 0, x - line.right)
        dy = max(line.top - y, 0, y - line.bottom)
        distance = (dx * dx + dy * dy) ** 0.5
        if distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def compose_prompt(result: OcrResult, cursor: tuple[int, int] | None = None) -> str:
    """把 OCR 结果拼成交给模型的提示词，标出鼠标指向的那一行。"""
    if not result.lines:
        raise ValueError("OCR 没有识别到文字")
    marked = nearest_line(result.lines, cursor) if cursor else 0
    body: list[str] = []
    for index, line in enumerate(result.lines):
        if not line.text:
            continue
        body.append(("▶ " if index == marked else "  ") + line.text)
    return OCR_PROMPT.format(body="\n".join(body))


def ocr_support(refresh: bool = False) -> tuple[bool, str]:
    """检测系统 OCR 是否可用，结果缓存。返回 (可用, 语言或原因)。"""
    global _support_cache
    if _support_cache is not None and not refresh:
        return _support_cache
    with _support_lock:
        if _support_cache is not None and not refresh:
            return _support_cache
        _support_cache = _probe_support()
        return _support_cache


def _probe_support() -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "系统 OCR 只支持 Windows 10/11"
    try:
        code, stdout, stderr = run_script(_SUPPORT_SCRIPT, timeout=15.0)
    except (OSError, RuntimeError) as error:
        return False, f"调用 PowerShell 失败：{error}"
    except Exception as error:  # noqa: BLE001 - 探测失败不该让界面崩溃
        return False, f"检测 OCR 失败：{error}"

    tag = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if code != 0:
        return False, (stderr.strip().splitlines() or ["系统 OCR 初始化失败"])[0][:160]
    if not tag or tag == "NONE":
        return False, "系统没有可用的 OCR 语言包（设置 → 时间和语言 → 语言 里添加中文）"
    return True, tag


def recognize(image: Image.Image, *, timeout: float = 25.0) -> OcrResult:
    """对一张 PIL 图片做 OCR，返回带坐标的行列表。"""
    if not IS_WINDOWS:
        raise RuntimeError("系统 OCR 只支持 Windows 10/11")
    if image.width < 8 or image.height < 8:
        raise RuntimeError("截图太小，无法识别文字")

    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    path = Path(name)
    try:
        image.convert("RGB").save(path, format="PNG")
        env = dict(os.environ)
        env["CURSOR_QA_OCR_IMAGE"] = str(path)
        try:
            code, stdout, stderr = run_script(_OCR_SCRIPT, timeout=timeout, env=env)
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"OCR 调用失败：{error}") from error
        if code != 0:
            detail = (stderr.strip().splitlines() or ["未知错误"])[0]
            if "NO_OCR_ENGINE" in detail:
                raise RuntimeError("系统 OCR 引擎不可用，请先安装中文语言包")
            raise RuntimeError(f"OCR 失败：{detail[:160]}")

        raw = stdout.strip().splitlines()
        if not raw:
            raise RuntimeError("OCR 没有返回结果")
        try:
            data = json.loads(base64.b64decode(raw[-1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError(f"OCR 结果无法解析：{error}") from error
        return _parse(data)
    finally:
        path.unlink(missing_ok=True)


def _parse(data: dict) -> OcrResult:
    lines: list[OcrLine] = []
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        text = normalize_text(str(item.get("t", "")))
        if not text:
            continue
        lines.append(
            OcrLine(
                text=text,
                left=int(item.get("x", 0) or 0),
                top=int(item.get("y", 0) or 0),
                width=int(item.get("w", 0) or 0),
                height=int(item.get("h", 0) or 0),
            )
        )
    lines.sort(key=lambda line: (line.top, line.left))
    return OcrResult(lines=tuple(lines), language=str(data.get("lang", "")))
