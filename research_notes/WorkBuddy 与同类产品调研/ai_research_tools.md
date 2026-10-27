# AI 深度研究 / 产品调研报告类产品调研（截至 2026-10-27）

> 说明：本笔记面向"LangGraph + 人机协同检查点（HITL）"的内容创作产品（海报 + 调研 + PPT）设计。
> - "Cited Findings" 中每条均附来源 URL；多数来源是官方帮助中心/官方博客/论文，少数是媒体或第三方评测，已逐条标注性质。
> - 多个官方页面（help.openai.com、perplexity.ai/hub、openai.com、genspark.ai）在本次检索中返回 403，只能依赖搜索摘要或二手报道，相关条目已注明"（搜索摘要，未读全文）"。
> - 所有"Inferences"为基于引用事实的推断，未经验证；时间线以来源中给出的日期为准，未给出日期的按来源发布日期推断。

---

## 1. 全球产品：从提问到带引用报告的 UX 机制（澄清 → 计划 → 运行 → 引用 → 反馈 → 输出）

### Takeaway
2026 年 10 月的主流格局是"两段式 HITL"：OpenAI 在 Chat 中先生成可编辑的研究计划（并可过滤网站、补充信源），在 Work/Codex 中改为先问澄清问题；Gemini 在应用端展示可编辑的多点计划，API 端用 `collaborative_planning` 开关实现同样的"先计划、再确认"；Kimi/Qwen 一轮澄清；Perplexity/Claude 不做前置澄清直接跑。引用几乎全部采用"正文内联 + 可点击跳转/高亮原文"；输出端普遍走"报告 → Canvas/页面 → 音频/信息图/幻灯片"的二次衍生链路。

### Cited Findings

