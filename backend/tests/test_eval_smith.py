"""评测传到 LangSmith（app.evals.smith）：用不连网的客户端接住上报，看数据集同步、一行一条 trace、中间件那几层裁掉、分数，以及不开的时候什么都不发。"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import langsmith.run_trees
import pytest
from langchain.agents.middleware import _trace_policy
from langsmith import Client
from langsmith.utils import LangSmithNotFoundError

from app import tracing
from app.evals import decisions, research, smith
from app.evals.research import Case, run_case
from app.evals.tape import Tape
from app.research_llm import FakeResearchPlanner
from tests.test_research_eval import FakeJudge


class MemoryClient(Client):
    """不连网的客户端：trace 不攒批（auto_batch_tracing=False），每次上报直接记在内存里；数据集、实验、分数也记在内存里。"""

    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:9", api_key="fake", auto_batch_tracing=False)
        self.m_runs: dict[str, dict] = {}  # 上报的 run：id → 字段（Client 自己有 runs 这个属性）
        self.m_datasets: dict[str, SimpleNamespace] = {}
        self.m_examples: dict[uuid.UUID, SimpleNamespace] = {}
        self.m_calls: list[str] = []  # 数据集上做了什么：create / update / delete
        self.m_projects: list[SimpleNamespace] = []
        self.m_feedback: list[dict] = []

    def create_run(self, **kw: Any) -> None:  # type: ignore[override]
        self.m_runs[str(kw["id"])] = dict(kw)

    def update_run(self, run_id: Any, **kw: Any) -> None:  # type: ignore[override]
        self.m_runs.setdefault(str(run_id), {}).update({k: v for k, v in kw.items() if v is not None})

    def read_dataset(self, *, dataset_name: Any = None, dataset_id: Any = None) -> Any:  # type: ignore[override]
        if dataset_name not in self.m_datasets:
            raise LangSmithNotFoundError(dataset_name)
        return self.m_datasets[dataset_name]

    def create_dataset(self, dataset_name: str, **kw: Any) -> Any:  # type: ignore[override]
        self.m_datasets[dataset_name] = SimpleNamespace(id=uuid.uuid4(), name=dataset_name)
        return self.m_datasets[dataset_name]

    def list_examples(self, dataset_id: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        return iter([e for e in self.m_examples.values() if e.dataset_id == dataset_id])

    def create_examples(self, *, dataset_id: Any = None, examples: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        for x in examples or []:
            e = SimpleNamespace(id=uuid.uuid4(), dataset_id=dataset_id, inputs=json.loads(json.dumps(x["inputs"])), metadata=x["metadata"])
            self.m_examples[e.id] = e
            self.m_calls.append(f"create {x['metadata']['key']}")
        return {}

    def update_example(self, example_id: Any, *, inputs: Any = None, metadata: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        self.m_examples[example_id].inputs, self.m_examples[example_id].metadata = inputs, metadata
        self.m_calls.append(f"update {metadata['key']}")
        return {}

    def delete_examples(self, example_ids: Any, **kw: Any) -> None:  # type: ignore[override]
        for i in example_ids:
            self.m_calls.append(f"delete {self.m_examples.pop(i).metadata['key']}")

    def create_project(self, project_name: str, **kw: Any) -> Any:  # type: ignore[override]
        pid = uuid.uuid4()
        self.m_projects.append(SimpleNamespace(id=pid, name=project_name, url=f"https://smith.test/o/t/projects/p/{pid}?x=1", **kw))
        return self.m_projects[-1]

    def create_feedback(self, run_id: Any = None, key: str = "unnamed", **kw: Any) -> Any:  # type: ignore[override]
        self.m_feedback.append({"run_id": str(run_id), "key": key, **kw})

    def flush(self, timeout: Any = None) -> None:
        pass


def _ex(key: str, say: str) -> smith.Example:
    return smith.Example(key, {"turns": [{"say": say}]}, {"source": "测试"})


def test_sync_dataset_creates_updates_and_deletes():
    client = MemoryClient()
    ds, ids = smith.sync_dataset(client, "d", "说明", [_ex("a", "一"), _ex("b", "二")])
    assert set(ids) == {"a", "b"} and client.m_calls == ["create a", "create b"]
    client.m_calls.clear()
    smith.sync_dataset(client, "d", "说明", [_ex("a", "一"), _ex("b", "二")])
    assert client.m_calls == []  # 没改的不动
    ds2, ids2 = smith.sync_dataset(client, "d", "说明", [_ex("a", "改了"), _ex("c", "三")])
    assert ds2 is ds and sorted(client.m_calls) == ["create c", "delete b", "update a"]
    assert ids2["a"] == ids["a"] and set(ids2) == {"a", "c"}
    assert client.m_examples[ids2["a"]].inputs == {"turns": [{"say": "改了"}]}


def test_check_scores_per_check_and_overall():
    two = [[{"name": "replied", "passed": True, "detail": ""}, {"name": "searched", "passed": False, "detail": "搜了 0 次"}],
           [{"name": "replied", "passed": False, "detail": "没有正文"}]]
    got = {s["key"]: (s["score"], s["comment"]) for s in smith.check_scores(two, False, None)}
    assert got == {"passed": (0, None), "replied": (0.5, "第2轮：没有正文"), "searched": (0, "第1轮：搜了 0 次")}
    one = smith.check_scores([[{"name": "looked_up", "passed": False, "detail": "没查"}]], False, None)
    assert one[1] == {"key": "looked_up", "score": 0, "comment": "没查"}  # 只有一轮不写第几轮
    assert smith.check_scores([], False, "RuntimeError: 坏了") == [{"key": "passed", "score": 0, "comment": "RuntimeError: 坏了"}]


def _experiment(client: MemoryClient, keys: list[str], dataset: str = "d") -> smith.Experiment:
    return smith.Experiment(dataset, "说明", [_ex(k, k) for k in keys], "exp-1", {"mode": "test"}, used=len(keys), repetitions=1, client=client)


def test_a_case_is_one_trace_in_the_experiment_with_middleware_trimmed():
    client = MemoryClient()
    exp = _experiment(client, ["x"])
    case = Case.model_validate({"id": "x", "source": "测试", "turns": [{"say": "广州天气怎么样"}, {"say": "直接答：为什么"}]})
    with smith.trimmed():
        r = exp.run("x", research.case_inputs(case), lambda: run_case(case, FakeResearchPlanner, FakeJudge()), research.case_scores, research.case_view)
    assert _trace_policy._DEFAULT_TRACE_POLICY is None  # 出来就清掉
    assert r["passed"], r
    runs = list(client.m_runs.values())
    roots = [x for x in runs if not x.get("parent_run_id")]
    assert len(roots) == 1 and roots[0]["name"] == "x"
    root = roots[0]
    # 都在这一行的 trace 里、记在这次实验下；根连着数据集里的样例，输出是两轮的正文
    assert {x["trace_id"] for x in runs} == {root["id"]} and {x["session_name"] for x in runs} == {"exp-1"}
    assert root["reference_example_id"] == exp.ids["x"]
    assert [t["say"] for t in root["outputs"]["turns"]] == ["广州天气怎么样", "直接答：为什么"] and root["outputs"]["passed"]
    names = [x["name"] for x in runs]
    assert names.count("research") == 2  # 两轮
    # 中间件那几层只留名字：输入输出都是那句说明；模型调用、工具调用照常全记
    mw = [x for x in runs if "Middleware." in x["name"]]
    assert mw and all(x["inputs"] == tracing.OMITTED and x.get("outputs", tracing.OMITTED) == tracing.OMITTED for x in mw)
    llm = [x for x in runs if x["run_type"] == "llm"]
    assert llm and all("LingDraft" in json.dumps(x["inputs"], ensure_ascii=False) for x in llm)
    tools = [x for x in runs if x["run_type"] == "tool"]
    assert any(x["name"] == "get_weather" and x.get("outputs") for x in tools)
    # 每项检查一个分数，都记在根上
    fb = {f["key"]: f for f in client.m_feedback}
    assert fb["passed"]["score"] == 1 and fb["replied"]["score"] == 1
    assert {f["run_id"] for f in client.m_feedback} == {str(root["id"])}
    assert exp.url == f"https://smith.test/o/t/datasets/{exp.dataset.id}/compare?selectedSessions={exp.project.id}"


def test_nothing_is_sent_without_an_experiment(monkeypatch):
    # 配了 key 也不开：线上服务、单元测试、CI 都走这条
    client = MemoryClient()
    monkeypatch.setattr(langsmith.run_trees, "_CLIENT", client)
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake")
    for v in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(v, raising=False)
    case = Case.model_validate({"id": "x", "source": "测试", "turns": [{"say": "广州天气怎么样"}]})
    assert run_case(case, FakeResearchPlanner, FakeJudge())["passed"]
    assert client.m_runs == {}


@pytest.fixture
def offline_eval(monkeypatch, tmp_path):
    """评测命令整段跑：真实模型、裁判换成假的，LangSmith 换成内存里的，结果写到临时目录。"""
    client = MemoryClient()
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake")
    monkeypatch.setattr(smith, "Client", lambda: client)
    monkeypatch.setattr("app.research_llm.LiveResearchPlanner", lambda scene=None, **kw: FakeResearchPlanner(**kw))
    monkeypatch.setattr(research, "LiveJudge", FakeJudge)
    monkeypatch.setattr(research, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(research, "TAPES_DIR", tmp_path / "tapes")
    return client


def _written(tmp_path) -> dict:
    [f] = (tmp_path / "results").glob("*.json")
    return json.loads(f.read_text(encoding="utf-8"))


def test_research_cli_uploads_an_experiment(offline_eval, tmp_path):
    case = "lookup-weather-month"
    research.main(["--case", case, "--repeat", "2", "--jobs", "2"])
    [p] = offline_eval.m_projects
    assert p.num_examples == 1 and p.num_repetitions == 2 and p.metadata["mode"] is None and "git" in p.metadata
    # 数据集是整份 cases.yaml，跑的只是这一个
    assert len(offline_eval.m_examples) == len(research.load_cases())
    roots = [x for x in offline_eval.m_runs.values() if not x.get("parent_run_id")]
    assert len(roots) == 2 and {x["name"] for x in roots} == {case}
    assert _written(tmp_path)["langsmith"].endswith(f"selectedSessions={p.id}")
    # --no-smith 不传
    research.main(["--case", case, "--no-smith"])
    assert len(offline_eval.m_projects) == 1


def test_eval_still_runs_when_langsmith_is_unreachable(offline_eval, monkeypatch, tmp_path, capsys):
    def down():
        raise ConnectionError("连不上")

    monkeypatch.setattr(smith, "Client", down)
    research.main(["--case", "lookup-weather-month"])
    assert "建实验出错，这次不传" in capsys.readouterr().out
    out = _written(tmp_path)  # 照常跑完、写了结果（假模型这个用例过不了，不看过没过）
    assert "langsmith" not in out and out["cases"][0]["turns"] and "error" not in out["cases"][0]


def test_rejudge_uploads_without_rerunning_the_agent(offline_eval, tmp_path):
    case = "lookup-weather-month"
    research.main(["--case", case, "--no-smith"])
    [old] = (tmp_path / "results").glob("*.json")
    old = old.rename(tmp_path / "old.json")
    research.main(["--case", case, "--rejudge", str(old)])
    [p] = offline_eval.m_projects
    assert p.metadata["mode"] == "rejudge"
    runs = list(offline_eval.m_runs.values())
    assert [x["name"] for x in runs if not x.get("parent_run_id")] == [case] and not any(x["name"] == "research" for x in runs)


def test_decisions_cli_uploads_each_sample(offline_eval, tmp_path):
    d = next(x for x in decisions.load_decisions() if x.id == "weather-month-call1")
    tape = Tape(tmp_path / "tapes" / f"{d.case}.json", fresh=True)
    (tmp_path / "tapes").mkdir()
    run_case(Case.model_validate({"id": d.case, "source": "测试", "turns": [{"say": "广州天气怎么样"}]}), FakeResearchPlanner, FakeJudge(), tape=tape)
    decisions.main(["--decision", d.id, "--samples", "2", "--no-sources"])
    [p] = offline_eval.m_projects
    assert p.num_examples == 1 and p.num_repetitions == 2
    assert len(offline_eval.m_examples) == len(decisions.load_decisions())
    roots = [x for x in offline_eval.m_runs.values() if not x.get("parent_run_id")]
    assert len(roots) == 2 and all(x["outputs"]["tool_calls"] for x in roots)
    assert {f["key"] for f in offline_eval.m_feedback} == {"passed", "looked_up", "replied"}
