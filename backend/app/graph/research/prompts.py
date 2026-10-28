"""调研链路给模型看的文字都在 prompts.toml：系统提示词、每步的输入模板、工具描述、系统注入的说明。"""

from __future__ import annotations

from pathlib import Path

from app.prompting import PromptFile

PROMPTS = PromptFile(Path(__file__).with_name("prompts.toml"))
texts = PROMPTS.texts
render = PROMPTS.render
