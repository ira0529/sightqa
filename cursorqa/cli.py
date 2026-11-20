"""命令行入口：参数解析、``--check`` 模式、启动界面。

既可以 ``python cursor_qa.py``，也可以 ``pip install .`` 之后直接 ``sightqa``。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

from . import __version__
from .capture import set_dpi_awareness
from .client import ApiError, VisionClient
from .config import (
    INTERVAL_MAX,
    INTERVAL_MIN,
    SPEAK_RATE_MAX,
    SPEAK_RATE_MIN,
    Settings,
    api_key_from_env,
    default_config_path,
    load,
    parse_interval,
    save,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sightqa",
        description="鼠标指题答疑：截图 → 多模态模型 / 本地 OCR → 朗读答案（仅支持 Windows）。",
        epilog="多模态模式要求模型能读图；打开 --ocr 后纯文本模型也可以。",
    )
    parser.add_argument("--interval", type=int, help=f"截图间隔秒数（{INTERVAL_MIN}–{INTERVAL_MAX}）")
    parser.add_argument("--model", help="模型名称，例如 deepseek-flash / gpt-4o / deepseek-chat")
    parser.add_argument("--base-url", help="接口地址，默认 https://api.deepseek.com")
    parser.add_argument(
        "--api-key",
        help="API 密钥；更安全的做法是使用环境变量 DEEPSEEK_API_KEY",
    )
    parser.add_argument("--no-voice", action="store_true", help="不朗读答案")
    parser.add_argument(
        "--rate",
        type=int,
        help=f"朗读语速（{SPEAK_RATE_MIN}–{SPEAK_RATE_MAX}，0 为正常）",
    )
    parser.add_argument("--ocr", action="store_true", help="图片转文字后再答题（可用纯文本模型）")
    parser.add_argument(
        "--no-full",
        action="store_true",
        help="题干被截断时不要补发整屏（默认会补发一次）",
    )
    parser.add_argument("--hide", action="store_true", help="开始运行时隐藏窗口")
    parser.add_argument("--check", action="store_true", help="只测试接口连通性，然后退出")
    parser.add_argument("--config", help=f"配置文件路径，默认 {default_config_path()}")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def merge_settings(args: argparse.Namespace, base: Settings) -> Settings:
    """命令行参数覆盖配置文件。"""
    settings = base
    if args.interval is not None:
        settings = replace(settings, interval=parse_interval(str(args.interval)))
    if args.rate is not None:
        if not SPEAK_RATE_MIN <= args.rate <= SPEAK_RATE_MAX:
            raise ValueError(f"语速需在 {SPEAK_RATE_MIN}–{SPEAK_RATE_MAX} 之间")
        settings = replace(settings, speak_rate=args.rate)
    if args.base_url:
        settings = replace(settings, base_url=args.base_url)
    if args.model:
        settings = replace(settings, model=args.model)
    if args.no_voice:
        settings = replace(settings, speak=False)
    if args.ocr:
        settings = replace(settings, ocr_mode=True)
    if args.hide:
        settings = replace(settings, hide_window=True)
    if args.no_full:
        settings = replace(settings, auto_full=False)
    key = (args.api_key or "").strip() or api_key_from_env()
    if key:
        settings = replace(settings, api_key=key)
    return settings.with_defaults()


def run_check(settings: Settings) -> int:
    """只做一次连通性测试，不打开界面。"""
    if not settings.api_key:
        print("✕ 没有 API 密钥：请设置 DEEPSEEK_API_KEY 或使用 --api-key")
        return 2
    client = VisionClient(settings.api_key, base_url=settings.base_url, model=settings.model)
    mode = "OCR 文字模式" if settings.ocr_mode else "多模态图片模式"
    print(f"→ 正在测试 {settings.model} @ {settings.base_url}（{mode}）")
    try:
        reply = client.test_text() if settings.ocr_mode else client.test()
    except ApiError as error:
        print(f"✕ {error.full_message()}")
        return 1
    except KeyboardInterrupt:
        print("✕ 已取消")
        return 1
    ok = "ok" in reply.text.lower()
    print(f"{'✓' if ok else '✕'} {reply.latency:.1f}s 返回：{reply.text[:120]}")
    if not ok:
        print("  提示：没有按测试要求回复 OK，可能接口或模型不对。")
        return 1
    print("  连通性正常，可以启动界面了。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or None

    try:
        settings = merge_settings(args, load(config_path))
    except ValueError as error:
        print(f"✕ {error}")
        return 2
    if args.check:
        return run_check(settings)

    if os.name != "nt":
        print("✕ 这个工具依赖 Windows 的截图与 SAPI 语音，仅在 Windows 10/11 上运行。")
        return 2

    try:
        save(settings, config_path or default_config_path())
    except OSError as error:
        print(f"! 配置未能保存（{error}），本次仍可正常使用。")
    set_dpi_awareness()
    from .ui import run  # 延迟导入：--check 模式不需要图形界面

    run(settings, config_path=config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