**OpenAI ChatGPT Deep Research（含轻量版）**
- 在 Chat 中，模型会先生成一份"研究计划"供用户审阅和修改；用户还可以"filter websites（过滤网站）"并"提供额外的信源上下文"；在 Work 或 Codex 中，模型则在首个 prompt 之后先提出澄清问题 — [OpenAI Help Center: Deep research in ChatGPT（搜索摘要，页面 403 未读全文）](https://help.openai.com/en/articles/10500283-deep-research-in-chatgpt)
- 运行中可以随时中断并"调整研究重点，包括调整它能访问的信源"；完成后在全屏报告视图中展示目录、"sources used（已用信源）"区块用于查证，以及"activity history（活动历史）"展示研究过程 — [OpenAI Help Center（搜索摘要）](https://help.openai.com/en/articles/10500283-deep-research)
- 2025-04 推出轻量版（o4-mini 驱动）：Free 用户每月 5 次轻量任务；Plus/Team 每月 10 次完整版 + 15 次轻量版；Pro 每月 125 次完整 + 125 次轻量；轻量版不出现在模型选择器中，达到完整版上限后自动切换；BrowseComp 得分 45.6%（完整版 51.5%） — [the-decoder, 2025-04](https://the-decoder.com/deep-research-feature-now-available-to-free-chatgpt-users/); [eWeek](https://www.eweek.com/news/openai-o4-mini-lightweight-reasoning/)
- 2025-02 首发时 Plus/Team/Enterprise/Edu 为每月 10 次，Pro 从 100 次提高到 120 次 — [eWeek, 2025-02](https://www.eweek.com/news/openai-expands-deep-research/)
- 2026-04 出现 $100 的新 Pro 档位，介于 $20 Plus 和 $200 Pro 之间，"提供更高的 Deep Research 配额"，但未给出具体次数（第三方定价汇总，需核实） — [cometapi 2026 定价汇总](https://www.cometapi.com/chatgpt-pricing-2026-free-vs-go-vs-plus-vs-pro/)
- API：2025-06-26 开放 `o3-deep-research` 与 `o4-mini-deep-research`，支持 web search、数据分析、MCP、代码执行；提供 webhook 以免轮询长任务；reasoning web search 定价 $10/千次调用起 — [the-decoder, 2025-06-26](https://the-decoder.com/openai-expands-api-access-to-deep-research-models/)
- Simon Willison 对 Deep Research system card 的评价：核对引用能抓到大部分幻觉，但"遗漏"更难发现，因为工具可能根本没搜到关键细节；带标题和引用的"报告体"会制造误导性的专业感 — [simonwillison.net](https://simonwillison.net/b/8511)

**Google Gemini Deep Research（应用端 + API）**
- 应用端：把 prompt 转为"多点研究计划"，用户可以在开始前审阅和修改（"You're in control of the plan"）；运行时子任务部分并行、部分串行，可浏览"数百个网站"；可选接入 Gmail/Drive/Chat；支持上传文件；"thinking panel"展示已学到什么、下一步计划；综合阶段会"评估信源、识别主题与不一致，进行多轮自我批判"；任务异步运行，完成后通知；报告可在 Canvas 中转为互动内容、测验、Audio Overview；当前模型 Gemini 3（首发 2024-12 用 1.5 Pro） — [gemini.google/overview/deep-research](https://gemini.google/overview/deep-research/)
- 一份德文教程描述了"Edit plan"按钮：可在计划里增加/划掉条目（如竞品、时间段、遗漏问题）再启动；典型运行 5–10 分钟 — [jeanhinz.io（第三方教程）](https://jeanhinz.io/gemini/deep-research/)
- 另一份 2026-05 的第三方教程：从工具菜单启用 Deep Research → 输入问题 → 批准"plan card" → 运行 5–15 分钟 — [macmyths.com（第三方）](https://macmyths.com/how-to-use-geminis-deep-research-tools-for-better-research-in-2026/)
- API（Interactions API，预览）：两个 agent `deep-research-preview-04-2026`（偏速度、适合流式进 UI）和 `deep-research-max-preview-04-2026`（最彻底）；必须 `background=True`，轮询 `in_progress → completed/failed`；`stream=True` 时事件含 `step.delta`（thought/text/image）；`agent_config` 支持 `thinking_summaries`、`visualization`（自动生成图表）、`collaborative_planning`（先返回计划，用 `previous_interaction_id` 来回修改，再设为 False 批准启动）；默认工具 `google_search`、`url_context`、`code_execution`，可接远程 `mcp_server` 与 `file_search`（自有语料库）；完成后可用 `previous_interaction_id` 追问而不重跑 — [ai.google.dev Deep Research 文档](https://ai.google.dev/gemini-api/docs/deep-research)
- 企业版（Vertex/Gemini Enterprise Agent Platform）：输入 1,048,576 token、输出 65,536 token；输入支持文本与 PDF（每 prompt 最多 3,000 个文件、每文件 3,000 页、PDF 50MB）；支持 citations、thought summaries；不支持结构化输出/函数调用；知识截止 2025-01 — [docs.cloud.google.com](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/google/deep-research)
- 一位开发者报告 Deep Research Max 经 Interactions API 运行数分钟后返回 HTTP 500，标准版正常（单一未确认报告） — [discuss.ai.google.dev](https://discuss.ai.google.dev/t/regression-deep-research-max-via-interactions-api-fails-with-http-500-after-in-progress-while-standard-deep-research-works/171823)
- 第三方报道称 Interactions API 于 2026-06-22 GA；Google 西语页面也称已 GA，但未找到 Google 官方公告确认日期 — [mer.vin（第三方）](https://mer.vin/2026/06/gemini-interactions-api-explained-googles-primary-interface-for-models-and-agents/)
- Google I/O 2026：AI Mode 中的"Deep Search"把问题拆为多次搜索并生成更详细回答；AI Mode 默认由 Gemini 3.5 Flash 驱动（Engadget 汇总） — [Engadget I/O 2026 汇总](https://engadget.com/2176896/everything-google-announced-io-2026-gemini-omni-spark)
- Deep Research 报告 → Audio Overview（播客式）早在 2025-03 即可从报告文件菜单启动；当时免费用户每月 5 份报告 — [nextpit](https://nextpit.com/gemini-ai-podcast-generation-canvas-coding-test-apps); [Yahoo Tech](https://tech.yahoo.com/general/articles/notebooklms-audio-overviews-home-within-194732135.html)
- 报告在 Canvas 中可转为网页、信息图、测验、闪卡、音频概览（第三方教程）；另一 2026-07 博客描述从报告生成信息图、导出 Google Doc、生成音频 — [graduateschool.edu](https://www.graduateschool.edu/learn/ai/gemini-deep-research-canvas); [geshan.com.np 2026-07](https://geshan.com.np/blog/2026/07/google-gemini-features/)

**NotebookLM（Gemini 生态下游）**
- 2025-11-21 前后上线 Slide Decks 与 Infographics，视觉由 Nano Banana Pro（Gemini 3 Pro Image）生成；Infographics 后续增加方形（社媒友好）选项 — [taxheal 转载（二手）](https://www.taxheal.com/?p=110288)
- NotebookLM 内的 Deep Research 运行后"不仅把综合报告加为 source，也把它用到的全部来源加进 notebook"；Fast Research 几秒给约 10 个来源；可对单张幻灯片点"Revise"下修改指令，系统会重新生成整套 deck；支持上传品牌规范作为 source 以统一 Reports/Slide Decks 的风格 — [christopherspenn.com 2026-02](https://www.christopherspenn.com/2026/02/notebooklms-game-changing-upgrade-create-stunning-infographics-slides-deep-research-in-one-powerful-ai-tool/); [jeffsu.org](https://www.jeffsu.org/notebooklm-changed-completely-heres-what-matters-in-2026/)
- 一位评测者认为 NotebookLM 的 Deep Research 质量不如其他助手的同类功能（主观评价） — [jeffsu.org](https://www.jeffsu.org/notebooklm-changed-completely-heres-what-matters-in-2026/)

**Anthropic Claude Research**
- 官方帮助中心（当前版本）：Research 运行多轮相互递进的搜索，根据发现决定下一步；可用于 Pro/Max/Team/Enterprise，Web/Desktop/Mobile；需先开启 web search，从 "+" 菜单选择 Research；会搜索网页与已连接工具（Google Workspace 等 connectors），研究期间自动调用 connectors 不逐次询问；移动端只搜网页且不提示；不能使用 Claude Desktop 本地 MCP；用量与普通对话共享配额；研究固定运行在 Claude Opus 5.5（回退 Opus 4.8），与聊天选择的模型无关 — [support.claude.com](https://support.claude.com/en/articles/11088861-using-research-on-claude-ai)
- 2025-04-15 首发（Max/Team/Enterprise，美/巴/日），强调"引用易于核实"；2025-05-01 推出"advanced Research"：最长运行 45 分钟，跨"数百个内部与外部信源"，交付"fully cited reports"，同期推出 Integrations（Jira、Confluence、Zapier、Asana 等） — [Maginative, 2025-05](https://www.maginative.com/article/anthropic-launches-claude-integrations-and-expands-research-capabilities/); [getcoai](https://getcoai.com/news/ai-research-mode-extends-to-45-minutes-for-claudes-reports)
- 德媒注意到 Research 输出仍会出现幻觉，建议用户自行核实来源 — [computerwoche.de](https://www.computerwoche.de/article/3977506/gut-ki-ergebnis-will-weile-haben.html)

**Perplexity（Research / Deep Research、Labs、Comet）**
- 2025 年初用户评测：Perplexity 收到首条查询即进入深度研究，没有澄清环节（与 ChatGPT 不同），作者认为这迫使用户写极详细的 prompt — [uzyn.com, 2025](https://uzyn.com/2025/thoughts-on-perplexity-deep-research-after-24-hours/)
- Labs 发布时"Deep Research"更名为"Research"，定位在 Search 与 Labs 之间，通常 3–4 分钟；Labs 面向交付物（报告、表格、仪表盘、简单 web app），常需 10 分钟以上，仅付费计划 — [Perplexity Labs 发布博文（搜索摘要，页面 403/404）](https://www.perplexity.ai/hub/blog/introducing-perplexity-labs)
- 2026 第三方汇总：Deep Research 对 Max/Pro 用户运行在 Claude Opus 4.5 上，可直接生成演示文稿、电子表格、仪表盘、网站；Comet 浏览器全平台免费（时间口径不一：2025-10 全球开放 vs 2026-03 免费） — [beginnersinai.org（第三方）](https://beginnersinai.org/whats-new-perplexity-2026/); [frankx.ai（第三方）](https://frankx.ai/blog/ultimate-perplexity-workflow-2026)
- 2Slides 指南（供应商营销）：Perplexity 不生成幻灯片文件，建议"先要带来源的统计数据 → 手动核实 → 喂给幻灯片生成器" — [2slides.com](https://2slides.com/blog/how-to-use-perplexity-for-research-backed-slides)
- MiniMax Agent 采用 Perplexity 的搜索基础设施（Aravind Srinivas 公告，称在 700+ agent 任务上评估了三家搜索供应商；厂商口径） — [aitoolly 转载](https://aitoolly.com/ai-news/article/2026-08-04-evaluating-the-efficiency-of-minimax-agent-an-in-depth-look-at-architecture-and-real-world-api-perfo)

**xAI Grok DeepSearch / DeeperSearch**
- 2025-02-19 随 Grok 3 发布，被描述为"合并多来源关键信息、权衡冲突事实与观点"，以"final summary trace"收尾；Premium+ 用户可用 — [x.ai/news/grok-3](https://x.ai/news/grok-3)
- 第三方指南：DeepSearch 把问题拆为子查询，并行搜 Web 与 X，跟随新链接，维持运行摘要，直到 10 步上限或时间阈值；用户可在"Thoughts"面板查看中间推理；API 侧对应 `web_search` + `x_search` 服务端工具，仅在工具被调用时产生 citations；DeeperSearch 为更多迭代、更深链接遍历的版本（仅此一处描述，无官方文档） — [tryprofound.com（第三方）](https://www.tryprofound.com/blog/understanding-grok-a-comprehensive-guide-to-grok-websearch-grok-deepsearch)
- 官方 API 文档：`web_search` 支持 `allowed_domains`/`excluded_domains`（各最多 5 个，不能同时设置）、`enable_image_understanding`、`enable_image_search`；响应带 `citations` — [docs.x.ai](https://docs.x.ai/docs/guides/live-search)

**Manus**
- Wide Research（2025-07-31）：每个子代理都是"完全能力的通用 Manus 实例"而非预设角色；面向数百个条目的大规模任务（Fortune 500 梳理、MBA 项目对比、GenAI 工具普查）；先 Pro，后逐步放开 Plus/Basic；页面未提及输出格式与子代理数量 — [manus.im/blog/introducing-wide-research](https://manus.im/blog/introducing-wide-research)
- Manus 产品博客列有 "Manus Slides"（Nano Banana Pro 驱动）、"PowerPoint Slides"、"GPT Image 2 in Manus Slides" 等条目；Manus 现为 Meta 一部分，但日媒称 2026-04 该收购未获中国监管批准，状态存疑 — [manus.im/zh-cn/blog/product](https://manus.im/zh-cn/blog/product); [tenbin.ai（日文第三方）](https://tenbin.ai/media/ai_guide/manus-full-guide)

**Genspark**
- Sparkpages：结构化一页式报告，含媒体与引用、表格、内置 copilot；Super Agent 可自主生成网站/演示文稿 — [lindy.ai 评测](https://wf.lindy.ai/blog/genspark-review); [webscraft.org](https://webscraft.org/blog/genspark-ai-oglyad-superagent-yakiy-avtonomno-stvoryuye-sayti-prezentatsiyi?lang=en)
- 定价（第三方口径不一致）：Free $0 / Plus $24.99 / Pro $249.99，Plus 约 10,000 积分/月，Pro 约 125,000；一套 slide deck 估计消耗 250–450 积分（另一来源称 50–100）；Trustpilot 平均 1.7 星，82% 一星，主诉为扣费争议与积分消失 — [eesel.ai](https://www.eesel.ai/blog/genspark-ai-review); [fast.io](https://fast.io/resources/genspark-ai-review-2026/)

**You.com ARI**
- 2025-02-27 发布：并行处理 400+ 来源，5 分钟内出报告，带"verified citations"与交互图表；2025-10 入选 TIME 最佳发明；自报对竞品 76% 胜率、FRAMES 80% 准确率 — [you.com ARI 发布](https://you.com/resources/introducing-ari-the-first-professional-grade-research-agent-for-business); [you.com TIME 获奖](https://you.com/resources/ari-named-on-times-list-of-the-best-inventions-of-2025)

**Consensus / Elicit / Scite（学术，简述）**
- Elicit：系统综述筛选、自定义抽取列，约 1.38 亿篇论文；Pro 可筛 5,000 篇、Enterprise 40,000 篇；Consensus：面向是非型问题的"Consensus Meter"展示研究分歧、Q1–Q4 期刊过滤、引用阈值、Deep Search；Scite：展示后续论文"支持/反驳"某结论；二者都基于 Semantic Scholar 数据 — [paperguide.ai 对比（第三方）](https://paperguide.ai/blog/elicit-vs-consensus/); [thedrive.ai（第三方）](https://thedrive.ai/blog/elicit-vs-consensus-ai-research-tools)

### Inferences
- 澄清与计划是"二选一或先后"的两种 HITL 形态：OpenAI 用"Chat 看计划 / Work-Codex 问问题"区分场景，说明在对话型界面里"可编辑的计划卡"比多轮问答更省交互；在任务/工程型界面则倾向问答。Gemini API 把它做成布尔开关 `collaborative_planning`，是最可直接复用的接口设计。
- "运行中可中断并调整信源"（OpenAI）与"完成后 previous_interaction_id 追问"（Gemini API）共同构成"运行前/运行中/运行后"三类干预点；但没有任何主流产品支持"只重写某一节、其余保持不变"的节级局部再生成（见 Gaps）。
- 报告 → 衍生物链路已成标配（Gemini 报告→Canvas→信息图/音频；NotebookLM→Slides；Perplexity Labs；Genspark/Manus Slides），但几乎都是"整篇重生成"，而非基于引用图谱的受控转换。

### Gaps
- OpenAI 帮助中心、Perplexity 帮助中心/博客、Genspark 官网均 403，无法确认 2026-10 当下的精确配额、导出格式（PDF/docx）与 Research 的澄清策略现状。
- 未找到 Claude Research 是否会先问澄清问题的官方说明，也未找到其当前运行时长上限的 2026 版本说明。
- 未找到任何产品"节级局部修订（保留其他章节）"的官方文档；Gemini 的 Canvas 编辑是文档级编辑而非研究级再跑。
- Grok DeeperSearch 无官方文档。

---

## 2. 中国产品：秘塔、Kimi、天工、纳米、百度、腾讯（元宝/ima/WorkBuddy）、阿里（夸克/通义）、豆包、扣子空间、MiniMax、智谱

### Takeaway
中国产品在"过程可视化"和"报告→交付物"上走得更激进（秘塔的实时研究路径图、天工的"溯源到原文段落"+ 文档/PPT/表格/网页/播客五模态、Kimi 的内嵌引用点击高亮原文 + 交互式 HTML 报告），澄清环节普遍是"一轮确认"（Kimi、天工、Qwen、纳米）；但官方对"计划确认""节级修订""信源过滤"的文档普遍缺失，主要靠媒体实测。

### Cited Findings

**秘塔AI搜索（Metaso）深度研究**
- 2025-07-15 发布即免费公开（登录即用，无需申请/会员）；提问后实时在"白布"上生成研究路径图，把问题拆成方向性子问题 → 逐个检索 → 头脑风暴新子问题 → 跨子问题关联；每个子问题结论用颜色标注：绿=已得出结论、紫=仍在完善、红=信息缺失；每个知识点标注来源可点击到原文；AMD vs NVIDIA 示例引用 400+ 中英文献；报告一键转为交互网页（自动排版、从相关报道抓图），也可导出 Word/PDF；搜索范围可选 全网/文库/学术/图片/视频/播客/我的（私有）；官方称 BrowseComp 与 xbench-DeepSearch 中英文均最优（仅图表，无数字） — [量子位 2025-07-15](https://www.qbitai.com/2025/07/309591.html)
- 2025-02-18 曾推出前身"浅度研究"（小模型 + 大模型协同） — [AIbase](https://news.aibase.com/zh/news/15445)
- 秘塔搜索 API 2025-07 上线：每次查询 0.03 元人民币，覆盖网页/图片/视频/文库，提供网页全文获取与问答接口，入口在 metaso.cn 首页"API" — [AIbase 2025-07](https://news.aibase.com/zh/news/19928)

**Kimi 深度研究（Kimi-Researcher）**
- 官方帮助：提交问题后 Kimi 会提出一轮澄清问题确认/收窄方向，可点"做个全面的研究"跳过；通常 10–25 分钟，后台运行、完成通知；引用内嵌正文，点击跳转并高亮原文；平均每份报告约 26 个信源、超一万字；每任务平均 23 步推理、规划约 74 个关键词、发现 206 个 URL、保留约前 3.2% 高质量内容；输出：Markdown 深度报告（有目录、可导出 PDF/Word）+ 交互式可视化 HTML 报告（可网页/手机预览、复制源码、公开链接分享）；帮助页称可编辑 PPT 不在范围内；专业数据库靠用户在提问中指定（如 PubMed、arXiv）；Moderato 套餐一次任务约消耗月配额 5–10%，失败退还配额；不适合创意写作、固定模板；长而不清晰的输入会带偏方向 — [kimi.com 帮助中心](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview)
- 官方功能页（2026 版）：输入主题（可上传文件）后提出澄清问题，可确认/收窄/放宽范围；覆盖公开网络 + 新闻、政府、学术、企业数据库、实时金融经济数据；输出 Markdown/PDF/PPT/Word/Excel/交互 HTML（可在首条 prompt 或对话中指定）；免费计划含月度额度，按 token 计量而非按次 — [kimi.com/features/deep-research](https://www.kimi.com/zh-hans/features/deep-research)
  - 注：帮助页说"PPT 不在范围内"，功能页列出 PPT 输出，两页口径冲突，以功能页日期更新为准但未验证。
- Kimi-Researcher 2025-06-20 开启小范围灰度，基于端到端自主强化学习 — [ai-bot.cn 转述](https://ai-bot.cn/kimi-researcher/)

**天工超级智能体 / Skywork（昆仑万维）**
- 2025-05-22 全球发布，定位"基于 deep research 的 AI 版 Office"：文档、PPT、表格、网页、播客五模态 + 通用智能体（接 10+ MCP）；PPT 智能体会先就主题、内容类型、风格提澄清问题，再拆成原子步骤；文档智能体有"任务确认"步骤，提出标题、重点、叙事角度；文档/PPT/网页带溯源标签，正文可链接到来源的具体段落，图片可链接到网页或知识源；生成结果可在线编辑，分享前可代码级编辑，可导出本地或网页分享；架构五层：自研 Deep Research 模型（RL 训练）→ Agent Workflow（任务拆解、每个子任务各自独立上下文跑 Deep Research）→ 自研生成模型 → 引用/反思/检查工作流 → 自研在线编辑系统；开源 SkyworkAI/DeepResearchAgent，并在 mcp.so 开放文档/表格/PPT 三个 MCP；2025-05 GAIA 榜首（厂商口径） — [36氪转载新智元 2025-05-22](https://www.36kr.com/p/3303246919228167); [中国新闻网](https://www.chinanews.com/cj/2025/05-22/10420097.shtml)
- PPT 支持在线编辑并导出 pptx/pdf — [AIbase](https://news.aibase.com/zh/news/18256)
- Deep Research Agent v2（2025-08）：新增多模态检索/理解/生成；自报 BrowseComp 27.8%，开启并行思考模式后 38.7% — [AIbase](https://news.aibase.com/zh/news/20505)

**纳米AI（360）超级搜索智能体**
- 2025-05-27 澎湃（360 关联作者撰写的宣传稿）：支持文/图/视频提问；用 MCP 工具跨社交、短视频、电商、地图、论坛检索；复杂任务可调用"数千"工具、写代码、操作浏览器；规划前会先问澄清问题（出发时间、偏好等）；结果以结构化内容交付，旅行计划为嵌入地图的网页；冰箱选购示例检索 500+ 网页；敏感操作在沙箱内运行 — [澎湃新闻 2025-05-27](https://www.thepaper.cn/newsDetail_forward_30881264)
- 第三方工具站：结合 360 搜索索引与多模型协同，提供深度搜索模式，一句话生成视频/报告/PPT — [aitoollab.cn](https://www.aitoollab.cn/tools/nano-ai-search/)

**百度（文心）**
- 2026-06-25 科技日报：百度把分散的文心站点整合为统一"百度文心"网站，新增 Office 在线编辑（PPT/Excel/Word）、定时任务、高考 AI 志愿报告、一键 AI PPT，以及"基于文心大模型 5.1 深度搜索能力的深入研究"；称文心 5.1 登 LMArena 搜索榜国内第一（自报） — [科技日报 2026-06-25](https://www.stdaily.com/web/gdxw/2026-06/25/content_537227.html)
- 2025-02 文心一言上线深度搜索（思考规划 + 工具调用），2025-04-01 起免费 — [TechNews 2025-02](https://technews.tw/2025/02/13/yiyan-free-april/)

**腾讯：元宝 / ima / WorkBuddy**
- 元宝 2024-07-01 上线"深度搜索"：从深度和广度扩展问题，同步生成内容大纲、思维导图、相关人物事件梳理；当时并非可主动选择的模式，而是先正常回答再提供一键"深度搜索"选项；检索基于微信搜索、搜狗搜索，依托公众号/视频号内容 — [爱范儿 2024](https://www.ifanr.com/1591412); [量子位 2024-07](https://www.qbitai.com/2024/07/161931.html)
- 2026-06-15 腾讯宣布 ima 知识库作为信源接入元宝：覆盖 20+ 行业，知识内容被应用超 1.4 亿次；元宝回答带来源引用、可追溯，点击引用卡片跳转 ima 原文并一键加入个人知识库；ima 也已进入 WorkBuddy、QClaw 等产品形成统一知识上下文层 — [IT之家 2026-06-15](https://www.ithome.com/0/964/468.htm); [华尔街见闻](https://wallstreetcn.com/articles/3774709)
- WorkBuddy：2026-03-09 上线（腾讯云 CodeBuddy 团队），桌面 AI 办公智能体，把指令拆为多步、处理本地文件、交付结果；国内版可切换混元/DeepSeek/GLM/Kimi/MiniMax；支持 MCP，接 QQ/飞书/钉钉、GitHub/Notion/Gmail；完全兼容 OpenClaw skills；2026-05-29 面向全球用户发布 — [TechNode 2026-05-29](https://technode.com/2026/05/29/tencent-launches-workbuddy-productivity-ai-agent-for-global-users/); [IT之家](https://www.ithome.com/0/927/896.htm)
- 第三方教程把"深度研究：对复杂问题进行深度调研并输出报告"列为 WorkBuddy 日常办公场景之一（非官方文档） — [aicoding.csdn.net](https://aicoding.csdn.net/6a4cb29e662f9a54cb8a9a55.html)
- 沙利文 2026-09-20《全球桌面 AI 智能体市场研究报告》将 WorkBuddy 列为中国个人端与企业级桌面智能体双榜第一；花旗 2026-07 研报引用腾讯管理层数据：跨平台 MAU 2,000 万、DAU 超 1,300 万（均为腾讯口径） — [扬子晚报 2026-09](https://www.yzwb.net/news/ch/202609/t20260920_394585.html); [itiger/Citi](https://www.itiger.com/news/1125565486)

**阿里：夸克 深度搜索 / 通义（Qwen）DeepResearch**
- 夸克 2025-05 推出"深度搜索"，并预告"深度搜索 Pro"，流程为"思考、搜索、验证、再思考"，几分钟给出研究报告；2025-07 夸克"志愿报告"Agent 采用"任务规划—执行—检查—反思"，累计生成超 1,000 万份志愿报告 — [量子位 2025-05](https://www.qbitai.com/2025/05/281263.html); [品玩 2025-07](https://www.pingwest.com/w/305924)
- Qwen DeepResearch 2511（2025-11-13 官方博客）：多智能体协同（需求分析、整体策略规划、搜索验证、并行工具调用、网页阅读分析、报告撰写），带记忆管理与研究状态跟踪；Normal/Advanced 两种模式；支持上传 PDF/Excel/图片；输出报告 + 一键生成网页与播客；"更直观的引用标注"；评测维度含反幻觉率、全面性、可靠性、指令遵循、报告长度、搜索深度、研究耗时（仅图表无数字）；入口 chat.qwen.ai — [docs.qwenlm.ai](https://docs.qwenlm.ai/qwen-deepresearch/index.html)
- AIbase 报道：系统先分析需求并确认细节，再起草多步研究计划，然后搜索、分析、整合；QwenChat 免费试用 — [AIbase](https://news.aibase.com/zh/news/18072)
- 阿里云百炼 Qwen-Deep-Research API：两步调用，第一次返回澄清问题，第二次传入用户回答并启动研究；仅华北（北京）区域、仅 Python DashScope SDK，不支持 OpenAI 兼容接口 — [help.aliyun.com](https://help.aliyun.com/en/model-studio/qwen-deep-research-api)
- Tongyi DeepResearch 开源模型（见第 5 节架构）。

**豆包**
- 2025-06-30"深入研究"在 App/网页/电脑版开放测试，免费；结果以"报告文档"或"可视化网页"两种形式呈现；App 中可点右上角"听"一键转播客 — [IT之家 2025-06-30](https://www.ithome.com/0/864/734.htm); [凤凰网](https://tech.ifeng.com/c/8kbdTpTCXIn)
- 豆包"深度思考（边想边搜）"：以思考驱动搜索，推理中按需多轮动态搜索；示例中一次查到 14 篇参考资料；回答含风险提示、对比表格；文章未说明来源如何呈现（这是另一功能，非"深入研究"） — [爱范儿](https://www.ifanr.com/1618920)

**扣子空间（字节）**
- 2025-04 推出，定位类 Manus 的通用 Agent 协作平台，邀请码内测；可与"通用实习生"或行业"领域专家"协作 — [人人都是产品经理](https://www.woshipm.com/ai/6208651.html)
- 实测：可生成 Markdown 报告、图文网页、Office 文档、PDF、飞书文档；默认"探索模式"（快），可切"规划模式"展示中间步骤便于随时调整；PPT 流程为粘贴内容 → 选风格 → 修改大纲 → 生成 → 在线编辑 → 下载 .pptx；7 页演示约 4 分钟；作者指出用户仍需较强 AI 素养才能精确定义任务 — [钛媒体](https://www.tmtpost.com/7549564.html); [少数派](https://sspai.com/post/98657); [ai-bot.cn coze-ppt](https://ai-bot.cn/coze-ppt/)

**MiniMax Agent**
- 官方：面向长程复杂任务的通用智能体，专家级多步规划、拆解并执行多个子任务，交付端到端方案；发布前内部使用约 60 天 — [minimax.io/news/minimax-agent](https://minimax.io/news/minimax-agent)
- MIT AI Agent Index：宣传用途含 Code、PPT、Deep Research、多模态、MCP；按积分计费订阅 — [aiagentindex.mit.edu](https://aiagentindex.mit.edu/2025/minimax-agent)
- 2026-01 更新推出桌面版（本地 + 云端）；MaxClaw 云端平台 — [weel.co.jp（日文第三方）](https://weel.co.jp/media/tech/minimax-agent/)

**智谱 AutoGLM 沉思**
- 2025-03-31 中关村论坛发布，兼具 Deep Research 与 Operator 能力：逐步分解问题，自动打开浏览器标签页搜索、记录、汇总，生成报告；可操作小红书等平台 — [投资界](https://news.pedaily.cn/202503/547756.shtml); [爱范儿](https://www.ifanr.com/1619258)
- 2025-04-11 产品调研实测：先弹"实验阶段"声明并要求确认品牌范围（澄清）；生成搜索关键词 → 常规搜索 → AutoGLM 搜索 → 工具调用 → 推理 → 总结；自动拉起 Chrome 可视操作（小红书把筛选从"综合"改为"最热"）；信源含小红书、知乎、抖音、微博、百度；Google Play 数据取不到；首次小红书搜索失败后回退到搜索引擎；报告"大而全、泛泛而谈"，缺定价等关键数据；文章未提及引用标注（对比 ChatGPT 会链接关键结论）；自动弹窗干扰多任务 — [人人都是产品经理 2025-04-11](https://www.woshipm.com/ai/6202270.html)

### Inferences
- 中国产品的差异化在"过程可视化"（秘塔的子问题状态着色 绿/紫/红 是一种天然的"事实 vs 待定 vs 缺失"UI）和"交付物多模态"（天工五模态、Kimi HTML 报告、豆包播客），而不是前置 HITL；可编辑计划这一环在中国产品中几乎没有官方文档支撑（Qwen/天工的"确认"更接近一轮问答）。
- 天工"正文链接到来源具体段落、图片链接到知识源"是目前看到的最细粒度引用模型，适合作为"研究 → PPT"时引用随内容迁移的参考。
- WorkBuddy 的"深度研究"目前更像桌面 Agent 的一个技能（skill）而非独立的研究产品，公开资料没有专门的研究 UX 描述；其优势是 ima 知识库 + 微信生态的信源接入。

### Gaps
- 未找到 WorkBuddy 深度研究的官方功能说明（澄清、计划、引用形式、导出）或任何独立实测。
- 未找到 元宝 在 2025–2026 年是否有独立"深度研究"模式的官方公告（只有 2024 的"深度搜索"与 2026 的 ima 信源接入）。
- 未找到 扣子空间、MiniMax Agent、豆包"深入研究"的官方引用机制说明（是否内联、是否可跳转）。
- 纳米AI 的"多模型协作"仅见第三方站点描述，官方稿未提。
- 夸克"深度搜索"与通义/Qwen DeepResearch 的技术关系无官方说明。

---

## 3. 事实 vs 推断、信源可信度、冲突信源、时效性与"验证/置信度"UI

### Takeaway
没有任何主流产品在报告 UI 中显式标注"这是推断"或给出句级置信度；现有做法是过程层面的（秘塔子问题三色状态、Gemini 多轮自我批判与"识别不一致"、Grok"权衡冲突事实"、Tongyi/LangChain 的压缩步骤要求带引用），并把验证责任交给"引用可点击核对"。学术工具（Scite 的支持/反驳标注、Consensus Meter）是唯一在产品层把"证据一致性"可视化的例子。引用幻觉有量化证据：深度研究 Agent 的引用 URL 中 3–13% 完全虚构、5–18% 无法解析。

### Cited Findings
- Gemini Deep Research 在综合阶段"评估信源、识别主题与不一致、多轮自我批判" — [gemini.google](https://gemini.google/overview/deep-research/)
- Grok DeepSearch 被描述为"权衡冲突的事实与观点" — [x.ai/news/grok-3](https://x.ai/news/grok-3)
- 秘塔：子问题结论着色 绿=已得结论 / 紫=完善中 / 红=信息缺失 — [量子位](https://www.qbitai.com/2025/07/309591.html)
- Anthropic 多代理系统：研究结束后由独立的 CitationAgent 处理文档与草稿，把每条论断对应到来源；子代理 prompt 内置"信源质量启发式"，引导其偏向权威来源而非 SEO 内容农场；人工测试发现自动评估漏掉的问题，包括"偏好 SEO 内容农场"和"对罕见问题产生幻觉答案" — [anthropic.com/engineering](https://www.anthropic.com/engineering/built-multi-agent-research-system)
- LangChain Open Deep Research：子代理结束时做最后一次 LLM 调用"写出带引用的详细答案"，再经清洗步骤去除原始抓取页面、失败的工具调用与无关内容后才交回 supervisor — [langchain.com/blog](https://www.langchain.com/blog/open-deep-research)
- Tongyi DeepResearch 的 Visit 工具：Jina 解析页面后用"summary model 按指定目标抽取相关内容"；Heavy 模式由综合模型合并多个并行代理的压缩报告 — [arXiv 2510.24701v3](https://arxiv.org/html/2510.24701v3)
- Scite 展示后续论文对某结论的"支持/反驳"；Consensus Meter 展示研究如何在某个论断上分歧 — [paperguide.ai](https://paperguide.ai/blog/elicit-vs-consensus/)
- 引用幻觉量化：跨 10 个模型/Agent 在 DRBench 上，3–13% 的引用 URL 为虚构（无 Wayback 记录），5–18% 无法解析；深度研究 Agent 每问引用更多、但 URL 幻觉率更高（预印本） — [arXiv 2604.03173](https://arxiv.org/pdf/2604.03173)
- ReportBench 人工审计发现两类失败：陈述幻觉（引用来源并不支持该论断）与引用幻觉（参考文献捏造）；例子包括 OpenAI Deep Research 把作者归给并不在论文作者列表的人、引用不存在的 URL — [arXiv 2508.15804](https://arxiv.org/pdf/2508.15804)
- DeepResearch Bench（100 个博士级任务、22 领域）：RACE 评报告质量，FACT 评"有效引用数"与"引用准确率"；Gemini-2.5-Pro Deep Research 平均 111.21 条有效引用显著领先，但 Gemini 与 OpenAI 的引用准确率相比 Perplexity Deep Research 有明显差距；Claude-3.7-Sonnet（带搜索）有效引用第二、引用准确率强 — [arXiv 2506.11763](https://arxiv.org/pdf/2506.11763)
- 后续论文指出 FACT 指标随时间不可靠：被引网页会 404 — [arXiv 2512.01948](https://arxiv.org/pdf/2512.01948)
- Simon Willison：核对引用能捕获大部分幻觉；"遗漏"更危险；报告体的排版会制造虚假权威感；Deep Research 的思维链曾对自身工具访问能力产生幻觉 — [simonwillison.net](https://simonwillison.net/b/8511)
- OpenAI Deep Research 完成后提供"sources used"区块专用于核对引用、"activity history"展示研究轨迹 — [OpenAI Help（搜索摘要）](https://help.openai.com/en/articles/10500283-deep-research)
- Anthropic 的 LLM-as-judge 评分维度：事实准确性、引用准确性、完整性、信源质量、工具效率；单 prompt 输出 0.0–1.0 分 + pass/fail 与人工判断最一致 — [anthropic.com/engineering](https://www.anthropic.com/engineering/built-multi-agent-research-system)

### Inferences
- "引用可核对"是行业默认的验证 UI，"置信度/推断标注"仍是空白；秘塔的三色状态 + Scite 的支持/反驳是两个可以直接借鉴的可视化原语：前者用于研究进程（子问题级），后者用于论断（claim 级）。
- 要在产品层区分事实与推断，技术上需要在压缩/写作阶段强制结构化输出（claim、support_urls、quote span、confidence），而不是事后让 LLM 标注——Anthropic 的"独立 CitationAgent"与 LangChain 的"带引用的压缩步骤"都把引用绑定前移到研究阶段，正是为此。
- 引用准确率与引用数量呈反向关系（DeepResearch Bench、2604.03173），因此产品应把"少而准"作为默认，并对每个 URL 做可达性与内容包含性校验（抓取页面并确认引文存在），否则 3–13% 的虚构 URL 会直接损害信任。

### Gaps
- 未找到任何产品公开的"置信度评分"或"推断标注"UI。
- 未找到各产品在"信源冲突时如何在报告中呈现"的官方说明（仅有"识别不一致""权衡冲突"的笼统描述）。
- 时效性（freshness）处理无官方文档；Parallel 的博客称 Exa 在时效性查询上准确率 24%，属供应商口径。

---

## 4. 输出格式与下游交付物（PPT / 信息图 / 营销内容）

### Takeaway
报告输出以 Markdown/全屏报告视图 + PDF/Word 导出为基线（OpenAI、Kimi、秘塔、Gemini），中国产品额外标配"交互式网页"（秘塔、Kimi、豆包、天工、Qwen）与"播客"（豆包、天工、Qwen、Gemini）；"研究 → PPT"已有四条成熟路径：天工（研究即 PPT 并带溯源）、NotebookLM（报告与全部来源入库 → Slide Deck/Infographic，可逐页 Revise）、Perplexity Labs/Genspark/Manus Slides（整体生成），以及扣子空间（大纲确认 → 生成 → 在线编辑 → pptx）。营销文案类下游没有看到产品级"研究 → 文案"的专门流程。

### Cited Findings
- OpenAI：完成后全屏报告视图（目录、sources used、activity history） — [OpenAI Help（搜索摘要）](https://help.openai.com/en/articles/10500283-deep-research)
- Gemini：报告可在 Canvas 转为互动内容、测验、Audio Overview；Canvas 可导出 Google Docs；第三方描述可生成网页/信息图/闪卡；API 的 `visualization: auto` 可在 prompt 要求时产出图表/图片 — [gemini.google](https://gemini.google/overview/deep-research/); [GSMArena](https://m.gsmarena.com/gemini_now_turns_your_documents_into_podcasts_collaborates_with_you_too-amp-67010.php); [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research)
- NotebookLM：Deep Research 结束后报告与全部来源进入 notebook → Slide Decks / Infographics（Nano Banana Pro）；单页 Revise；支持品牌规范作为 source；原生 PPTX 导出（第三方报道） — [christopherspenn.com](https://www.christopherspenn.com/2026/02/notebooklms-game-changing-upgrade-create-stunning-infographics-slides-deep-research-in-one-powerful-ai-tool/); [levelup.gitconnected.com](https://levelup.gitconnected.com/whats-new-in-notebooklm-2026-a-deep-dive-into-its-latest-features-4c73ac6dee8a); [taxheal 转载](https://www.taxheal.com/?p=110288)
- Kimi：Markdown 报告（导出 PDF/Word）+ 交互式 HTML（预览/复制源码/公开链接）；功能页列出 PDF/PPT/Word/Excel/HTML — [kimi.com 帮助](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview); [kimi.com 功能页](https://www.kimi.com/zh-hans/features/deep-research)
- 秘塔：报告一键转交互网页（自动排版 + 抓图）、导出 Word/PDF — [量子位](https://www.qbitai.com/2025/07/309591.html)
- 天工：文档/PPT/表格/网页/播客五模态，PPT 在线编辑、导出 pptx/pdf，带溯源；表格智能体通过网络搜索而非上传文件收集数据，示例产出六个 sheet + 图表 — [36氪](https://www.36kr.com/p/3303246919228167); [AIbase](https://news.aibase.com/zh/news/18256)
- 豆包：报告文档或可视化网页，一键转播客 — [IT之家](https://www.ithome.com/0/864/734.htm)
- Qwen DeepResearch 2511：报告 + 一键网页/播客 — [docs.qwenlm.ai](https://docs.qwenlm.ai/qwen-deepresearch/index.html)
- 扣子空间：Markdown/网页/Office/PDF/飞书文档；PPT 需先确认大纲，生成后在线改文字换图、下载 .pptx — [钛媒体](https://www.tmtpost.com/7549564.html); [ai-bot.cn](https://ai-bot.cn/coze-ppt/)
- 纳米AI：一句话生成视频/报告/PPT（第三方站点） — [aitoollab.cn](https://www.aitoollab.cn/tools/nano-ai-search/)
- Perplexity Labs：报告、表格、仪表盘、web app；Assets/App 标签（搜索摘要）；2026 第三方称 Deep Research 可直接产出演示文稿 — [Perplexity Labs（搜索摘要）](https://www.perplexity.ai/hub/blog/introducing-perplexity-labs); [beginnersinai.org](https://beginnersinai.org/whats-new-perplexity-2026/)
- Genspark：Sparkpages 一页式报告 + AI Slides（评测称约 60 秒生成 10 页 deck） — [eesel.ai](https://www.eesel.ai/blog/genspark-ai-review)
- Manus：Manus Slides / PowerPoint Slides 博客条目 — [manus.im/zh-cn/blog/product](https://manus.im/zh-cn/blog/product)
- 百度文心站：一键 AI PPT 与"深入研究"并列为同一站点功能 — [科技日报 2026-06-25](https://www.stdaily.com/web/gdxw/2026-06/25/content_537227.html)
- DeerFlow 2.0：生成文件落在 `/mnt/user-data/outputs`，由 `present_files` 交付 — [DeerFlow README](https://github.com/bytedance/deer-flow)

### Inferences
- "研究 → PPT"目前有两种模式：(a) 天工/NotebookLM 式——研究产物（报告 + 来源集合）成为下游的 source，引用随之迁移；(b) Genspark/Manus/Perplexity Labs 式——Agent 一次性生成 deck，引用基本丢失。对需要"可追溯"的产品调研 PPT，(a) 更可取。
- "交互式 HTML 报告"在中国市场已是默认期望（秘塔、Kimi、豆包、Qwen、天工），应视为与 Markdown 并列的一等输出。
- 没有产品级的"研究 → 营销文案"流程，是可切入的空白；但要避免 Simon Willison 指出的"报告体伪权威"，文案侧更需要显式的事实/推断分层。

### Gaps
- 未找到任何产品把"引用"保留进 PPT 讲者备注或幻灯片脚注的官方说明（天工称"带溯源"但未见具体呈现方式的文档）。
- NotebookLM 的 PPTX 原生导出仅见第三方报道，未找到 Google 官方说明。

---

## 5. 已知技术架构模式（多代理研究、搜索工具、引用处理、上下文压缩）

### Takeaway
2026 年已收敛的参考架构是"Scope（澄清 + 研究简报）→ Supervisor/Lead 并行派发子研究代理（各自独立上下文、迭代搜索、结束时压缩为带引用的 findings）→ 单次写作（或独立引用代理）"。Anthropic、LangChain Open Deep Research、GPT Researcher、Tongyi Heavy 模式、Skywork 的 Agent Workflow 本质相同；差异在 HITL 位置（LangChain 的澄清在 scope、DeerFlow 的计划确认在 planner 后）、上下文管理（Tongyi 的"每步重建工作区"、Anthropic 的外部记忆 + 文件系统引用）与引用绑定时机（研究期 vs 写作后）。

### Cited Findings

**Anthropic 多代理研究系统（2025-06）**
- Orchestrator-worker：Lead（Opus 4）制定策略并并行派发 3–5 个子代理（Sonnet 4），子代理各自 3+ 工具并行调用，复杂查询总耗时降低最多 90%；Lead 把计划存到外部记忆（上下文超 200k 会截断）；研究充分后由 CitationAgent 把论断绑定来源 — [anthropic.com/engineering](https://www.anthropic.com/engineering/built-multi-agent-research-system)
- 子代理搜索策略：先短而宽的查询，看可用内容后再收窄；每个工具结果后用交错思考判断质量与缺口；"文件系统/artifact 模式"让子代理把产出存外部、只回传轻量引用，减少信息损失与 token 复制 — 同上
- 努力度分级：简单事实 1 代理 3–10 次工具调用；直接比较 2–4 子代理各 10–15 次；复杂研究 10+ 子代理分工 — 同上
- 经济性：Agent 约 4× 聊天 token，多代理约 15×；多代理比单 Opus 4 在内部评估中高 90.2%；BrowseComp 上 token 用量解释 80% 的性能方差，三因素（token、工具调用次数、模型）解释 95% — 同上
- 失败模式：过度派生 50 个子代理、无止境搜索不存在的来源、超量更新干扰、超过充分性后继续、冗长查询返回少、同步等待批次阻塞中途引导（异步执行是计划改进） — 同上
- 工程：从检查点恢复而非重启 + 重试；rainbow deployment 保护运行中的 Agent；生产追踪决策模式而不读对话内容 — 同上

**LangChain Open Deep Research（LangGraph，2025）**
- 三阶段：Scope（用户澄清——"用户很少一开始给足上下文"——然后把对话压缩为研究简报，作为后续"北极星"）→ Research（supervisor 判断是否拆为独立子主题并行派子代理；子代理各自上下文窗口内做 tool-calling 循环，搜索工具/MCP 可配；结束时 LLM 写"带引用的详细答案"+ 清洗步骤；supervisor 对照简报决定是否再派）→ Write（单次 LLM 调用用简报 + 全部 findings 一次写完） — [langchain.com/blog](https://www.langchain.com/blog/open-deep-research)
- 教训：早期版本让子代理并行写各章节导致报告割裂，改为只在研究阶段多代理、写作一次成稿；单代理在"三家实验室对比"中把三个主题混在一个上下文里研究得更浅；supervisor 决定深度以避免简单问题跑 10+ 分钟；开放问题：token 重的工具响应过滤、在热路径跑评估、长期记忆复用报告 — 同上

**DeerFlow（字节，LangGraph）**
- 2.0 是"从头重写"的"super agent harness"，与 1.x 无共享代码（1.x 在 `1.x` 分支维护）；运行时在 Gateway API 内，`/api/langgraph/*` 路由；HITL 用静态 `interrupt_before/after` 断点，动态中断用 `command.resume`；搜索提供方：Brave、DuckDuckGo、Tavily、Serper、SearXNG、InfoQuest、Sofya、GroundRoute、腾讯云 WSA、Serply；抓取：Jina、Browserless、Crawl4AI、Firecrawl、fastCRW、Unbrowse、InfoQuest；MCP 支持 OAuth、`tool_search` 延迟发现；沙箱：本地/Docker/K8s/E2B 等；可委托给外部 ACP agent（如 MiniMax Code）；产出文件经 `present_files` 交付 — [github.com/bytedance/deer-flow](https://github.com/bytedance/deer-flow)
- 注：1.x README（含 coordinator → planner → 人工计划确认 → researcher/coder → reporter，播客/PPT/散文输出）本次抓取 404，相关细节见 Inferences。

**Tongyi DeepResearch（阿里，开源，2025-10）**
- 30.5B 总参 / 3.3B 激活、128K 上下文、Apache-2.0；两种推理模式：ReAct 与 IterResearch 式 Heavy（n 个并行代理各自产出压缩报告 + 答案，由综合模型合并）；上下文管理：每步只基于"重建的工作区"（问题 + 作为压缩记忆的演进报告 + 最近一次动作/观察）而非全历史；工具：Search（Google，每查询前 10 条，多查询并发）、Visit（Jina 解析 + 摘要模型按目标抽取）、Python 沙箱、Google Scholar、File Parser；数据合成引用 WebSailor/WebShaper；分数（ReAct → Heavy）：HLE 32.9 → 38.3，BrowseComp 43.4 → 58.3，BrowseComp-ZH 46.7 → 58.1，xbench-DeepSearch 75.0，FRAMES 90.6 — [arXiv 2510.24701v3](https://arxiv.org/html/2510.24701v3); [GitHub Alibaba-NLP/DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)
- 仓库工具栈：Serper（搜索 + Scholar）、Jina（网页）、OpenAI 兼容摘要 API、DashScope 文件解析、SandboxFusion 代码执行 — [GitHub](https://github.com/Alibaba-NLP/DeepResearch)

**GPT Researcher**
- Planner 生成研究问题 → 执行代理按问题检索（聚合 20+ 网络来源）→ planner 过滤、聚合、写报告；每个抓取来源先摘要并追踪出处；平均约 3 分钟、约 $0.1；导出 PDF/Word；用 gpt-4o-mini + gpt-4o 控制成本；另有 LangGraph 多代理版本（Chief Editor + Researcher 子代理，受 STORM 启发）；Deep Research 模式为递归树状、可配深度/广度、约 5 分钟 — [docs.gptr.dev](https://docs.gptr.dev/docs/gpt-researcher/getting-started/introduction); [rywalker.com 总结（第三方）](https://rywalker.com/research/gpt-researcher)

**Hugging Face Open Deep Research（smolagents）**
- smolagents（2024-12-30）核心循环约千行单文件，代理输出可执行 Python 而非 JSON 工具调用；Open Deep Research 为其 examples 下对 OpenAI Deep Research 的复现，2025-02 GAIA 验证集 pass@1 55.15%（OpenAI 67.36%） — [morphllm.com（第三方）](https://www.morphllm.com/smolagents); [kingy.ai（第三方）](https://kingy.ai/blog/smolagents-by-hugging-face-comprehensive-guide-to-a-minimalistic-ai-agent-framework/)

**Perplexica**
- 开源答案引擎：UI + 决定下一步的 agents/chains + SearXNG 元搜索 + LLM + 嵌入模型重排；结果向量化做相似度检索，top 来源交给生成器并引用；Copilot 模式（多查询）开发中；六种 focus 模式（学术、YouTube 等） — [the-decoder](https://the-decoder.com/perplexica-is-an-open-source-ai-search-engine-alternative-to-perplexity-ai); [GitHub 镜像](https://github.com/OmarElKadri/Perplexica)

**Skywork（天工）**
- Agent Workflow：任务拆解、上下文管理、子任务派发给文档/表格等专门代理，每个子任务各自独立上下文跑 Deep Research；"引用、反思、检查"工作流；开源 SkyworkAI/DeepResearchAgent；文档/表格/PPT 三个 MCP — [36氪](https://www.36kr.com/p/3303246919228167)

**Qwen DeepResearch 2511**
- 多代理协同：需求分析、策略规划、搜索验证、并行工具调用、网页阅读、报告撰写；记忆管理 + 研究状态跟踪 — [docs.qwenlm.ai](https://docs.qwenlm.ai/qwen-deepresearch/index.html)

**Google Deep Research Agent（托管）**
- 可通过 `mcp_server` 与 `file_search` 把私有数据纳入研究；`collaborative_planning` 实现计划确认；流式 `step.delta` 便于把思考/文本/图片渐进渲染到前端；断流需保存 `last_event_id` 重连 — [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research)

### Inferences
- 对 LangGraph 产品而言，最直接可复用的是 LangChain Open Deep Research 的三阶段图：在 Scope 节点插入 `interrupt`（澄清/简报确认），在 Supervisor 派发前插入第二个 `interrupt`（计划确认），在 Write 之后插入第三个（分章节修订）。DeerFlow 2.0 的 `interrupt_before/after` + `command.resume` 正是这套机制的工程实现。
- 基于训练知识（本次未能抓取 1.x README 验证）：DeerFlow 1.x 的图为 coordinator → planner → human_feedback（用户回复 `[ACCEPTED]` 或自然语言修改计划，可配 `auto_accepted_plan`）→ research_team（researcher 用 Tavily/DuckDuckGo/Brave/Arxiv + Jina 抓取；coder 用 Python REPL）→ reporter，支持 `max_plan_iterations`、`max_step_num`、report style、播客（TTS）/PPT（marp）/散文输出、RAGFlow/VikingDB 私有知识。这是"计划确认 HITL"在 LangGraph 中最成熟的公开范例，需按 `1.x` 分支代码复核。
- 上下文压缩的三种可借鉴实现：子代理结束压缩（LangChain/Anthropic）、每步重建工作区（Tongyi）、外部文件系统 + 引用回传（Anthropic）。对"研究 → PPT"场景，第三种最合适：研究 artifact 持久化后，PPT/海报节点读取同一 artifact，而不是把全文塞回上下文。
- 引用绑定应在子代理压缩阶段完成（claim → URL → 引文片段），写作阶段只做重排；事后"CitationAgent"是补救，不是替代。

### Gaps
- DeerFlow 1.x README 与 Perplexity Labs 博文未能直接抓取；SkyworkAI/DeepResearchAgent 仓库未读。
- 未找到 Kimi-Researcher、秘塔深度研究、天工 Deep Research 模型的公开技术报告（仅厂商宣传与 benchmark 图片）。

---

## 6. 搜索 API 与中文网络约束（Tavily / Exa / Parallel / Serper / 博查 / 秘塔 / 腾讯云 WSA / 微信公众号 / 付费行业报告）

### Takeaway
海外三家（Tavily、Exa、Parallel）在 2026 年都提供"搜索 + 抽取 + 深度研究"端点，定价约 $1–15/千次，但互相引用的 benchmark 全为供应商口径；国内可合规使用的 Web Search API 以 博查（Bocha）为代表，秘塔 API（0.03 元/次）与腾讯云 WSA（DeerFlow 2.0 已内置）可作补充。微信公众号内容没有官方对外搜索 API，搜狗微信搜索功能已下线，第三方 MCP 靠搜狗抓取，稳定性与合规性存疑；腾讯自家产品（元宝/ima/WorkBuddy）是唯一有合法公众号信源的路径。付费行业报告与企业数据源无公开 API 证据。

### Cited Findings
- Parallel 2026-10-10 定价对比：Parallel Turbo/Fast 约 $1/千次，一把 key 覆盖抽取与深度研究；Tavily 按积分计费，1,000 次基础搜索约 $5–8；Exa Instant $4/千、Fast/Auto $7、Deep $12–15；Parallel 无 crawl 端点 — [parallel.ai Tavily alternatives](https://parallel.ai/articles/tavily-alternatives); [parallel.ai 四方对比](https://parallel.ai/articles/openai-web-search-vs-parallel-vs-exa-vs-tavily-how-to-choose)
- Parallel 引用 Artificial Analysis Search Index：Parallel basic 73、Tavily basic 66；称 Exa 时效性查询准确率 24%（供应商口径） — [parallel.ai](https://parallel.ai/articles/tavily-alternatives)
- Tavily 自己的 Exa vs Parallel 评测承认 Parallel Advanced 在若干研究任务上可匹敌或超过 Exa 且更便宜，建议自测；Tavily 提供 Extract/Map/Crawl/Research 端点；2026-02 Nebius 宣布收购 Tavily，API 与零数据保留承诺不变 — [tavily.com blog](https://tavily.com/blog/exa-vs-parallel-benchmarking-retrieval-apis-for-ai-agents-in-2026); [parallel.ai](https://parallel.ai/articles/tavily-vs-parallel-search)
- Exa 基于自有嵌入索引，强在语义发现、相似页面、指定 URL 内容 — [parallel.ai](https://parallel.ai/articles/openai-web-search-vs-parallel-vs-exa-vs-tavily-how-to-choose)
- 博查：2024-08 调研时国内"只看到博查一家"提供面向 AI 的 Web Search API（内容合规、价格实惠）；面向国内市场、主打中文效果与数据不出境；覆盖网页/新闻/短视频/百科/天气/火车票等，提供语义重排；后续产品"博查万象"含 Web Search、AI Search（流式、追问）、Semantic Reranker — [bilibili 专栏 2024-08](https://www.bilibili.com/read/cv36692629); [ai-bot.cn](https://ai-bot.cn/sites/76040.html)
- 秘塔搜索 API：0.03 元/次，网页/图片/视频/文库 + 全文获取 + 问答接口 — [AIbase](https://news.aibase.com/zh/news/19928)
- 腾讯云联网搜索（WSA）：需在控制台开通并配置 `WSA_API_KEY`，用于让大模型检索公网信息 — [cloud.tencent.com 文档](https://cloud.tencent.com/document/product/1552/132746)
- DeerFlow 2.0 内置搜索提供方含 Tencent Cloud WSA、Serper、Tavily、Brave、SearXNG 等 — [DeerFlow README](https://github.com/bytedance/deer-flow)
- Tongyi DeepResearch 仓库用 Serper（搜索 + Scholar）+ Jina（网页） — [GitHub](https://github.com/Alibaba-NLP/DeepResearch)
- 微信公众号：搜狗微信搜索的订阅公众号、关键词、收藏文章功能下线（搜狗未公布原因）；第三方方案：JustOneAPI 公众号搜索接口、wxmp（需公众平台登录 cookies）、Weixin Search MCP（通过搜狗微信搜索抓取）；搜狗与微信内容合作条款约 2019 年到期后抓取受限 — [钛媒体](https://www.tmtpost.com/2578037.html); [lobehub weixin_search_mcp](https://lobehub.com/zh/mcp/fancyboi999-weixin_search_mcp); [pypi wxmp](https://pypi.org/project/wxmp/)
- 2025-07 微信 AI 搜索被指"开盒"，腾讯回应仅整合公众号及互联网公开信息；"推文内人名超链接"功能已取消 — [IT之家](https://www.ithome.com/0/865/280.htm)
- 元宝/ima/WorkBuddy 共享 ima 知识库信源，元宝引用可跳转 ima 原文 — [IT之家 2026-06-15](https://www.ithome.com/0/964/468.htm)
- AutoGLM 沉思用浏览器操作绕过 API 限制访问小红书、知乎、抖音、微博、百度；但首次小红书搜索失败、Google Play 取不到 — [人人都是产品经理](https://www.woshipm.com/ai/6202270.html)
- 秘塔搜索范围含"文库"与"学术"；Kimi 功能页称覆盖政府、学术、企业数据库、实时金融数据（未列具体库名，帮助页建议用户自行在提问中指定 PubMed/arXiv） — [量子位](https://www.qbitai.com/2025/07/309591.html); [kimi.com](https://www.kimi.com/zh-hans/features/deep-research)

### Inferences
- 中文产品调研的信源栈建议：博查/秘塔 API 做主搜索，腾讯云 WSA 做备份，Serper/Tavily 做英文补充；公众号内容只能通过"用户上传/粘贴"或接入腾讯生态产品间接获得，产品设计上应把"用户补充信源（上传 PDF、粘贴链接/全文）"作为一等 HITL 入口（OpenAI 与 Gemini 均已提供）。
- 艾瑞/易观等付费报告、企查查类企业数据无公开 API 证据，产品应提供"受信域名白名单 + 用户自备资料"而非承诺自动获取。
- Grok API 的 `allowed_domains`（最多 5 个）与 OpenAI 的"filter websites"说明"限定可信域名"是成熟需求，但上限很低；自建产品可做更大白名单并在引用处标注域名等级。

### Gaps
- 未找到 2026 年 博查/秘塔 的最新定价与限额（仅 2024–2025 报道）。
- 未找到国家统计局、艾瑞、易观、企查查等数据源在任何深度研究产品中的官方接入说明。
- 未找到腾讯云是否对外开放公众号内容搜索 API 的官方文档。

---

## 7. 定价 / 限额 / 已知弱点与基准

### Takeaway
限额体系分三类：按次（OpenAI 10/15/125+125）、按 token/配额（Kimi、Claude 与聊天共享、Genspark 积分）、免费（秘塔、豆包、Qwen 试用）。公认弱点：引用幻觉（3–13% 虚构 URL）、偏好 SEO 内容农场、报告过长且"大而全"、遗漏比错误更难发现、同步批次阻塞导致中途难以引导、积分计费不透明（Genspark 1.7 星）。

### Cited Findings
- OpenAI 限额：Free 5 轻量/月；Plus/Team 10 完整 + 15 轻量；Pro 125 + 125；2026-04 新增 $100 档（第三方） — [the-decoder](https://the-decoder.com/deep-research-feature-now-available-to-free-chatgpt-users/); [cometapi](https://www.cometapi.com/chatgpt-pricing-2026-free-vs-go-vs-plus-vs-pro/)
- Claude Research：Pro/Max/Team/Enterprise，与普通对话共享用量，研究会更快耗尽配额 — [support.claude.com](https://support.claude.com/en/articles/11088861-using-research-on-claude-ai)
- Gemini：150 国、45+ 语言；2025-03 免费用户每月 5 份报告（当时口径） — [gemini.google](https://gemini.google/overview/deep-research/); [Yahoo Tech](https://tech.yahoo.com/general/articles/notebooklms-audio-overviews-home-within-194732135.html)
- Kimi：Moderato 一次任务约 5–10% 月配额，失败退还；按 token 计量 — [kimi.com 帮助](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview)
- 秘塔深度研究免费；豆包深入研究免费测试；Qwen DeepResearch 免费试用；AutoGLM 沉思（第三方称）免费不限量 — [量子位](https://www.qbitai.com/2025/07/309591.html); [IT之家](https://www.ithome.com/0/864/734.htm); [AIbase](https://news.aibase.com/zh/news/18072); [ai-bot.cn](https://ai-bot.cn/autoglm-research/)
- Genspark：Plus $24.99 / Pro $249.99，积分不透明，Trustpilot 1.7 星 — [eesel.ai](https://www.eesel.ai/blog/genspark-ai-review)
- Manus Wide Research 先 Pro 后 Plus/Basic — [manus.im](https://manus.im/blog/introducing-wide-research)
- GPT Researcher 约 $0.1/报告、3 分钟 — [docs.gptr.dev](https://docs.gptr.dev/docs/gpt-researcher/getting-started/introduction)
- Anthropic：多代理 15× token，同步批次阻塞中途引导 — [anthropic.com/engineering](https://www.anthropic.com/engineering/built-multi-agent-research-system)
- LangChain：用户不希望简单问题跑 10+ 分钟 — [langchain.com/blog](https://www.langchain.com/blog/open-deep-research)
- Kimi 帮助：不建议中途"停止输出"，长而不清晰的输入会带偏方向 — [kimi.com 帮助](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview)
- AutoGLM 实测：报告"大而全、泛泛而谈"、关键数据缺失、弹窗干扰 — [人人都是产品经理](https://www.woshipm.com/ai/6202270.html)
- 引用幻觉与 SEO 农场偏好：见第 3 节（arXiv 2604.03173、2508.15804、Anthropic 博客、Simon Willison）
- 基准：Tongyi Heavy BrowseComp 58.3；OpenAI 完整版 51.5 / 轻量 45.6；Skywork v2 27.8 → 38.7（并行思考）；HF ODR GAIA 55.15 vs OpenAI 67.36；DeepResearch Bench 100 题 22 领域 — 分别见 [arXiv 2510.24701](https://arxiv.org/html/2510.24701v3); [the-decoder](https://the-decoder.com/deep-research-feature-now-available-to-free-chatgpt-users/); [AIbase](https://news.aibase.com/zh/news/20505); [morphllm](https://www.morphllm.com/smolagents); [arXiv 2506.11763](https://arxiv.org/pdf/2506.11763)
- 元宝早期用户反馈："不少 AI 搜索尝鲜者用了一阵就换回传统搜索，质量参差是主要顾虑" — [人人都是产品经理](https://www.woshipm.com/share/6206593.html)

### Inferences
- 自建产品若用 API（o3-deep-research / Gemini Deep Research Agent / Qwen-Deep-Research）需面对"按任务计费且单任务成本不透明"的问题；采用 LangGraph 自建 + 搜索 API（$1–8/千次）+ 自选模型可把单次调研成本控制在美元级（GPT Researcher 的 $0.1 为下限参考），并能自定义深度分级（Anthropic 的 1/2–4/10+ 代理分级）。
- "中途无法引导"是所有托管产品的共同短板（OpenAI 仅允许中断后调整信源），LangGraph 的 `interrupt` 可在"每轮 supervisor 派发前"暴露一个可选检查点，形成差异化。

### Gaps
- 2026-10 当下各家精确限额多数未能从官方页面核实（403）。
- 未找到独立的中国产品横评（秘塔 vs Kimi vs 天工 vs Qwen）在引用准确率上的量化数据。

---

## 8. 对 LangGraph + HITL 产品（海报 + 调研 + PPT）设计的可借鉴机制汇总

### Takeaway
把上述产品机制映射到 LangGraph 节点与检查点，可得到一条有明确先例支撑的流程：澄清（一轮、可跳过）→ 研究简报 → 可编辑计划卡（增删子问题、限定域名、上传资料）→ 并行子研究（子问题三色状态流式展示）→ 带引用压缩 → 一次成稿 + 引用校验 → 章节级修订 → 以"报告 + 来源集合"为 source 生成 PPT/海报/文案并保留溯源。

### Cited Findings
- 一轮澄清且可跳过：Kimi"做个全面的研究" — [kimi.com 帮助](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview)
- 可编辑计划卡：Gemini 应用端 + API `collaborative_planning` — [gemini.google](https://gemini.google/overview/deep-research/); [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research)
- 过滤网站 / 补充信源 / 运行中调整信源：OpenAI — [OpenAI Help（搜索摘要）](https://help.openai.com/en/articles/10500283-deep-research)
- 域名白名单：Grok `allowed_domains` — [docs.x.ai](https://docs.x.ai/docs/guides/live-search)
- 私有资料纳入研究：Gemini `file_search`/`mcp_server`、Claude connectors、秘塔"我的"、Qwen 上传 PDF/Excel/图片 — [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research); [support.claude.com](https://support.claude.com/en/articles/11088861-using-research-on-claude-ai); [量子位](https://www.qbitai.com/2025/07/309591.html); [docs.qwenlm.ai](https://docs.qwenlm.ai/qwen-deepresearch/index.html)
- 子问题状态可视化（绿/紫/红）与研究路径图：秘塔 — [量子位](https://www.qbitai.com/2025/07/309591.html)
- 流式思考 + 异步后台 + 完成通知：Gemini（`step.delta`、`background=True`）、Kimi（10–25 分钟后台）、OpenAI（activity history） — [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research); [kimi.com](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview)
- 引用内嵌 + 点击跳转高亮原文：Kimi；正文链接到来源段落、图片链接到知识源：天工 — [kimi.com](https://www.kimi.com/zh-cn/help/deep-research/deep-research-overview); [36氪](https://www.36kr.com/p/3303246919228167)
- 完成后追问不重跑：Gemini API `previous_interaction_id` — [ai.google.dev](https://ai.google.dev/gemini-api/docs/deep-research)
- 单页 Revise 重生成 deck、品牌规范作为 source：NotebookLM（第三方报道） — [christopherspenn.com](https://www.christopherspenn.com/2026/02/notebooklms-game-changing-upgrade-create-stunning-infographics-slides-deep-research-in-one-powerful-ai-tool/)
- PPT 生成前先确认大纲：扣子空间；PPT 前先问主题/类型/风格：天工 — [ai-bot.cn](https://ai-bot.cn/coze-ppt/); [36氪](https://www.36kr.com/p/3303246919228167)
- 研究阶段多代理、写作一次成稿：LangChain ODR — [langchain.com/blog](https://www.langchain.com/blog/open-deep-research)
- 努力度分级与评估 rubric（事实/引用准确性、完整性、信源质量、工具效率）：Anthropic — [anthropic.com/engineering](https://www.anthropic.com/engineering/built-multi-agent-research-system)
- 引用 URL 可达性校验必要性：3–13% 虚构 URL — [arXiv 2604.03173](https://arxiv.org/pdf/2604.03173)

### Inferences
- 建议的 LangGraph 检查点（均用 `interrupt`，可按用户偏好 `auto_accept`）：(1) 澄清节点，一轮、最多 3 问、可跳过；(2) 计划卡节点，子问题列表 + 信源范围（域名白名单、上传资料、中文/英文比例）可编辑；(3) 每轮 supervisor 派发前的可选"继续/收敛"检查点，用于解决"中途无法引导"；(4) 成稿后的章节级修订节点，只重跑受影响子问题的研究与该章节写作；(5) 下游生成前的"PPT 大纲/海报文案"确认节点。
- 事实 vs 推断的分层应作为写作节点的结构化输出约束（每条论断带 `kind: fact|inference`、`sources[]`、`quote`），UI 上用秘塔式颜色与 Scite 式"支持/反驳"标记呈现，并在 PPT 讲者备注与海报脚注中保留来源——这是现有产品都没做到的点。
- 对"研究 → PPT/海报/文案"，采用 NotebookLM/天工式"研究 artifact 作为 source"的模式，由各下游节点读取同一持久化 artifact（Anthropic 文件系统模式），避免把全文塞回上下文并保证引用一致。

### Gaps
- 以上 HITL 节点组合没有任何单一产品完整实现过，缺少直接的用户研究证据说明"多检查点"是否会被用户视为负担（LangChain 仅指出用户不愿等 10+ 分钟，Kimi 指出用户常跳过澄清的倾向未量化）。
