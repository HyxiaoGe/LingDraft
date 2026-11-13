"""技能（app.skills、load_skill，2026-11-14 起，见 docs/Skills方案.md）：目录拼在系统提示词里，说明正文模型读了才给；
目录和工具只在这段对话有技能可用时给（现在的三个都要跑命令：工作任务、登录用户）；说明还在的不再给一遍，被压缩换成一句话的可以再读。
只用假模型（“用技能：X”）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import ToolException
from pydantic import Field

from app import costs, skills
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from app.graph.research import history
from app.graph.research.agent import AGENT_PROMPT, QUIET_PROMPT, WORK_PROMPT, ChatContext, skills_offered, system_prompt
from app.graph.research.compaction import compact, turn_order, uncompacted
from app.graph.research.state import Compaction, Turn
from app.research_llm import FakeResearchPlanner, FakeToolModel
from app.tools.skills import make_load_skill_tool
from tests.auth_helpers import USER, bearer, guest_token
from tests.research_helpers import events, last, new, say

NAMES = ["data-visualization", "statistical-analysis", "validate-data"]


# ---- 读技能目录 ----


def test_the_bundled_skills_load():
    got = {s.name: s for s in skills.catalog()}
    assert list(got) == NAMES and [s.title for s in got.values()] == ["数据可视化", "统计分析", "分析核查"]
    assert all(s.needs_run for s in got.values())  # 三个都要在沙箱里算、画
    for s in got.values():
        assert s.description == " ".join(s.description.split()) and s.body.startswith("# ")
        # 给 Claude Code 的东西没带过来：斜杠命令、原仓库里的文件
        assert "/validate-data" not in s.body and "CONNECTORS" not in s.body
    # 样式表会把字体换掉（沙箱里中文成方框）：示例里换完样式接着把字体设回来；沙箱里没有窗口，不 show
    viz = got["data-visualization"].body
    after = viz[viz.index("plt.style.use("):][:400]
    assert "plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'DejaVu Sans']" in after and ".show()" not in viz
    assert skills.title_of("validate-data") == "分析核查" and skills.title_of("nope") == "nope"


GOOD = "name: demo\ndescription: Does a thing. Use when asked.\nmetadata:\n  title: 演示"


def _write(root, name: str = "demo", front: str | None = GOOD, body: str = "# Demo\n\nDo it."):
    d = root / name
    d.mkdir()
    (d / skills.FILE).write_text(body if front is None else f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")


def test_a_directory_of_skills_reads_in_name_order(tmp_path):
    _write(tmp_path, "zeta", "name: zeta\ndescription: |\n  Line one,\n  line two.\nlicense: MIT\nmetadata:\n  title: 第二个\n  requires: run_command")
    _write(tmp_path)
    (tmp_path / "__pycache__").mkdir()  # 不是技能的目录、目录外面的文件（说明、许可证）不管
    (tmp_path / "README.md").write_text("说明")
    a, b = skills.catalog(tmp_path)
    assert (a.name, a.title, a.description, a.body, a.needs_run) == ("demo", "演示", "Does a thing. Use when asked.", "# Demo\n\nDo it.", False)
    assert (b.name, b.title, b.description, b.needs_run) == ("zeta", "第二个", "Line one, line two.", True)


@pytest.mark.parametrize(("front", "body", "why"), [
    (None, "# Demo", "开头没有用 --- 围起来的 YAML"),
    ("name: demo\ndescription: [oops", "# Demo", "YAML 解析不了"),
    ("- a\n- b", "# Demo", "不是键值"),
    (GOOD + "\nuser-invocable: false", "# Demo", "标准里没有的键：user-invocable"),
    (GOOD.replace("name: demo", "name: Demo"), "# Demo", "name 不对"),
    (GOOD.replace("name: demo", "name: other"), "# Demo", "和目录名不一样"),
    ("name: demo\nmetadata:\n  title: 演示", "# Demo", "没有 description"),
    (GOOD.replace("Does a thing.", "x" * 1100), "# Demo", "description 超过 1024"),
    (GOOD, "", "没有正文"),
    (GOOD, "字" * 20_001, "正文超过 20000"),
    ("name: demo\ndescription: Does a thing.", "# Demo", "没有 title"),
    (GOOD + "\n  requires: network", "# Demo", "requires 只认 run_command"),
    (GOOD + "\n  version: 2", "# Demo", "字符串到字符串"),
])
def test_a_broken_skill_fails_loudly(tmp_path, front, body, why):
    _write(tmp_path, front=front, body=body)
    with pytest.raises(skills.SkillError, match=f"技能 demo 的 SKILL.md .*{why}"):
        skills.catalog(tmp_path)


def test_a_skill_directory_without_the_file(tmp_path):
    (tmp_path / "demo").mkdir()
    with pytest.raises(skills.SkillError, match="技能 demo 的 SKILL.md 不存在"):
        skills.catalog(tmp_path)


# ---- 目录在系统提示词里，只列这段对话能用的；工具说明里不重复 ----


def test_the_catalog_is_a_section_of_the_system_prompt():
    at, cat = datetime.fromisoformat("2026-11-14T10:00:00+08:00"), skills.catalog()
    for keys in (WORK_PROMPT, QUIET_PROMPT, AGENT_PROMPT):
        text = system_prompt(at, keys, skills=cat)
        for sk in cat:
            assert f"\n- {sk.name}: {sk.description}\n" in text
        assert text.count("[Skills]") == 1 and "load_skill" in text
        # 没有技能可用的对话没有这一段，别的段一个字不变
        without = system_prompt(at, keys)
        assert "[Skills]" not in without and "load_skill" not in without
        assert without == text[:text.index("[Skills]")] + text[text.index("[Skills]"):].split("\n\n", 1)[1]
    work = system_prompt(at, WORK_PROMPT, memory="likes tea", skills=cat)
    assert work.index("[Files you write]") < work.index("[Skills]") < work.index("[Work mode]") < work.index("[What you remember about the user]")
    one = system_prompt(at, WORK_PROMPT, skills=cat[:1])
    assert "\n- data-visualization: " in one and "validate-data" not in one


def test_the_tool_itself(monkeypatch):
    t = make_load_skill_tool()
    assert t.name == history.SKILL_TOOL == "load_skill"
    assert "[Skills]" in t.description and not any(sk.name in t.description for sk in skills.catalog())  # 目录不在这里再列一遍
    assert costs.tool_price(history.SKILL_TOOL) is None  # 不收钱

    def broken():
        raise skills.SkillError("技能 demo 的 SKILL.md 没有正文")

    monkeypatch.setattr("app.tools.skills.catalog", broken)  # 写坏了的技能：建工具时（服务启动）就报错
    with pytest.raises(skills.SkillError):
        make_load_skill_tool()


def _runtime(msgs: list, *, work: bool = True, sandbox: bool = True, owner: str = "u1", compaction: Compaction | None = None) -> ToolRuntime:
    ctx = ChatContext(owner=owner, thread_id="t1", sandbox=object() if sandbox else None)
    state = {"messages": list(msgs), "turns": [Turn(id="turn-1", started_at="2026-11-14T00:00:00+00:00", work=work)]}
    return ToolRuntime(state=state if compaction is None else {**state, "compaction": compaction}, context=ctx, config={},
                       stream_writer=lambda _: None, tool_call_id="c9", store=None)


def test_skills_that_need_commands_are_only_offered_where_commands_run(monkeypatch):
    plain = skills.Skill(name="plain-writing", title="写作", description="Write plainly.", body="# Plain\n\nShort words.", needs_run=False)
    bundled = skills.catalog()
    monkeypatch.setattr(skills, "catalog", lambda: (*bundled, plain))
    work, chat = _runtime([]), _runtime([], work=False)
    assert [s.name for s in skills_offered(work.state, work)] == [*NAMES, "plain-writing"]
    for rt in (chat, _runtime([], sandbox=False), _runtime([], owner="guest:abc")):  # 普通对话、没配沙箱、游客
        assert [s.name for s in skills_offered(rt.state, rt)] == ["plain-writing"]
    load = make_load_skill_tool().func
    text, art = load(runtime=chat, name="plain-writing")
    assert text == '<skill name="plain-writing">\n# Plain\n\nShort words.\n</skill>' and art == {"skill": "plain-writing", "title": "写作"}
    # 这段对话没给的技能，模型硬读也不给；说明里列的是给了的那几个
    with pytest.raises(ToolException, match="There is no skill named data-visualization. Available skills: plain-writing."):
        load(runtime=chat, name="data-visualization")
    monkeypatch.setattr(skills, "catalog", lambda: bundled)
    with pytest.raises(ToolException, match="No skills are available in this conversation."):  # 工具没给，模型硬调的
        load(runtime=chat, name="data-visualization")


# ---- 读过的不再给一遍；压缩换成一句话的可以再读 ----


def _call(cid: str, name: str = "data-visualization") -> AIMessage:
    return AIMessage("", id=f"a-{cid}", tool_calls=[{"name": history.SKILL_TOOL, "args": {"name": name}, "id": cid}])


def _result(cid: str, got: tuple[str, dict]) -> ToolMessage:
    return ToolMessage(got[0], artifact=got[1], tool_call_id=cid, name=history.SKILL_TOOL, id=f"r-{cid}")


def test_instructions_still_in_context_are_not_given_twice():
    load = make_load_skill_tool().func
    msgs: list = [HumanMessage("第一问", id="u1"), _call("c1")]
    first = load(runtime=_runtime(msgs), name="data-visualization")
    assert first[0].startswith('<skill name="data-visualization">\n# Data Visualization Skill') and first[0].endswith("\n</skill>")
    assert first[1] == {"skill": "data-visualization", "title": "数据可视化"}
    msgs += [_result("c1", first), AIMessage("第一问的回答", id="a1"), HumanMessage("第二问", id="u2"), _call("c2")]
    # 同一轮里、后面的轮里再读同一个：只回一句
    again = load(runtime=_runtime(msgs), name=" data-visualization ")
    assert again[0].startswith("The instructions of the data-visualization skill are already in this conversation")
    assert again[1] == {"skill": "data-visualization", "title": "数据可视化", "repeat": True}
    assert load(runtime=_runtime(msgs), name="validate-data")[0].startswith('<skill name="validate-data">')  # 别的技能照常给
    # 没读成的不算读过
    failed = ToolMessage("There is no skill named x.", status="error", tool_call_id="c1", name=history.SKILL_TOOL, id="r-c1")
    assert load(runtime=_runtime([msgs[0], msgs[1], failed]), name="data-visualization")[0].startswith("<skill ")

    # 第一轮压过第一层：发给模型的那份里说明换成一句话（那句“已经在了”也换），state 里的不动；这时再读就给
    msgs += [_result("c2", again), AIMessage("第二问的回答", id="a2"), HumanMessage("第三问", id="u3"), _call("c3")]
    for edge in ("u1", "u2"):
        c = Compaction(cleared_through=edge)
        sent = {m.id: m for m in compact(msgs, c, turn_order(msgs))}
        assert sent["r-c1"].content.startswith("[Removed to save space]") and "call load_skill with the same name" in sent["r-c1"].content
        assert sent["a-c1"].tool_calls == msgs[1].tool_calls and msgs[2].content == first[0]
        assert (sent["r-c2"].content == again[0]) == (edge == "u1")
        assert [m.id for m in uncompacted({"messages": msgs, "compaction": c})][0] == ("u2" if edge == "u1" else "u3")
        assert load(runtime=_runtime(msgs, compaction=c), name="data-visualization")[0].startswith("<skill ")
    # 第二层（整轮换成短版）到了的轮也一样
    c = Compaction(cleared_through="u2", summarized_through="u1", summaries={"u1": "短版一"})
    assert load(runtime=_runtime(msgs, compaction=c), name="data-visualization")[0].startswith("<skill ")
    assert [m.id for m in uncompacted({"messages": msgs})] == [m.id for m in msgs]  # 没压过：都在


# ---- 经接口：工作任务里才有，过程里是一步 ----


class Recording(FakeToolModel):
    """记下主 agent 每次调模型带的工具和系统提示词。bind_tools 复制后还是同一个 list。"""

    sent: list = Field(default_factory=list)
    systems: list = Field(default_factory=list)

    def _message(self, messages):
        self.sent.append(list(self.bound))
        self.systems.append(next((str(m.content) for m in messages if isinstance(m, SystemMessage)), ""))
        return super()._message(messages)


@pytest.fixture
def planner():
    p = FakeResearchPlanner()
    p._model = Recording()
    return p


@pytest.fixture
def client(planner):
    api_main.set_research_runner(ResearchRunner(planner=planner))
    return TestClient(api_main.app, headers=USER)


def _steps(turn: dict) -> list[dict]:
    return [i for i in turn["items"] if i["type"] == "step" and i["kind"] == "skill"]


def _reply(turn: dict) -> str:
    return next(i["text"] for i in reversed(turn["items"]) if i["type"] == "text")


def test_work_task_loads_a_skill_and_shows_it_as_a_step(planner, client):
    evs = events(client.post("/api/research/threads", json={"text": "用技能：data-visualization", "work": True}))
    v = last(evs, "state")
    tid, turn = v["thread_id"], v["turns"][-1]
    assert v["status"] == "idle" and _reply(turn).startswith('技能读到了：<skill name="data-visualization">\n# Data Visualization Skill')
    (step,) = _steps(turn)
    assert (step["text"], step["ok"], step["pending"], step["message"]) == ("数据可视化", True, False, "")
    # 执行中发的事件：先一条 pending，做完一条；页面上是中文名，不带说明正文
    live = [d["step"] for e, d in evs if e == "step" and d["step"]["kind"] == "skill"]
    assert [(s["pending"], s["text"]) for s in live] == [(True, "数据可视化"), (False, "数据可视化")]
    assert "Chart Selection Guide" not in str([d for e, d in evs if e in ("step", "state")])
    # 排在工具列表最后：前面的工具定义不动，前缀缓存接得上；目录在系统提示词里
    assert planner._model.sent[-1][-1] == history.SKILL_TOOL
    assert all("[Skills]" in x and "\n- validate-data: " in x for x in planner._model.systems)
    # 后面的轮里再读：说明还在，只回一句；别的技能照常给
    again = last(events(say(client, tid, "用技能：data-visualization")), "state")["turns"][-1]
    assert _reply(again).startswith("技能读到了：The instructions of the data-visualization skill are already")
    assert _steps(again)[0]["ok"] and _steps(again)[0]["text"] == "数据可视化"
    other = last(events(say(client, tid, "用技能：validate-data")), "state")["turns"][-1]
    assert _reply(other).startswith('技能读到了：<skill name="validate-data">') and _steps(other)[0]["text"] == "分析核查"


def test_a_name_that_is_not_in_the_catalog(client):
    turn = new(client, "用技能：no-such-skill", work=True)["turns"][-1]
    assert _reply(turn).startswith("技能没读成：There is no skill named no-such-skill. Available skills: data-visualization, ")
    (step,) = _steps(turn)
    assert (step["text"], step["ok"], step["message"]) == ("no-such-skill", False, "没有这个技能")


def test_chat_and_guests_get_neither_the_tool_nor_the_catalog(planner, client):
    chat = new(client, "用技能：data-visualization")
    assert _reply(chat["turns"][-1]) == "没有技能可用。" and not _steps(chat["turns"][-1])
    assert history.SKILL_TOOL not in planner._model.sent[-1] and "[Skills]" not in planner._model.systems[-1]
    guest = TestClient(api_main.app, headers=bearer(guest_token()))
    assert _reply(new(guest, "用技能：data-visualization", work=True)["turns"][-1]) == "没有技能可用。"
    assert history.SKILL_TOOL not in planner._model.sent[-1] and "[Skills]" not in planner._model.systems[-1]
    work = new(client, "用技能：data-visualization", work=True)
    assert _reply(work["turns"][-1]).startswith("技能读到了：") and history.SKILL_TOOL in planner._model.sent[-1]
    assert "[Skills]" in planner._model.systems[-1]
