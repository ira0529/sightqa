"""多模态模型识别。

这个工具的核心前提是：**模型必须能读图**。纯文本模型不会报错，只会
一本正经地瞎编，所以启动前先把模型名判一遍，再给出明确提示。
"""

from __future__ import annotations

from dataclasses import dataclass

OK = "ok"
UNKNOWN = "unknown"
TEXT_ONLY = "text-only"
EMPTY = "empty"


@dataclass(frozen=True)
class Preset:
    model: str
    label: str
    multimodal: bool


@dataclass(frozen=True)
class ModelVerdict:
    level: str
    message: str

    @property
    def is_ok(self) -> bool:
        return self.level == OK

    @property
    def is_blocking(self) -> bool:
        return self.level in (EMPTY, TEXT_ONLY)


# 界面上提供的候选项；用户可以自由输入别的模型名。
PRESETS: tuple[Preset, ...] = (
    Preset("deepseek-flash", "DeepSeek 多模态", True),
    Preset("gpt-4o", "OpenAI 多模态", True),
    Preset("gpt-4o-mini", "OpenAI 多模态 · 便宜", True),
    Preset("qwen-vl-max", "通义千问视觉", True),
    Preset("glm-4v", "智谱视觉", True),
    Preset("deepseek-chat", "DeepSeek 文本模型 · 不支持图片", False),
    Preset("deepseek-reasoner", "DeepSeek 推理模型 · 不支持图片", False),
)

# 已知的纯文本模型前缀（命中即判定为读不到图片）。
TEXT_ONLY_PREFIXES: tuple[str, ...] = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-coder",
    "deepseek-v3",
    "deepseek-r1",
    "deepseek-v2",
    "gpt-3.5",
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
    "qwen-long",
    "glm-4-flash",
    "glm-4-air",
    "llama",
    "mistral",
    "mixtral",
    "phi-3",
    "command-r",
    "claude-2",
    "gemini-1.0-pro",
    "ernie-3.5",
    "o1-mini",
    "o3-mini",
)

# 名称里出现这些片段，基本可以认为支持图片输入。
MULTIMODAL_HINTS: tuple[str, ...] = (
    "vl",
    "vision",
    "omni",
    "multimodal",
    "multi-modal",
    "image",
    "4o",
    "4.1",
    "4.5",
    "5v",
    "flash",
    "gemini",
    "claude-3",
    "claude-4",
    "sonnet",
    "opus",
    "haiku",
    "internvl",
    "llava",
    "minicpm-v",
    "step-1v",
    "glm-4v",
    "doubao",
)

SUGGESTION = "deepseek-flash、gpt-4o、qwen-vl-max、glm-4v"


def preset_models() -> list[str]:
    """下拉框候选项。"""
    return [preset.model for preset in PRESETS]


def preset_label(model: str) -> str:
    for preset in PRESETS:
        if preset.model == model:
            return preset.label
    return "自定义模型"


def is_known_multimodal(model: str) -> bool:
    return check_model(model).is_ok


def check_model(name: str, require_vision: bool = True) -> ModelVerdict:
    """判断模型名是否像多模态模型。

    规则很朴素：先排除已知纯文本模型，再看名称里有没有视觉相关片段。
    判不准的时候给 ``unknown``，提示用户先点“测试连接”实测。

    ``require_vision=False``（OCR 模式）时只看模型名是否为空——识别成文字后，
    纯文本模型也能作答。
    """
    model = (name or "").strip()
    if not model:
        if require_vision:
            return ModelVerdict(EMPTY, "请填写支持图片输入的多模态模型。")
        return ModelVerdict(EMPTY, "请填写模型名称。")
    if not require_vision:
        return ModelVerdict(OK, "✓ OCR 模式：模型只需能读文字，纯文本模型也可以。")

    low = model.lower()
    for prefix in TEXT_ONLY_PREFIXES:
        if low.startswith(prefix):
            return ModelVerdict(
                TEXT_ONLY,
                f"「{model}」是纯文本模型，读不到截图。请换成多模态模型，例如 {SUGGESTION}。",
            )

    for hint in MULTIMODAL_HINTS:
        if hint in low:
            return ModelVerdict(OK, f"✓ 已识别为多模态模型：{preset_label(model)}。")

    return ModelVerdict(
        UNKNOWN,
        "？无法确认该模型能否读图，请先点“测试连接”，确认返回 OK 再开始。",
    )
