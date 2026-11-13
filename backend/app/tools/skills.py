"""读技能的工具（load_skill）：模型觉得用得上就按名字读，结果是那个技能的说明正文（见 app.skills）。

- 技能的目录（名字和什么时候用）拼在系统提示词里（agent.toml 的 skills，只列这段对话能用的，见 agent.skills_offered）。
- 工具一直在列表里，排在最后（前面的工具定义不动，前缀缓存接得上）；这段对话没有技能可用的，调模型时去掉
  （见 agent.ModelSelect）。模型硬调了没给的，这里也不给。
- 说明还原样留在发给模型的那份里的，再读只回一句“已经在了”；被上下文压缩换成一句说明的（compaction 第一层）可以再读。
- 只有说明：不跑东西，不产生来源，不收钱。页面上是过程里的一步（history.step_of 的 skill）。
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.graph.research import history, prompts
from app.skills import catalog


class LoadSkillArgs(BaseModel):
    name: str = Field(description=prompts.render("tools.load_skill.name"))


def make_load_skill_tool():
    catalog()  # 建工具时（服务启动）读一遍：哪个 SKILL.md 写坏了这时就报错，不等到模型去读

    @tool(history.SKILL_TOOL, description=prompts.render("tools.load_skill.description"), args_schema=LoadSkillArgs, response_format="content_and_artifact")
    def load_skill(runtime: ToolRuntime, name: str) -> tuple[str, dict]:
        from app.graph.research.agent import skills_offered  # agent、compaction 用到本模块（build_agent），放在这里导入
        from app.graph.research.compaction import uncompacted

        offered = skills_offered(runtime.state, runtime)
        if not offered:
            raise ToolException(prompts.render("tools.load_skill.unavailable"))
        skill = next((s for s in offered if s.name == name.strip()), None)
        if skill is None:
            raise ToolException(prompts.render("tools.load_skill.unknown", name=name, names=", ".join(s.name for s in offered)))
        art = {"skill": skill.name, "title": skill.title}
        if any(history.skill_loaded(m) == skill.name for m in uncompacted(runtime.state)):
            return prompts.render("tools.load_skill.already", name=skill.name), {**art, "repeat": True}
        return prompts.render("tools.load_skill.loaded", name=skill.name, body=skill.body), art

    load_skill.handle_tool_error = True
    return load_skill
