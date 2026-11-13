"""对话链路给模型看的文字都在这个目录的 toml 里：系统提示词、工具说明、系统注入的说明、后台用的提示词。

- agent.toml：主 agent 的系统提示词（按 agent.SYSTEM_SECTIONS 的顺序拼），和系统替模型补的几句话。
- subagent.toml：子 agent 的系统提示词，和派子 agent 的 task 工具。
- tools_web.toml：搜索、读页、查文档、取当前时间。
- tools_travel.toml：天气、当前位置、查地点、比路线、航班、火车、行程卡。
- tools_files.toml：用户发的文件怎么给模型看、读文件、查资料库、写文件和改文件。
- tools_images.toml：看图、画图，用户发的图怎么告诉模型。
- tools_ask.toml：问用户（Work 模式）。
- tools_sandbox.toml：在沙箱里跑命令（工作任务里才有）。
- tools_schedule.toml：建、改、删、列定时任务（登录用户才有），和定时任务触发的一轮前面加的说明。
- tools_skills.toml：读技能（load_skill；技能本身在 app/skills/ 里，不在这儿）。
- compaction.toml：上下文压缩，和取回原文的 recall 工具。
- background.toml：不在回答里出现的几件事：整理记忆、起标题、Auto 判断难易、推荐问题。

几份合成一份读（app.prompting.PromptFile）：键是“段.键”，一个键放在哪份文件里对代码没有区别，同一个键出现两次直接报错。
2026-11-11 以前是一个 prompts.toml（Sean 嫌都挤在一个文件里），拆的时候每个键的文字一字没动。

工具那几份里，description 是工具说明，lingdraft 是接在 agent-tools 给的说明后面的灵稿自己的话，和参数同名的键是参数说明，
其余多半是工具结果里给模型看的话（没做成时的居多），各段上面的注释写了。
模板是 Jinja2（{{ 变量 }}、{% if %}），变量缺了直接报错；代码用 prompts.render("段.键", 变量=…) 取。
改文字只改这里；后端不热加载，改完重启。

都是英文。注释里标着“Fusion:”的段是当初照 Fusion（~/code/fusion/backend/app/ai/prompts）对应的段落写的，只改了工具名、取值范围和灵稿没有的东西；
2026-11-03 Sean 定 Fusion 只作参考、不是设计来源，已经照着写的不动。没标的是灵稿自己写的。
"""

from __future__ import annotations

from pathlib import Path

from app.prompting import PromptFile

PROMPTS = PromptFile(Path(__file__).parent)
texts = PROMPTS.texts
render = PROMPTS.render
