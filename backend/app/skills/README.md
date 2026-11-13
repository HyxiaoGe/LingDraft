# 技能

这个目录下一个子目录是一个技能，格式照 [Agent Skills](https://agentskills.io/specification)：`SKILL.md` 开头的 YAML 写 `name`（和目录同名）和 `description`（做什么、什么时候用），后面是给模型看的说明。怎么读、怎么给模型见 `__init__.py` 和 `app/tools/skills.py`：`name` 和 `description` 拼进系统提示词，正文等模型调 `load_skill` 才给。

灵稿自己用的两项放在 `metadata` 里：

- `title`：页面上显示的中文名，必填。
- `requires: run_command`：要能跑命令才用得上。不能跑命令的对话（普通对话、游客、没配沙箱）不给这个技能。

说明是给模型看的，用英文写。写错了格式后端启动时就报错。

## 来源和改动

三个技能都来自 [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) 的 `data/skills/`，提交 `43ccfc0d22c5dbe2dd7e1b3670a6fbaed4dfe745`，Apache License 2.0（许可证全文见本目录的 `LICENSE-APACHE-2.0.txt`）。2026-11-14 为灵稿改过，改动如下，其余和原文一样。

三个都改的：

- 开头去掉 Claude Code 专用的 `user-invocable`、`argument-hint`，加上 `license` 和 `metadata`（上面两项）。

`data-visualization`：

- 示例里 `plt.style.use('seaborn-v0_8-whitegrid')` 后面加一行把字体设回 `WenQuanYi Micro Hei`。样式表会把字体换成 Arial，沙箱里中文就成了方框（2026-11-14 在沙箱里核过：换了以后画“销售额”报缺字，设回去正常）。
- 示例里存图、存网页的路径放到 `outputs/` 下：沙箱里只有这个目录的文件会给用户。
- plotly 的示例去掉 `fig.show()`（沙箱里没有窗口），`write_html` 带上 `include_plotlyjs='cdn'`（不带的话一份 4.8 MB）。

`statistical-analysis`：正文没改。

`validate-data`：

- 原文是一条斜杠命令（`/validate-data <要检查的分析>`）。灵稿没有斜杠命令，由模型自己决定什么时候读，所以：标题去掉命令名；`description` 改成两种情况都提（用户让检查一份分析；模型交出自己的分析之前）；“Usage” 一节改写成这两种用法，自查时不输出核查报告，只把该说的限制写进分析里；“Output Format” 前面注明是用户让检查时用的。
- 去掉指向原仓库 `CONNECTORS.md` 的那一行，和结尾的 “Examples”“Tips” 两节（都是斜杠命令的用法）。
- 讲 SQL 的部分留着：用户贴一段查询和结果让检查，这在灵稿里也做得了。
