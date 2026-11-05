"""把对话的过程传到 LangSmith（线上用）：dev 的 .env 里配了 LANGSMITH_TRACING=true、LANGSMITH_API_KEY、LANGSMITH_PROJECT 才传。
本机的 backend/.env 只有评测用的 key、不开 tracing，单元测试和 CI 都不传（评测自己在跑用例的那一段打开，见 app.evals.smith）。

- 每轮对话一条 trace（运行器另起任务、用空的上下文跑，见 api.research_runner._spawn），带着 thread_id，网站上按对话归到一起；
  起标题、推荐问题也另起任务，各是一条，叫 title、suggest；整理记忆叫 dream。
- 中间件那几层不记输入输出：每一层记的都是同一份请求，一轮问天气就有 1.5 MB（假模型实测）；模型调用、工具调用照常全记。
- 传不上去（连不上、key 过期）是后台线程打一条日志，对话照常。
"""

from __future__ import annotations

import logging
import os

from langchain.agents.middleware import configure_trace_policy
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.types import TracePolicy
from langsmith.utils import tracing_is_enabled

logger = logging.getLogger(__name__)

OMITTED = {"omitted": "中间件这一层不记输入输出：和模型调用那条记的是同一份（见 app.tracing）"}


def _omit(_: object) -> dict:
    return OMITTED


TRIMMED = TracePolicy(process_inputs=_omit, process_outputs=_omit)  # 中间件那几层只留名字和用时


def start() -> bool:
    """服务起来时调：开了 tracing 就把中间件那几层裁掉（设的是整个进程的默认值），返回开没开。"""
    if not tracing_is_enabled():
        return False
    configure_trace_policy(TRIMMED)
    logger.info("LangSmith tracing 已开，项目 %s", os.environ.get("LANGSMITH_PROJECT") or "default")
    return True


def stop() -> None:
    """服务关掉时调：等后台把没传完的 trace 传完（部署重启时不丢最后几条）。"""
    wait_for_all_tracers()
