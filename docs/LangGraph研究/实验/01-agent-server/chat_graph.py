"""实验用：把灵稿的对话 agent 原样挂到 langgraph dev 上（假模型、假网关）。搜索词里带“慢”时假网关慢 4 秒，方便试停止和排队。"""
import asyncio
import time

from dataclasses import dataclass, field
from pathlib import Path

from app.graph.research import agent
from app.graph.research.agent import build_agent
from app.tools.images import ImageStore
from app.research_llm import FakeResearchPlanner, fake_gateway_client

gw = fake_gateway_client()
_search = gw.search


def slow_search(query, **kw):
    if "慢" in query:
        time.sleep(4)
    return _search(query, **kw)


gw.search = slow_search


@dataclass(frozen=True)
class ExpContext:  # Agent Server 按运行传来的 JSON 构造 context，放不进对象：给默认值
    images: ImageStore = field(default_factory=lambda: ImageStore(Path(__file__).parent / "images"))
    check_stop: object = lambda: None


agent.ChatContext = ExpContext


# Agent Server 用 astream 跑图，middleware 要有异步版。我们的都是同步的：放到线程里跑，handler 再转回事件循环
def _bridge(sync_name):
    async def wrapper(self, request, handler):
        loop = asyncio.get_running_loop()
        sync_handler = lambda req: asyncio.run_coroutine_threadsafe(handler(req), loop).result()
        return await asyncio.to_thread(getattr(self, sync_name), request, sync_handler)
    return wrapper


for cls in (agent.TurnMiddleware, agent.ContentGuard):
    cls.awrap_model_call = _bridge("wrap_model_call")
agent.ToolProgressMiddleware.awrap_tool_call = _bridge("wrap_tool_call")
graph = build_agent(FakeResearchPlanner(gw), checkpointer=None)
