"""评测的工具录像：真跑时把每次调 MCP 工具（搜索、读页、天气、地图、航班火车、查文档）的结果录下来，
之后回放时同样的调用直接给录好的结果：不发请求、不花钱，每次拿到的材料一样，前后的差别只来自模型和提示词。

- 按用例一份，存在 data/evals/tapes/<用例>.json（不进仓库：里面是搜到、读到的网页内容）。
- 录在 langchain.mcp 转好的工具这一层（换掉工具的 coroutine，见 app.tools.mcp.McpTool.wrap）：
  中间件、结果整理、来源编号、卡片照常走真代码；工具说明照常从服务现取，改了说明评测照样测得出。
- 按工具名和参数对上。同样的调用录了几次就按顺序给，用完了一直给最后一次。
  没对上的（比如换了个搜索词）照常真调，补录进去；每轮记下对上几次、真调几次（结果里的 tape）。
- 抛错的不录（连不上、网关上限、MCP 报的 isError），回放时照常真调；服务在结果里说没成的（ok=false，比如读页超时）照样录，
  回放时 agent 遇到的也是同样的失败。
- 录的时候记下用例开始的时间；回放时把这个用例的时钟挪到那时（app.clock），“明天”和录的那天一样。
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from app.clock import Clock

_locks: dict[Path, threading.Lock] = {}
_locks_mu = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _locks_mu:
        return _locks.setdefault(path.resolve(), threading.Lock())


def key(tool: str, args: dict) -> str:
    return f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


class Tape:
    """一个用例的一次运行用一个。fresh=True 是重新录（不看已有的录像，存的时候整份换掉）；
    否则有录像就回放、没对上的补录，没有录像等于第一次录。"""

    def __init__(self, path: Path, *, fresh: bool = False) -> None:
        self.path = path
        data = {} if fresh or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        self.fresh = fresh or not path.exists()
        self.recorded_at: datetime | None = datetime.fromisoformat(data["recorded_at"]) if data.get("recorded_at") else None
        self._old: dict[str, list[dict]] = {}
        for c in data.get("calls", []):
            self._old.setdefault(key(c["tool"], c["args"]), []).append(c)
        self._used: dict[str, int] = {}
        self.new: list[dict] = []
        self.hits = 0
        self.live = 0

    def start(self, clock: Clock) -> None:
        """用例开始时调：回放就把时钟挪到录的时候，第一次录就记下现在。"""
        now = datetime.now(UTC)
        if self.recorded_at is None:
            self.recorded_at = now
        else:
            clock.shift = self.recorded_at - now

    def counts(self) -> dict[str, int]:
        return {"hits": self.hits, "live": self.live}

    def _take(self, k: str) -> dict | None:
        got = self._old.get(k)
        if not got:
            return None
        i = self._used.get(k, 0)
        self._used[k] = i + 1
        return got[min(i, len(got) - 1)]

    def wrap(self, tool: BaseTool) -> BaseTool:
        call_live = tool.coroutine  # type: ignore[attr-defined]  # langchain.mcp 给的是 StructuredTool
        name = tool.name

        async def call(**args: Any) -> tuple[Any, Any]:
            k = key(name, args)
            if (rec := self._take(k)) is not None:
                self.hits += 1
                return copy.deepcopy(rec["content"]), copy.deepcopy(rec["artifact"])
            content, artifact = await call_live(**args)
            self.live += 1
            self.new.append(copy.deepcopy({"tool": name, "args": args, "content": content, "artifact": artifact}))
            return content, artifact

        tool.coroutine = call  # type: ignore[attr-defined]
        return tool

    def save(self) -> None:
        """重新录的整份换掉；回放的只把没对上、真调了的补进去（同一个用例同时跑几遍时，别的那遍补的也留着）。"""
        if not self.new and not self.fresh:
            return
        with _lock(self.path):
            if self.fresh:
                data: dict[str, Any] = {"recorded_at": self.recorded_at.isoformat() if self.recorded_at else None, "calls": []}
            else:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            known = {key(c["tool"], c["args"]) for c in data["calls"]}
            data["calls"] += [c for c in self.new if self.fresh or key(c["tool"], c["args"]) not in known]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
