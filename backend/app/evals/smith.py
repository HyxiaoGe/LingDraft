"""评测传到 LangSmith：每次评测是一个实验，在网站上存着、点开看过程、按用例并排比。检查、跑法、本地的结果文件都不变。

- 只在评测进程里、只在跑用例的那一段开 tracing（tracing_context）：本机 8000、dev、单元测试和 CI 都不上报。
  .env 里只放 LANGSMITH_API_KEY，不放 LANGSMITH_TRACING；没配 key 或加了 --no-smith 就不传。
- 用例的正本在仓库（cases.yaml、decisions.yaml）：每次跑之前按 id 同步成数据集，改过的更新，删掉的也删掉（数据集有版本，旧实验不受影响）。
- 每个用例跑一遍（决策点每重跑一次）是实验里的一行，连着一条 trace：这几轮对话、起标题、推荐问题、裁判的调用都在里面。
  评测是同步跑一轮的（research.play → run_turn → aio.run），交给事件循环时带着调用方的上下文，所以都挂在这一行下面；
  线上接口另起任务用的是空上下文（api.research_runner._spawn），评测不走那条。
- 每项检查记一个分数：多轮的用例按轮算过了几成，没过的写在说明里；passed 是这一行整个过没过（出错的写出错原因）。
- 中间件那几层不记输入输出（configure_trace_policy）：每一层记的都是同一份请求，假模型一轮问天气从 1.5 MB 降到 0.3 MB；
  模型调用、工具调用照常全记。
- 显不显著还是看本地的对比（decisions --compare、research --stats）；网站上的 trace 免费版留 14 天，本地的结果文件一直在。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import configure_trace_policy
from langgraph.types import TracePolicy
from langsmith import Client, traceable, tracing_context
from langsmith import env as ls_env
from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError

RESEARCH_DATASET = "lingdraft-research"
DECISIONS_DATASET = "lingdraft-decisions"
KEY_FIELD = "key"  # 样例的 metadata 里记着它是哪个用例（决策点），同步时按它对
OMITTED = {"omitted": "中间件这一层不记输入输出：和模型调用那条记的是同一份（见 app.evals.smith）"}


def configured() -> bool:
    """配了 key 才传（app.config 已经把 .env 读进环境变量）。"""
    return bool(os.environ.get("LANGSMITH_API_KEY"))


@dataclass
class Example:
    key: str  # 用例或决策点的 id
    inputs: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


def sync_dataset(client: Client, name: str, description: str, examples: list[Example]) -> tuple[Any, dict[str, Any]]:
    """把仓库里的用例同步成数据集：没有的建，改过的更新，仓库里删掉的删掉。返回数据集和 用例 id → 样例 id。"""
    try:
        ds = client.read_dataset(dataset_name=name)
    except LangSmithNotFoundError:
        ds = client.create_dataset(name, description=description)
    have = {(e.metadata or {}).get(KEY_FIELD): e for e in client.list_examples(dataset_id=ds.id)}
    want = {x.key: x for x in examples}
    for key, x in want.items():
        meta = {**x.metadata, KEY_FIELD: key}
        if key in have and (have[key].inputs != x.inputs or (have[key].metadata or {}) != meta):
            client.update_example(have[key].id, inputs=x.inputs, metadata=meta)
    if stale := [e.id for k, e in have.items() if k not in want]:
        client.delete_examples(stale)
    if new := [x for k, x in want.items() if k not in have]:
        client.create_examples(dataset_id=ds.id, examples=[{"inputs": x.inputs, "metadata": {**x.metadata, KEY_FIELD: x.key}} for x in new])
    ids: dict[str, Any] = {k: have[k].id for k in want if k in have}
    if new:
        for e in client.list_examples(dataset_id=ds.id):
            if (k := (e.metadata or {}).get(KEY_FIELD)) in want:
                ids[k] = e.id
    return ds, ids


def _omit(_: Any) -> dict:
    return OMITTED


@contextmanager
def trimmed() -> Generator[None]:
    """这一段里中间件那几层只留名字和用时。设的是整个进程的默认值，出来时清掉。"""
    configure_trace_policy(TracePolicy(process_inputs=_omit, process_outputs=_omit))
    try:
        yield
    finally:
        configure_trace_policy(None)


class Experiment:
    """一次评测：数据集同步好，建一个实验（LangSmith 里是挂着数据集的一个项目），之后每跑一行就 run 一次。"""

    def __init__(self, dataset: str, description: str, examples: list[Example], name: str, metadata: dict[str, Any],
                 used: int, repetitions: int, client: Client | None = None):
        self.client = client or Client()
        self.dataset, self.ids = sync_dataset(self.client, dataset, description, examples)
        meta = {**metadata, "git": ls_env.get_git_info()}
        for attempt in range(5):  # 同一秒起了两次：名字撞了换一个
            try:
                self.project = self.client.create_project(name if not attempt else f"{name}-{uuid.uuid4().hex[:4]}", reference_dataset_id=self.dataset.id,
                                                          metadata=meta, num_examples=used, num_repetitions=repetitions)
                break
            except LangSmithConflictError:
                continue
        else:
            raise RuntimeError(f"LangSmith 上实验名 {name} 一直冲突")
        self.name = self.project.name
        self.feedback_errors = 0

    @property
    def url(self) -> str:
        """这次实验在网站上的页面（和 evaluate() 打出来的一样：数据集的对比页，选中这次）。"""
        base = (self.project.url or "").split("?")[0].split("/projects/p/")[0]
        return f"{base}/datasets/{self.dataset.id}/compare?selectedSessions={self.project.id}"

    def run(self, key: str, inputs: dict[str, Any], fn: Callable[[], dict], scores: Callable[[dict], list[dict]],
            view: Callable[[dict], dict]) -> dict:
        """在一条 trace 里跑 fn（跑一个用例、重跑一次决策点），根上记 view(结果)，再把 scores(结果) 一项项记成分数。返回 fn 的结果。"""
        ended: dict[str, Any] = {}

        @traceable(name=key, run_type="chain", process_outputs=view)
        def target(inputs: dict[str, Any]) -> dict:  # noqa: ARG001 —— inputs 只是记进 trace
            return fn()

        with tracing_context(enabled=True):
            result = target(inputs, langsmith_extra={"project_name": self.name, "reference_example_id": self.ids[key], "client": self.client,
                                                     "on_end": lambda r: ended.setdefault("run", r)})
        run = ended.get("run")
        if run is None:
            return result
        for s in scores(result):
            try:
                self.client.create_feedback(run.id, s["key"], score=s["score"], comment=s.get("comment"), trace_id=run.trace_id,
                                            session_id=self.project.id, start_time=run.start_time, stop_after_attempt=3)
            except Exception:  # noqa: BLE001 —— 分数没传上去不打断评测，最后报个数；本地的结果文件里都有
                self.feedback_errors += 1
        return result

    def close(self) -> None:
        """等后台把 trace 都传完。"""
        self.client.flush()


def start(dataset: str, description: str, examples: list[Example], name: str, metadata: dict[str, Any], used: int,
          repetitions: int) -> Experiment | None:
    """配了 key 就建实验、打出链接；没配或者建不了（连不上、key 不对）就这次不传，打一句为什么，评测照常跑、本地结果照写。"""
    if not configured():
        print("没配 LANGSMITH_API_KEY，不传 LangSmith", flush=True)
        return None
    try:
        exp = Experiment(dataset, description, examples, name, metadata, used, repetitions)
    except Exception as e:  # noqa: BLE001
        print(f"LangSmith 建实验出错，这次不传：{type(e).__name__}: {e}", flush=True)
        return None
    print(f"LangSmith 实验：{exp.url}", flush=True)
    return exp


def check_scores(checks_by_turn: list[list[dict]], passed: bool, error: str | None) -> list[dict]:
    """每项检查一个分数（按轮算过了几成，没过的写在说明里），加上整个过没过。"""
    by: dict[str, list[tuple[int, dict]]] = {}
    for i, checks in enumerate(checks_by_turn, 1):
        for c in checks:
            by.setdefault(c["name"], []).append((i, c))
    multi = len(checks_by_turn) > 1
    out = [{"key": "passed", "score": int(passed and not error), "comment": error}]
    for name, cs in by.items():
        failed = [(f"第{i}轮：" if multi else "") + c["detail"] for i, c in cs if not c["passed"]]
        out.append({"key": name, "score": sum(c["passed"] for _, c in cs) / len(cs), "comment": "；".join(failed) or None})
    return out
