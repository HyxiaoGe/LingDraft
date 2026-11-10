"""prompts.toml：每段都能解析，变量缺了直接报错。"""

import pytest
from jinja2 import UndefinedError

from app.graph.research import prompts
from app.graph.research.agent import SYSTEM_SECTIONS, WORK_SECTIONS
from app.prompting import ENV


def test_every_prompt_parses():
    texts = prompts.texts()
    assert {f"agent.{k}" for k in SYSTEM_SECTIONS} | {"tools.get_current_time.description", "title.prompt", "suggest.prompt",
                                                         "tools.search_web.lingdraft"} <= texts.keys()
    # Work 模式的轮多一段 work，排在记忆前面，别的和 Chat 一样
    assert {f"agent.{k}" for k in WORK_SECTIONS} <= texts.keys() and "work" not in SYSTEM_SECTIONS
    assert [k for k in WORK_SECTIONS if k != "work"] == list(SYSTEM_SECTIONS) and WORK_SECTIONS.index("work") + 1 == WORK_SECTIONS.index("memory")
    for src in texts.values():
        ENV.parse(src)


def test_missing_variable_fails_loudly():
    with pytest.raises(UndefinedError):
        prompts.render("agent.current_date")  # 少了日期
    with pytest.raises(KeyError, match="没有 agent.nope"):
        prompts.render("agent.nope")
