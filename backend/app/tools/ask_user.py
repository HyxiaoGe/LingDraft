"""模型问用户（人介入，2026-11-10 Sean 定，见 docs/Work模式调研.md、docs/LangChain研究/02-人介入.md）：ask_user。

模型做到一半，碰到只有用户能定、不问就可能做偏的地方，调这个工具：工具里调 LangGraph 的 interrupt，这一轮停在这里（检查点存下来、这次执行结束，
见 api.research_runner），页面出一张问题卡；用户回答或者跳过以后，运行器带着回答接着跑（Command(resume=…)），工具把回答交给模型。
- 接着跑时工具从头再执行一遍（interrupt 这一次直接返回回答），所以 interrupt 之前不做有副作用的事，判断也只按这一步开始时的消息（每次执行都一样）；
  回答的格式都在 AskAnswer 里校验（不合格的存不进去，可以再答），interrupt 返回以后不再抛错。
- 一张卡一个问题，选项最多 history.MAX_ASK_OPTIONS 个（也可以不给）；用户可以自己写，也可以跳过：跳过的让模型按自己的判断做，并在回答里说明是按什么做的。
- 一轮最多问 history.MAX_ASKS 次：再问的不停下来，直接让模型按自己的判断做。
- 不回答就一直等，不会过一阵自己接着做。只给主 agent：子 agent 不能问人。不收钱（models.toml 的 [tools] 没定价）。
- 只在 Work 模式的轮里能问（2026-11-10 Sean 定，见 history.WORK）：Chat 的轮不给模型这个工具，硬调了也不停。
- 卡上还有“别再问了”（2026-11-11 Sean 定，AskAnswer.mute）：这个问题当跳过，并且把这段对话记成不再问（history.ASKS_OFF，工具用 Command 写进 state）；
  之后 Work 的轮和 Chat 的轮一样不给这个工具，硬调了也不停，直到用户在页面上恢复提问。
Work 模式里什么时候该问由模型判断（工具说明、系统提示词的 work 一段），代码不替它判断。
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.graph.research import prompts
from app.graph.research.history import ASK_TOOL, ASKS_OFF, MAX_ASKS, ask_args, asks_before, last_human

ASK = "ask"  # interrupt 带的东西里的 type：运行器、页面按它认出这是在问用户（别的中断，比如 MCP 的 elicitation，不是）
MAX_ANSWER = 2000  # 回答最长这么多字


class AskArgs(BaseModel):
    question: str = Field(description=prompts.render("tools.ask_user.question"))
    options: list[str] = Field(default_factory=list, description=prompts.render("tools.ask_user.options"))


class AskAnswer(BaseModel):
    """用户的回答：接着跑时交给 interrupt 的值（response_schema）。skip 是点了跳过，这时 answer 是空的；
    mute 是点了“别再问了”：也是跳过，另外这段对话之后不再问。
    asked_at、answered_at 是运行器填的（问出来、回答的时间，见 research_runner._aanswer_input），算这一轮用时的时候去掉中间这段。"""

    model_config = ConfigDict(extra="forbid")
    answer: str = Field(default="", max_length=MAX_ANSWER)
    skip: bool = False
    mute: bool = False
    asked_at: str = ""
    answered_at: str = ""

    @field_validator("answer")
    @classmethod
    def _trimmed(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _answered(self) -> AskAnswer:
        if not self.skip and not self.answer:
            raise ValueError("回答不能是空的")
        if self.mute and not self.skip:
            raise ValueError("别再问了就是跳过这个问题，不能同时带回答")
        return self


def make_ask_tool():
    @tool(ASK_TOOL, description=prompts.render("tools.ask_user.description", max_asks=MAX_ASKS), args_schema=AskArgs)
    async def ask_user(runtime: ToolRuntime, question: str, options: list[str] | None = None) -> ToolMessage | Command:
        call_id = runtime.tool_call_id or ""
        question, choices = ask_args({"question": question, "options": options})
        if not question:
            return ToolMessage(content=prompts.render("tools.ask_user.empty"), name=ASK_TOOL, tool_call_id=call_id, status="error")
        turns = runtime.state.get("turns") or []
        # Chat 的轮、用户说了别再问的对话不给这个工具（见 agent.ModelSelect）；模型照着之前几轮的样子硬调了也不停下来问
        if not (turns and turns[-1].work) or runtime.state.get(ASKS_OFF):
            return ToolMessage(content=prompts.render("tools.ask_user.chat"), name=ASK_TOOL, tool_call_id=call_id)
        messages = runtime.state.get("messages", [])
        if asks_before(messages[last_human(messages) + 1:], call_id) >= MAX_ASKS:
            return ToolMessage(content=prompts.render("tools.ask_user.limit", max_asks=MAX_ASKS), name=ASK_TOOL, tool_call_id=call_id)
        got = interrupt({"type": ASK, "call_id": call_id, "question": question, "options": choices}, response_schema=AskAnswer)
        content = prompts.render("tools.ask_user.skipped") if got.skip else prompts.render("tools.ask_user.answered", answer=got.answer)
        msg = ToolMessage(content=content, name=ASK_TOOL, tool_call_id=call_id, artifact={"ask": got.model_dump()})
        return Command(update={ASKS_OFF: True, "messages": [msg]}) if got.mute else msg

    return ask_user
