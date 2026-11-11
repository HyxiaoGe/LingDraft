"""prompts/ 里的几份 toml：每段都能解析，变量缺了直接报错；几份合成一份读。"""

import pytest
from jinja2 import UndefinedError

from app.graph.research import prompts
from app.graph.research.agent import SYSTEM_SECTIONS, WORK_SECTIONS
from app.prompting import ENV, PromptFile


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


def test_a_directory_of_files_reads_as_one(tmp_path):
    # 对话的提示词按块分成几份（app/graph/research/prompts/）：合成一份读，键在哪份里没有区别
    (tmp_path / "a.toml").write_text('[agent]\none = "1"\n[tools.x]\ndescription = "x"\n')
    (tmp_path / "b.toml").write_text('[agent]\ntwo = "{{ n }}"\n[tools.y]\ndescription = "y"\n')
    p = PromptFile(tmp_path)
    assert p.texts() == {"agent.one": "1", "tools.x.description": "x", "agent.two": "{{ n }}", "tools.y.description": "y"}
    assert p.render("agent.two", n=2) == "2"
    with pytest.raises(KeyError, match="没有 agent.nope"):
        p.render("agent.nope")
    # 同一个键在两份里都有：不猜用哪个，直接报错
    (tmp_path / "c.toml").write_text('[agent]\none = "again"\n')
    with pytest.raises(ValueError, match="agent.one 在 a.toml 和 c.toml 里都有"):
        PromptFile(tmp_path).texts()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        PromptFile(empty).texts()


def test_conversation_prompts_are_split_by_area():
    assert prompts.PROMPTS.path.is_dir() and len(list(prompts.PROMPTS.path.glob("*.toml"))) > 1
