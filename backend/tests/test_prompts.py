"""prompts.toml：每段都能解析，变量缺了直接报错。"""

import pytest
from jinja2 import UndefinedError

from app.graph.research import prompts
from app.prompting import ENV


def test_every_prompt_parses():
    texts = prompts.texts()
    assert {"agent.system", "consolidate.system", "tools.search_web.description"} <= texts.keys()
    for src in texts.values():
        ENV.parse(src)


def test_missing_variable_fails_loudly():
    with pytest.raises(UndefinedError):
        prompts.render("consolidate.user")
    with pytest.raises(KeyError, match="没有 agent.nope"):
        prompts.render("agent.nope")
