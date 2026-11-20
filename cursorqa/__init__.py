"""鼠标指题答疑 —— 把鼠标停在题目上，自动截图并交给多模态模型作答。

模块划分（除 ``ui`` 外均不依赖 tkinter，便于测试）：

* :mod:`cursorqa.config`  配置读写与校验
* :mod:`cursorqa.models`  多模态模型识别与提示
* :mod:`cursorqa.capture` 鼠标位置截图
* :mod:`cursorqa.client`  多模态接口调用（重试、错误归类）
* :mod:`cursorqa.speech`  Windows 本地中文朗读
* :mod:`cursorqa.ui`      Tkinter 界面
"""

from __future__ import annotations

__version__ = "1.2.0"
__all__ = ["__version__"]
