"""线上 tracing（app.tracing）：没开什么都不做；开了以后一轮对话是一条 trace，带着 thread_id，中间件那几层只留名字。
LangSmith 换成不连网的客户端（见 test_eval_smith.MemoryClient）。"""

from __future__ import annotations

import langsmith.run_trees
import langsmith.utils
import pytest
from fastapi.testclient import TestClient
from langchain.agents.middleware import _trace_policy, configure_trace_policy

from app import tracing
from app.api import main as api_main
from app.api.research_runner import ResearchRunner
from tests.research_helpers import events, last
from tests.test_eval_smith import MemoryClient

TRACING_VARS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT")


@pytest.fixture
def env(monkeypatch):
    """清掉 tracing 相关的环境变量；langsmith 会缓存读到的值，前后都清掉缓存。"""
    for v in TRACING_VARS:
        monkeypatch.delenv(v, raising=False)

    def clear() -> None:
        langsmith.utils.get_env_var.cache_clear()
        langsmith.utils.get_tracer_project.cache_clear()

    clear()
    yield monkeypatch
    configure_trace_policy(None)
    clear()


def test_nothing_happens_without_tracing(env):
    env.setenv("LANGSMITH_API_KEY", "fake")  # 本机的 .env 只有 key
    assert tracing.start() is False
    assert _trace_policy._DEFAULT_TRACE_POLICY is None


def test_a_turn_is_one_trace_with_its_thread_and_middleware_trimmed(env):
    env.setenv("LANGSMITH_TRACING", "true")
    env.setenv("LANGSMITH_API_KEY", "fake")
    env.setenv("LANGSMITH_PROJECT", "lingdraft-test")
    client = MemoryClient()
    env.setattr(langsmith.run_trees, "_CLIENT", client)
    assert tracing.start() is True
    assert _trace_policy._DEFAULT_TRACE_POLICY is tracing.TRIMMED

    api_main.set_research_runner(ResearchRunner(fake=True))
    st = last(events(TestClient(api_main.app).post("/api/research/threads", json={"text": "广州天气怎么样"})), "state")
    assert st["status"] == "idle"
    tracing.stop()

    runs = list(client.m_runs.values())
    roots = [x for x in runs if not x.get("parent_run_id")]
    assert [x["name"] for x in roots] == ["research"]  # 假模型起标题、推荐问题不调 LangChain，不出 trace
    root = roots[0]
    assert {x["trace_id"] for x in runs} == {root["id"]} and {x["session_name"] for x in runs} == {"lingdraft-test"}
    assert root["extra"]["metadata"]["thread_id"] == st["thread_id"]  # 网站上按对话归到一起
    mw = [x for x in runs if "Middleware." in x["name"]]
    assert mw and all(x["inputs"] == tracing.OMITTED and x.get("outputs", tracing.OMITTED) == tracing.OMITTED for x in mw)
    assert any(x["run_type"] == "llm" and "LingDraft" in str(x["inputs"]) for x in runs)  # 模型调用照常全记
    assert any(x["run_type"] == "tool" and x["name"] == "get_weather" and x.get("outputs") for x in runs)
