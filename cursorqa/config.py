"""配置模型、校验与持久化。

这里只做纯数据处理，不碰 tkinter，也不碰网络，方便单元测试。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

APP_NAME = "cursor-qa"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

INTERVAL_MIN = 1
INTERVAL_MAX = 3600
INTERVAL_DEFAULT = 10

# 截图范围预设：名称 -> (宽, 高)
REGIONS: dict[str, tuple[int, int]] = {
    "compact": (400, 260),
    "standard": (550, 350),
    "wide": (760, 480),
}
REGION_LABELS: dict[str, str] = {
    "compact": "紧凑 · 400×260",
    "standard": "标准 · 550×350",
    "wide": "宽屏 · 760×480",
}
REGION_DEFAULT = "standard"

# 语音语速（SAPI 的 -10…10，0 为正常）
SPEAK_RATE_MIN = -10
SPEAK_RATE_MAX = 10
SPEAK_RATE_DEFAULT = 0

KEY_ENV_VARS = ("CURSOR_QA_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def default_config_path() -> Path:
    """配置文件位置：Windows 用 %APPDATA%，其它平台用 ~/.config。"""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME / "config.json"
    return Path.home() / ".config" / APP_NAME / "config.json"


@dataclass(frozen=True)
class Settings:
    """界面上的全部可持久化设置。``api_key`` 只有在记住时才写盘。"""

    interval: int = INTERVAL_DEFAULT
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    region: str = REGION_DEFAULT
    speak: bool = True
    speak_rate: int = SPEAK_RATE_DEFAULT
    hide_window: bool = False
    ocr_mode: bool = False
    auto_full: bool = True
    remember_key: bool = False
    api_key: str = ""

    def with_defaults(self) -> Settings:
        """补齐空字段，越界值回落到默认值。"""
        interval = self.interval if INTERVAL_MIN <= self.interval <= INTERVAL_MAX else INTERVAL_DEFAULT
        region = self.region if self.region in REGIONS else REGION_DEFAULT
        base_url = self.base_url.strip() or DEFAULT_BASE_URL
        model = self.model.strip() or DEFAULT_MODEL
        rate = max(SPEAK_RATE_MIN, min(SPEAK_RATE_MAX, int(self.speak_rate)))
        return replace(
            self,
            interval=interval,
            region=region,
            base_url=base_url,
            model=model,
            speak_rate=rate,
            api_key=self.api_key.strip(),
        )

    def for_disk(self) -> Settings:
        """写盘版本：未勾选“记住”时抹掉密钥。"""
        if self.remember_key:
            return self.with_defaults()
        return replace(self.with_defaults(), api_key="")

    def region_size(self) -> tuple[int, int]:
        return REGIONS.get(self.region, REGIONS[REGION_DEFAULT])


def parse_interval(text: str) -> int:
    """把界面上的间隔文本转成秒数，失败时抛出 ``ValueError``（中文原因）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError(f"请填写截图间隔（{INTERVAL_MIN}–{INTERVAL_MAX} 秒）")
    if not raw.isdigit():
        raise ValueError(f"截图间隔必须是整数秒（{INTERVAL_MIN}–{INTERVAL_MAX}）")
    value = int(raw)
    if not INTERVAL_MIN <= value <= INTERVAL_MAX:
        raise ValueError(f"截图间隔需在 {INTERVAL_MIN}–{INTERVAL_MAX} 秒之间，当前为 {value}")
    return value


def api_key_from_env() -> str:
    """按优先级读取环境变量中的密钥。"""
    for name in KEY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _coerce_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def from_mapping(data: dict[str, object]) -> Settings:
    """宽松解析：坏字段跳过，不因为一行脏数据整份配置失效。"""
    base = Settings()
    model = data.get("model")
    base_url = data.get("base_url")
    region = data.get("region")
    api_key = data.get("api_key")
    return Settings(
        interval=_coerce_int(data.get("interval"), base.interval),
        base_url=base_url if isinstance(base_url, str) else base.base_url,
        model=model if isinstance(model, str) else base.model,
        region=region if isinstance(region, str) else base.region,
        speak=_coerce_bool(data.get("speak"), base.speak),
        speak_rate=_coerce_int(data.get("speak_rate"), base.speak_rate),
        hide_window=_coerce_bool(data.get("hide_window"), base.hide_window),
        ocr_mode=_coerce_bool(data.get("ocr_mode"), base.ocr_mode),
        auto_full=_coerce_bool(data.get("auto_full"), base.auto_full),
        remember_key=_coerce_bool(data.get("remember_key"), base.remember_key),
        api_key=api_key if isinstance(api_key, str) else base.api_key,
    ).with_defaults()


def load(path: Path | None = None) -> Settings:
    """读取配置；文件不存在或损坏时返回默认值，绝不抛异常。"""
    target = path or default_config_path()
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return Settings().with_defaults()
    if not isinstance(data, dict):
        return Settings().with_defaults()
    return from_mapping(data)


def save(settings: Settings, path: Path | None = None) -> Path:
    """原子写入配置；失败时抛出 ``OSError``，由调用方决定是否提示。"""
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings.for_disk()), ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temp_name, target)
    except OSError:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return target
