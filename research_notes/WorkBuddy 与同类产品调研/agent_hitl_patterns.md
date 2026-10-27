# 通用办公/内容创作 AI Agent 的 Human-in-the-loop 与多轮修改交互模式调研（截至 2026-10）

> 调研日期：2026-10-27。覆盖全球与中国市场的通用 Agent 产品，以及 LangGraph / LangChain / AG-UI / Vercel AI SDK / OpenAI Agents SDK / Microsoft Agent Framework / MCP / A2UI 等框架。目标是为 LangGraph 产品（海报 + 研究 + PPT）及其前端提供具体的 UI 机制与公开架构细节。
>
> 体例：每个问题下分「Takeaway / Cited Findings（带 URL 的已验证事实）/ Inferences（推断）/ Gaps（未找到可靠来源）」。产品/API 名保留原文。多数中国产品的"交互细节"来自媒体实测而非官方文档，可信度已逐条标注。

---

## 1. 生成前：提问策略——何时问、问几个、用什么 UI 形式

### Takeaway
主流产品正在收敛到「默认不问、按需少问、给选项」：Manus Plan Mode 只在"缺上下文"时先问否则直接出计划；Microsoft Researcher 的追问可用一句 "Go ahead" 跳过；Lovable Plan mode "常常先问澄清问题"但只在有清晰方案时才出计划；Notion/ChatGPT Work/Cowork 则把"问"集中在"高后果动作前的审批"而不是需求澄清。中文社区的设计共识是：工具/上下文能查到的不要问；必须问时每次 1–2 个、给选项、允许自由输入。

### Cited Findings
- Manus Plan Mode（官方博客，2026-07-22）："If the request lacks context, Manus asks questions first. If the path is clear, it generates the plan right away."——即澄清问题是条件触发、不是必经步骤；计划是"a full Markdown document with goals, steps, and constraints"。Plan Mode 为手动开启（web 输入 `/` 选 Plan；移动端 `+` 选 Plan Mode），FAQ 明确 "It's manual-only. Your quick tasks stay quick." — [Manus: Introducing Plan Mode](https://www.manus.im/blog/manus-plan-mode)
- Microsoft 365 Copilot Researcher 官方支持页：Researcher "might ask follow-up questions to better understand what you're looking for"，用户可以回答，也可以直接回复 "Go ahead" 跳过；输出为带引用的结构化报告，可编辑、可用它"start a presentation or document"。页面未说明问题数量与形式。 — [Get started with Researcher](https://support.microsoft.com/topic/e63ab760-f3de-4c47-ae87-dad601b0e9c4)
- Lovable Plan mode 官方文档：Plan mode 会跨多步调查项目（文件、日志），"often asks clarifying questions first"，只有在有清晰实现方案时才写计划，对话可以在没有计划的情况下结束；"Plan mode never modifies your code." — [Lovable Plan mode](https://docs.lovable.dev/features/plan-mode)
- ChatGPT agent（已下线，被 ChatGPT Work 取代）官方帮助页：agent 在需要时"pauses for clarification or confirmation"，并在"actions of consequence"前请求许可；帮助中心现已标注 agent 不再可用，推荐 ChatGPT Work。 — [OpenAI Help: ChatGPT agent](https://help.openai.com/en/articles/11752874-chatgpt-agent)；[Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/)
- ChatGPT Work（2026-07-09 发布，GPT-5.6 系列）："asks before taking important actions"，用户决定它能访问/分享/发送什么、何时算完成；企业管理员可预授权敏感操作。二手来源，未提到需求澄清问题的数量或形式。 — [thecentral.ai: ChatGPT Work explained](https://thecentral.ai/p/chatgpt-work-explained)；官方页（抓取被拒，仅标题可见）[OpenAI](https://openai.com/index/chatgpt-for-your-most-ambitious-work/)
- Notion Agent 官方帮助页把"问"做成三档全局设置：**Always ask**（任何改内容/用连接应用的动作前都问）、**Review for me**（默认；Agent 自审，只在它判定可能不安全时暂停询问）、**Skip all approvals**。设置放在聊天顶部，可在发送前切换，Agent 运行中不可切换；管理员可以禁用 Skip。 — [Notion: Choose how Notion Agent asks for your approval](https://www.notion.com/help/choose-how-notion-agent-asks-for-your-approval)
- Skywork（天工超级智能体，2025-05-22 发布）：第三方搜索摘要称"表格模式……会先询问、补充需求细节，然后生成一个「待办清单」后，才开始正式干活"（来自搜索结果片段，原文未能完整抓取）；而 PConline 的产品介绍稿（2025-05-23）没有提到任何澄清问题，只描述 5 个专家智能体 + 1 个通用智能体、"Edit"按钮在线编辑。 — [知乎：昆仑万维天工超级智能体体验如何？](https://www.zhihu.com/question/1908902853201889095)（片段）；[PConline: 天工超级智能体破冰之旅](https://www.pconline.com.cn/ai/article/919528.html)
- WPS 灵犀（2025-07 WAIC 发布，2026-05 实测）："一句话生成大纲→确认后生成PPT，支持多轮改大纲、换模板"——即把"确认点"放在大纲而不是提问。 — [新浪：WPS AI(灵犀3.0)实测 2026.5](https://k.sina.com.cn/article_7857141524_1d452771401902kwa4.html)；[新华网：金山办公发布 WPS 灵犀](http://www.news.cn/finance/20250728/64d6e8901aa74e7ebc6942049d353b16/c.html)
- 中文社区关于"澄清问题"的设计共识（非官方产品规范）：
  - 只在"指令置信度低于阈值或关键参数缺失导致无法执行"时触发澄清。 — [FineBI: 用AI Agent平台做主动意图澄清](https://www.finebi.com/blog/article/6a3e4615bc3caea587e661ed)
  - "每次只问1-2个关键信息，并且给出选项供用户选择，不要让用户输入开放式答案"。 — [CSDN: 信息不足时让 Agent 主动反问澄清](https://blog.csdn.net/TSG415545321/article/details/162242182)
  - "先用只读工具查清代码库中的事实，再带着选项询问用户偏好"；两种反面：'十万个为什么'与'把问题原样踢回用户'。 — [javabetter: 用户需求模糊时 Agent 怎么澄清](https://javabetter.cn/ai/video/agent-requirement-clarification.html)
  - "就地澄清"模式：在关键决策点提供数量适当的选项，并允许在选项之外自由输入。 — [Siyu's Newsletter: AI Agent 产品交互设计](https://siyujia.net/posts/ai-agent-hci)
- MCP elicitation（2026-07-28 版规范）为"结构化提问"提供了标准 schema：form mode 限定为**扁平对象 + 原始类型**（string/number/boolean/enum，enum 支持 `oneOf`+`title` 单选、`array`+`anyOf` 多选、`minItems/maxItems`、`default`），"Clients that support defaults SHOULD pre-populate form fields"；响应三态 `accept / decline / cancel`；客户端"MUST ... allow users to review and modify their responses before sending"。 — [MCP Elicitation spec](https://modelcontextprotocol.io/specification/latest/client/elicitation)
- LangGraph `interrupt(value, response_schema=...)`（`langgraph>=1.2.12`）：可以给 interrupt 附带 Pydantic/TypedDict/dataclass schema，resume 值会被校验，校验失败抛 `pydantic.ValidationError`，可在同一 thread 重发。 — [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts.md)

### Inferences
- 在"海报 + 研究 + PPT"场景，可把 Manus 的规则直接产品化：**默认不问**；只有"缺少高返工成本信息"（受众、页数/尺寸、品牌素材、语言、交付格式）时才问；问题以 MCP elicitation 式"单选/多选 + 推荐默认值 + 自由输入"卡片呈现，并提供 Researcher 式的 "直接开始" 跳过按钮。
- Notion 的三档审批设置是一种"提问频率"的用户可控旋钮，适合作为设置项（"每步确认 / 关键处确认 / 不打扰"），而不是写死在 prompt 里。
- 没有任何官方文档给出"问题数量上限"的数字；1–2 个/轮是社区经验，不是厂商规范。

### Gaps
- 未找到 Manus/Genspark/Skywork/WorkBuddy 等官方文档说明"问题的 UI 形态（卡片/表单/纯文本）"与"最多问几个"。
- 未找到任何厂商公开的"只问高返工成本问题"的书面准则；最接近的是 Manus Plan Mode FAQ 中"catching misalignment in a plan is much cheaper than fixing a deployed site"的论证。
- Genspark Super Agent 是否会追问：所有评测（readray/lindy/sortwell）均描述"输入即自动分析并执行"，未见追问描述。

---

## 2. 生成前：计划/大纲的呈现、编辑与确认

### Takeaway
"可编辑的 Markdown 计划文档 + Confirm 门"已成为 2026 年的通用范式（Manus Plan Mode、Lovable Plan view、WorkBuddy Plan 模式、扣子空间规划模式、MiniMax Mavis plan 模式）。差异在于：是否手动触发（Manus 手动；扣子按模式）、能否按段评论修改（Lovable 支持选段 Comment）、是否有版本/撤销（Lovable 有 undo/redo 与 `.lovable/plan/` 归档）、以及计划确认后是否仍可改（Manus/Lovable 均可）。

### Cited Findings
- **Manus Plan Mode**（2026-07-22）：暂停→可行性检查→"surfaces the approach as a structured document"（Markdown：goals/steps/constraints）；用户"can click into the document and rewrite anything, and ask Manus to edit it for you"；"Manus will not start building until you confirm or dismiss the plan"；可在任务中途开启："Manus stops, generates a plan for the next phase, and waits for your approval before continuing"；FAQ：确认后的计划可以在同一会话里再改，"Manus adapts its work"；确认前可反复修改。 — [Manus Plan Mode](https://www.manus.im/blog/manus-plan-mode)
- **Lovable Plan mode**（官方文档）：计划在全屏 **Plan view** 展示，内容含 overview、key decisions & assumptions、components/data models/APIs、步骤、可选图表；可直接编辑 Markdown；**选中计划的一部分点 Comment** 只让 Lovable 改那一段；每次修订保存为版本，**undo/redo** 箭头穿梭，旧版本只读，`Back to latest` 返回；**Approve** 按钮在 Plan view 和聊天里的 plan card 上，有未保存编辑时显示 **Save & approve**；**Skip** 不收费；批准后切换到 Build mode，计划文件 `.lovable/plan.md` 归档到 `.lovable/plan/`（按日期命名），之后可随时回到 Plan mode 基于当前代码库生成新计划。 — [Lovable Plan mode](https://docs.lovable.dev/features/plan-mode)；三模式说明 [Lovable modes](https://docs.lovable.dev/features/modes)
- **腾讯 WorkBuddy**（2026-03-09 正式上线）三种模式：Craft「你说我做」、Plan「先想后做」（"复杂任务，先出方案确认再动手"）、Ask「只聊不动手」（不改文件）。教程描述任务自动拆解为"读取数据 → 清洗分析 → 生成图表 → 撰写结论 → 排版 PPT"逐步执行，"生成过程会实时预览"，结果面板有"展示产物、全部文件、变更记录和实时预览"。 — [腾讯云开发者社区: WorkBuddy 上手指南（2026-09-01）](https://developer.cloud.tencent.com/article/2735242)；另一篇教程称 Plan 模式"先分析拆解步骤，确认后再执行"，Ask 模式"只读不写"。 — [CSDN: 从零上手 WorkBuddy](https://blog.csdn.net/yanceyxin/article/details/160649173)
- **扣子空间（Coze Space）**（2025-04 内测）：探索模式 vs 规划模式；规划模式下"执行任务前会先展示规划的步骤"，评测作者"这次我就没有修改，直接点击'开始任务'"，即计划可改、点击开始才执行；规划模式耗时显著更长（一篇评测称翻 4 倍、近 20 分钟才出 PPT）。 — [PConline: 字节版 Manus 扣子空间实测（2025-04-19）](https://www.pconline.com.cn/ai/1917/19174972.html)；[人人都是产品经理: 扣子空间功能解析](https://www.woshipm.com/ai/6220588.html)；[阮一峰: 办公类 AI 初探：扣子空间](https://www.ruanyifeng.com/blog/2025/04/coze-space.html)
- **MiniMax Mavis**（2026-05 发布，Leader/Worker/Verifier 三角色）：plan 模式先列 7 步，用户批准后执行；跑完第 1–3 步后停下汇报并等 "继续"，第 5 步后再停一次；用户可在运行中追加要求，Mavis 会吸收。评测者认为"停下来确认是好实践，但用户常常不想被打断"。 — [PConline: MiniMax 推出了 Mavis（2026-05-15）](https://www.pconline.com.cn/focus/2149/21496771.html)
- **Kimi OK Computer**（2025-09）：生成 Todo List，完成项标 "x"、进行中标 "-"，用户据此跟踪进度；未描述计划可编辑。 — [量子位: 实测 Kimi OK Computer](https://www.qbitai.com/2025/09/337099.html)
- **纳米AI 超级搜索智能体**（2025-06-11 发布）："工作流程全程可视化"，用户能看到每步搜索路径与工具调用；文中未提可编辑任务清单。 — [量子位: 纳米AI超级搜索智能体发布](https://www.qbitai.com/2025/06/293324.html)
- **LangChain `TodoListMiddleware`**：给 agent 一个 `write_todos` 工具用于任务规划与跟踪（Deep Agents 默认包含）。 — [LangChain built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in.md)
- **Anthropic 上下文工程指南**（2025-09-29）："Like Claude Code creating a to-do list, or your custom agent maintaining a NOTES.md file"是跨多次工具调用保持进度的简单方式。 — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### Inferences
- 对 PPT/海报产品，"计划"的最佳形态是**大纲**（WPS 灵犀与 Manus Slides 的实践）：页级结构 + 每页要点 + 视觉风格/模板选择，而非工程式步骤列表。Lovable 的"选段 Comment 只改那一段"与"版本 undo/redo"是最值得照搬的大纲编辑机制。
- Manus "手动开启 Plan Mode" 与 Notion "默认 Review for me" 都在表达同一取向：确认门应是可选的，默认路径要快。
- 在 LangGraph 中，大纲确认天然对应一个 `interrupt()` 节点（payload = 大纲 JSON + `response_schema` 定义可编辑字段），resume 值即编辑后的大纲；大纲应写入独立 state 字段（见第 5 节）。

### Gaps
- 扣子空间升级为"扣子 3.0/v3.5.2（2026-09-28）"后，规划模式是否保留、是否改为"可视化工作台"（应用商店文案称"用户的拖动和修改也能被 Agent 识别"），没有官方更新日志证实。 — [应用宝: 扣子 v3.5.2](https://sj.qq.com/appdetail/com.coze.space)；[腾讯新闻: 扣子3.0更新（2026-06-01）](https://news.qq.com/rain/a/20260601A061L200)
- WorkBuddy Plan 模式的计划是否可编辑、以什么 UI 呈现（todo/文档）——两篇教程均未说明。
- Genspark/Skywork/纳米AI 均无"可编辑计划"的官方描述。

---

## 3. 执行中：进度流式、打断/引导、暂停等待输入、动作审批

### Takeaway
执行中交互分三类：(a) 进度可视化（步骤列表、工具调用、实时预览、沙盒屏幕）；(b) 用户主动打断/追加指令且不丢进度（ChatGPT agent、WorkBuddy、Mavis、Comet 均支持）；(c) Agent 主动暂停请求许可（登录接管、发送/发布/删除前审批），并且 2026 年起普遍支持**把这个提问推到手机**（Cowork 7 月、Manus 移动端）。框架层对应 `interrupt()`/`needs_approval`/`approval_mode`/`toolApproval`。

### Cited Findings
- ChatGPT agent 官方："you can interrupt at any point to clarify your instructions, steer it toward desired outcomes, or change the task entirely"，之后"resumes with the new information, but without losing previous progress"；登录时暂停并通过 "..." 菜单让用户**接管虚拟浏览器**，接管期间不截屏；交还后"attempts to continue from the prior workflow state"，有时需要重新建立运行或再次询问。 — [OpenAI: Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/)；[Help: ChatGPT agent](https://help.openai.com/en/articles/11752874-chatgpt-agent)
- Claude Cowork：2026-07-07 扩展到 web/移动端，任务可在云端后台运行；"If Claude needs permission to complete a task, you'll get a notification on your phone"，可在手机上回应让它继续；Anthropic 原则 "Nothing ships until you've reviewed and approved it."；2026-09-16 "Claude Cowork comes to every conversation"，不再需要先选模式。 — [Engadget (2026-07-07)](https://www.engadget.com/2209495/now-you-can-direct-anthropic-claude-cowork-ai-from-your-phone/)；[Claude Release notes](https://support.claude.com/en/articles/12138966-release-notes)；第三方称默认"asks before taking an action"，可改为只在需要细看时 check in（未在官方 release notes 中找到该设置项）。 — [digitalapplied](https://www.digitalapplied.com/blog/claude-chat-cowork-merge-docs-slides-what-teams-set)
- WorkBuddy 教程："不满意随时打断并提修改意见"，示例"第 3 页图表换成柱状图"、配色改蓝色"即时调整"；Ask 模式避免 AI"自作主张改文件"。 — [腾讯云: WorkBuddy 上手指南](https://developer.cloud.tencent.com/article/2735242)
- MiniMax Mavis：分段执行后暂停汇报、等待"继续"；一个 Worker 跑了 12 分钟无输出、Leader 看起来卡住，作者只能用 bash 查状态——"不透明等待"是显式痛点。 — [PConline: Mavis](https://www.pconline.com.cn/focus/2149/21496771.html)
- 智谱 AutoGLM 沉思（2025-03）：用浏览器插件操作网页，未登录站点时"需要用户介入，等待手动确认"；36 氪试用"折腾了四轮、八次操作"才出报告。 — [36氪: 体验 AutoGLM 沉思](https://36kr.com/p/3231306726587524)；[网易: 实测 AutoGLM 沉思](https://www.163.com/dy/article/JS0R68CG05198R91.html)
- Perplexity Comet 助手：可随时停止或中途补充指导；登录/购买等敏感动作前请求许可（Comet 非 Computer，但同公司交互取向）。 — [createwith: Perplexity updates Comet](https://www.createwith.com/tool/perplexity/updates/perplexity-updates-comet-with-more-transparent-assistant-controls)
- Lindy："Confirm before sending" 设置：Agent 先起草，经 Slack/web 批准后才执行外部动作；信任建立后可按技能逐个改为全自动。 — [automationatlas: build a Lindy assistant 2026](https://automationatlas.io/answers/how-to-build-a-lindy-ai-assistant-2026/)
- 扣子空间（2025-04 评测）："适时与用户确认意图"、"在关键步骤后可能暂停，向用户确认或请求修改"（规划模式）。 — [腾讯新闻: Coze Space 怎么样](https://news.qq.com/rain/a/20250421A09L3T00)
- **框架层**：
  - LangGraph：`interrupt()` 通过抛特殊异常实现——"No try/except around interrupt()"；恢复时**整个节点从头重跑**（子图作为函数调用时父节点与子图节点都重跑）；节点内多个 interrupt **严格按索引匹配**，禁止条件跳过/重排/`while True` 循环；interrupt 前的副作用要幂等。流式驱动推荐 `graph.stream_events(..., version="v3")`，投影 `stream.interrupted`、`stream.interrupts`（`Interrupt.value/.id`）；并行节点各自 interrupt 时可一次 `Command(resume={i.id: value ...})`。`interrupt_before/after` 静态断点"not recommended for human-in-the-loop"。 — [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts.md)
  - LangGraph streaming：`stream_mode` 支持 `values / updates / messages / custom / checkpoints / tasks / debug`；节点/工具内用 `get_stream_writer()` 发自定义进度事件；`subgraphs=True` 带 `ns` 标识子图来源；v2 下 interrupt 出现在 `values` stream part 的 `interrupts` 字段，`invoke()` 返回 `GraphOutput.interrupts`；LangGraph v1.2 引入 typed-projection 的 event streaming。 — [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming.md)
  - LangChain `HumanInTheLoopMiddleware`（LangChain 1.0，2025-10-22 发布）：`interrupt_on={tool: True|False|InterruptOnConfig}`；`allowed_decisions` ∈ {`approve`,`edit`,`reject`,`respond`}；`when` 谓词（`langchain>=1.3.3`）；`interrupt_mode="batched"`(默认，每个 model turn 一次 interrupt，payload 含 `action_requests[{name,args,description}]` + `review_configs[{action_name,allowed_decisions}]`) 或 `"per_call"`(`langchain>=1.4.6`，每个工具调用一个 interrupt，value 含 `type:"tool_approval"`, `tool_call_id`, `name`, `args`, `description`, 自带 `response_schema`)；resume：batched 用 `Command(resume={"decisions":[{"type":"approve"}|{"type":"edit","edited_action":{"name","args"}}|{"type":"reject","message"}|{"type":"respond","message"}]})` 且顺序须与 `action_requests` 一致；per_call 用 `Command(resume={interrupt_id: decision})`，可逐个回答，未答的保持 pending。 — [LangChain Human-in-the-loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop.md)；[LangChain/LangGraph 1.0 发布](https://www.langchain.com/blog/langchain-langgraph-1dot0)
  - OpenAI Agents SDK（Python）：`needs_approval=True` 或 async callable（"fail closed"）；`result.interruptions` 含 `ToolApprovalItem`；`state = result.to_state()` → `state.approve(item, always_approve=False)` / `state.reject(item, rejection_message=...)` → `Runner.run(agent, state)`；`state.to_json()/RunState.from_json()` 可持久化；"Approval permits a tool call to execute; it does not guarantee exactly-once execution"（建议幂等键）；序列化状态未鉴权，只能反序列化可信快照。 — [OpenAI Agents SDK: Human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
  - Microsoft Agent Framework（文档 ms.date 2026-10-15）：Python `@tool(approval_mode="always_require")`，`result.user_input_requests[].function_call`，`to_function_approval_response(True/False)` 以 user Message 回传并带同一 `AgentSession`；.NET 用 `ApprovalRequiredAIFunction` + `ToolApprovalRequestContent.CreateResponse(bool)`；Harness Agent 自带 `ToolApprovalMiddleware`：排队、"standing approvals"（always approve）、`auto_approval_rules`；默认 approval response 必须绑定到同一 session 里的 pending request（防伪造/重放）。 — [MS Agent Framework: tool approvals](https://learn.microsoft.com/en-us/agent-framework/tutorials/agents/function-tools-approvals)
  - Vercel AI SDK（v6 起）：`needsApproval: true|fn` 暂停 `execute`，客户端 tool part 进入 `approval-requested` 状态（`approval.id/isAutomatic/requestReason/reason`），UI 调 `addToolApprovalResponse({id, approved, reason?})`，`useChat({ sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses })` 自动续跑；`ToolLoopAgent.toolApproval` 状态 `not-applicable|approved|denied|user-approval`；`experimental_toolApprovalSecret` 用 HMAC 绑定 tool name + call id + input 防客户端伪造审批；provider-executed tools 不走此机制。 — [AI SDK: Tool Approvals](https://ai-sdk.dev/docs/agents/tool-approvals)；[AI SDK 6 beta 公告](https://v6.ai-sdk.dev/docs/announcing-ai-sdk-6-beta)；[Cookbook: human-in-the-loop](https://ai-sdk.dev/cookbook/next/human-in-the-loop)

### Inferences
- "进度"在办公 Agent 里最有效的呈现是**产物实时预览**（WorkBuddy "实时预览/变更记录"、Skywork/Kimi 的在线 PPT），而不是终端日志；LangGraph 的 `custom` stream + `get_stream_writer()` 适合推送"当前页/当前区块已更新"的粒度事件，前端据此局部刷新预览。
- "打断不丢进度"在 LangGraph 中可落地为：用户新消息作为普通 input 进入同一 thread（文档明确"Pass a plain input dict to continue multi-turn conversations"），由 reducer 把"追加指令"合并进 state，而不是用 interrupt。
- 手机端审批（Cowork）暗示审批 payload 必须是**自包含、可序列化、可离线展示**的（LangGraph 要求 interrupt 值 JSON-serializable 正是为此）。

### Gaps
- Manus/Genspark 执行中"打断并追加指令"的官方机制未找到文档（仅 Plan Mode 可中途开启）。
- Perplexity Computer 本身的审批/追问机制未找到官方帮助文档；评测（cybernews 等）抓取失败。
- 国内产品（WorkBuddy、豆包工作、扣子）对"危险操作（删文件/发送）"是否有审批弹窗——教程均未描述；WorkBuddy 只有"首次授权文件夹"建议。

---

## 4. 生成后：预览、反馈方式、局部重生成与已确认内容的保留

### Takeaway
2026 年的共同方向是「选区 + 指令」：Claude 6 月"Highlight the part you want changed, type the change"、Gemini Canvas "Select & ask"、WorkBuddy "人机双写"（选中段落下指令就地改写）、Genspark AI Slides 选中文本改写且版式不变、Lovable 选段 Comment。保留已确认内容主要靠"只改选区 + 直接手工编辑 + 版本回退"，而非显式"锁定"机制；真正的"元素级编辑器"在 Kimi OK Computer、豆包工作、Genspark Advanced Edit、Claude Design 中出现。

### Cited Findings
- Claude（2026-06-12 release note）：Edit drafts in place——"Highlight the part you want changed, type the change"，Claude 在原位修改；2026-09-16 起 Claude Design / Claude Slides / Claude Docs 在任何对话可用（所有计划含 Free，Enterprise 需管理员开启，beta）。 — [Claude Release notes](https://support.claude.com/en/articles/12138966-release-notes)
- Claude Design（Anthropic Labs，2026-04-17，研究预览）：描述需求→live canvas 上出第一版→通过"chat、inline comments、direct text edits、或 Claude 生成的属性滑块（颜色/间距/字体）"精修；导出 PDF/PPTX/HTML/可分享 URL，可交接 Canva。 — [unite.ai: Anthropic launches Claude Design](https://www.unite.ai/anthropic-launches-claude-design-for-visual-prototyping-and-presentations/)
- Gemini Canvas 官方帮助：点 **Select & ask**（Canvas 面板右下）→选中文本/区段→输入 prompt；docs 还可 **Suggest edits**；全局用 **Change length / Change tone** 滑块；可直接在文档里打字，"Changes are auto-saved"；顶部 **Previous Version / Next Version** 穿梭版本；从 doc 可 **Create** 出 slide show、infographic、quiz、web page、Audio Overview；导出 Docs/Slides(PDF)。 — [Gemini Apps Help: Canvas](https://support.google.com/gemini/answer/16047321?hl=en&co=GENIE.Platform%3DDesktop)。注意 Gemini Enterprise 的 Canvas 文档称"each update generates a completely new Canvas artifact. The existing artifact is not modified or versioned."——与消费者版的版本穿梭不同。 — [Google Cloud: Gemini Enterprise Canvas](https://docs.cloud.google.com/gemini/enterprise/docs/assistant-canvas)
- WorkBuddy V5.3.5（2026-07-30，"人机双写"，联合腾讯文档）：AI 聊天框旁边放完整文档编辑界面，用户手写、**选中一段并下指令**，AI 就地改写/扩写/生成图表，"即时生效"；多名腾讯文档核心员工调入 WorkBuddy 团队。 — [钛媒体: WorkBuddy 给腾讯长脸了（2026-08-02）](https://www.tmtpost.com/8088161.html)
- Genspark AI Slides（2025-12 视频评测，2026-01 整理）：三种修改入口——"Fix layout / polish content"；**选中文本并提 prompt**（如改短），"只有选中的文本变了，版式不变"；**Advanced edit** 打开手动编辑器，保存后回到 deck 生效；导出 PDF/PPTX/Google Slides。 — [readray: Genspark AI workspace review](https://readray.ai/en/notes/gensparkai-20260114/genspark-ai-workspace-review)；Timewell 称新"rich editing"模式可复制/删除元素、微调位置，此前小改动要导出到别的工具。 — [timewell: Genspark Figma](https://timewell.jp/en/columns/genspark-figma)；官方博客推荐导出 Canva/Figma 做重编辑。 — [Genspark blog](https://www.genspark.ai/blog/genspark-ai-slides-edit)；评测汇总称"revision consistency"是公开评价中的常见抱怨，Genspark 对"repeated micro-edits"不划算（信用点消耗）。 — [lindy: Genspark review 2026](https://www.lindy.ai/blog/genspark-review)；[sortwell](https://sortwell.net/en/ai-tools/genspark-ai-review)
- Manus Slides 官方文档：修改方式为"通过对话让 Manus 改，或导出 .pptx 后直接编辑"；文档未描述逐页编辑、选元素或单页重生成；可输入已有研究报告、上传 .pptx 模板（套用其版式/配色/字体）、CSV/Excel 数据；生成 5–15 分钟。 — [Manus Slides docs](https://manus.im/docs/features/slides.md)
- Skywork：生成物右上角「Edit」按钮直接编辑，"调整PPT排版、Excel公式，及时刷新数据保存结果，避免重复生成"；可多轮对话精修；导出 PPTX/PDF/HTML/Google Slides。 — [PConline: 天工超级智能体](https://www.pconline.com.cn/ai/article/919528.html)；早期实测称在线编辑"目前仅限于修改文字内容"，有文字挤压、页面尺寸不一致等瑕疵；因产物本质是网页，AI 改不好可下载 HTML 源码自己改。 — [搜狐: 天工超级智能体功能测评](https://www.sohu.com/a/898900839_121275984)
- Kimi OK Computer（2025-09）：PPT "支持了高自由度的编辑，不仅是文本替换，还支持更改格式、添加图片、流程图、新幻灯片"，且可下载 PPT 格式不丢效果；但"部分元素会超出页面，需要和 Agent 沟通修改要求后，进行一次修复"。 — [知乎: 实测 Kimi OK Computer](https://zhuanlan.zhihu.com/p/1955688194763884210)；[量子位](https://www.qbitai.com/2025/09/337099.html)
- 豆包工作（2026-09-01 开售）：生成 14 页商务 PPT 约 10 分钟，"文件可以逐个元素修改"；分析结果导出为"可编辑的多维表格"，可直接调分析维度不必重导数据；网页初版仍需较多手动调整。 — [CSDN: 半个月豆包工作实测（2026-10-11）](https://blog.csdn.net/FY122345/article/details/167392888)；专业版实测"PPT配图偶开天窗，数据图表识别仍需人工兜底"。 — [新浪财经/每经: 实测豆包专业版（2026-06-25）](http://finance.sina.com.cn/jjxw/2026-06-25/doc-inierihu5715598.shtml)
- WPS 灵犀："边聊边改"：对话中改大纲，并"二次精调模板、单页和版式"。 — [新华网 (2025-07-28)](http://www.news.cn/finance/20250728/64d6e8901aa74e7ebc6942049d353b16/c.html)
- 通义千问 PPT（2026 横评）："生成后可单独修改每一页的布局"，"二改成本"项得分较高。 — [KeyNote: 国内5大AI PPT工具横评 2026](https://www.keynote.org.cn/blog/ai-ppt-2026-comparison-china/)
- Lovable：对计划"选中一部分点 Comment，Lovable 只改那一段"（计划层面的局部重生成范式）。 — [Lovable Plan mode](https://docs.lovable.dev/features/plan-mode)
- Flowith：画布式——"每个 prompt/response/image 都是可编辑节点，可分叉成分支并排比较"，分支结构是核心卖点；Neo agent 无终端与文件访问。 — [eesel: Flowith review 2026](https://www.eesel.ai/blog/flowith-review)
- 框架层"编辑而非重做"：LangChain HITL 的 `edit` 决策直接改工具参数（`edited_action`），文档提醒"keep changes small"，大改会让模型重新评估甚至重跑工具；`respond` 不能用来否决有副作用的工具（模型会当成功）。 — [LangChain HITL](https://docs.langchain.com/oss/python/langchain/human-in-the-loop.md)
- LangGraph `update_state(config, values, as_node=...)` 创建新 checkpoint 而非改旧的，reducer 照常生效；用历史 `checkpoint_id` 重新 invoke 可从该点重放/分叉（之前节点跳过并复用结果，之后节点重跑）。 — [LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers.md)

### Inferences
- "保留已确认内容"没有哪家做了显式"锁定"UI；实际做法是：(1) 选区级指令让模型只产出选区替换；(2) 产物可直接手工编辑（手工编辑即隐式确认）；(3) 版本穿梭兜底。对 LangGraph 产品，建议在 state 中给每页/每个元素维护 `status: draft|confirmed` + `version`，重生成节点只处理 `draft` 项，并用 `update_state(as_node=...)` 把用户手工编辑写回 checkpoint，形成"分叉点"。
- Genspark "选中文本改写不动版式" 与 Claude "edit in place" 都说明：**返回 diff/替换块而不是整份产物**是避免"小反馈全量重生成"的关键；AG-UI 的 `StateDelta`（JSON Patch）正适合把局部更新推到前端。
- 评测中"导出后需要在 PowerPoint 里清理"（Genspark）、"元素溢出需再修一轮"（Kimi）、"配图开天窗"（豆包）说明 PPT 场景必须把"渲染校验（溢出/空图）"做成自动检查节点，而不是靠用户反馈。

### Gaps
- 没有任何产品公开"局部重生成时如何保证与已确认页风格一致"的技术说明。
- 未找到 Genspark Super Agent 工作区对"已确认事实/已改过的页"跨任务保留的描述。
- Claude Design 是否支持点选单个画布元素再下指令：搜索结果仅说"inline comments / direct text edits / sliders"，未见元素级选择描述。

---

## 5. 跨轮次与跨会话的任务状态/记忆（项目、素材、已确认事实、品牌包）

### Takeaway
产品层普遍用「Project（固定指令 + 知识库/文件）+ 个人记忆（可编辑条目）+ 任务线程」三层结构：Manus Projects、ChatGPT Projects（记忆按 Project 隔离）、Claude Projects + 可编辑分类记忆（2026-08-25）、WorkBuddy（用户画像/项目记忆/历史会话检索 + 按文件夹分组任务）。框架层对应 LangGraph **checkpointer（thread 内）+ Store（跨 thread）**，Anthropic 建议用结构化笔记/压缩/子代理控制上下文。

### Cited Findings
- Manus Projects：每个 project 有一条 master instruction 和一个知识库，新任务自动继承；指令更新在已有任务下次发消息时生效，**文件更新只对新建任务生效**，旧任务保持创建时配置；project 像"folders"，可把已有任务移入；文档未描述任务间共享产物/历史。 — [Manus Projects docs](https://manus.im/docs/features/projects.md)
- Manus 2.0（2026-09-28 发布）：自研 Agent 框架 Cascade、事件触发的长任务、个人事务 app Cue。 — [投资界: Manus 回来了（2026-09）](https://news.pedaily.cn/202609/569798.shtml)（注：Manus 官方博客页标题出现 "Manus is now part of Meta"，与"恢复独立运营"的报道矛盾，本调研无法核实所有权现状。 — [Manus blog](https://www.manus.im/blog/manus-plan-mode)）
- Claude：2026-07-10 记忆改为"individual categorized entries"；2026-08-25 记忆在 Topics 下可编辑、跨 chat 与 Cowork 云端生效，Free/Pro/Max 默认开、Team/Enterprise 默认关；2026-09-16 Cowork 合并后"Chats, tasks, projects, connectors, and skills carry over"。 — [Claude Release notes](https://support.claude.com/en/articles/12138966-release-notes)
- ChatGPT Projects：每个 Project 是独立工作区（指令、上传文件、记忆作用域），主聊天的记忆不流入 Project，Project 记忆不外泄（二手来源，日期自相矛盾）。 — [suprmind: ChatGPT features](https://suprmind.ai/hub/chatgpt/features/)
- WorkBuddy：记忆分"用户画像、项目记忆、过往会话检索"三层，可说"记住，我以后所有报告都用这个格式"；侧栏"任务列表（按文件夹分组）"，每个工作区有自己的工作日志和项目笔记；技能系统 + 技能市场（SkillHub 200+）；连接器含腾讯文档/飞书/钉钉/Notion。 — [腾讯云: WorkBuddy 上手指南](https://developer.cloud.tencent.com/article/2735242)；`.workbuddy/IDENTITY.md` 设定 AI 人格/风格，`/skills`、`/find-skills`。 — [CSDN: 从零上手 WorkBuddy](https://blog.csdn.net/yanceyxin/article/details/160649173)
- Skywork：知识库最多 50 个文件 + YouTube/播客/网页链接，可转成报告/网站/播客；输出可溯源，来源列表区分网络来源与知识库来源。 — [PConline](https://www.pconline.com.cn/ai/article/919528.html)
- 豆包工作：结合飞书日程/任务/群消息/文档等"工作上下文"，产出沉淀为飞书文档、多维表格、任务等"可继续协作的工作对象"。 — [CSDN 实测](https://blog.csdn.net/FY122345/article/details/167392888)
- MiniMax Agent（2026-01 更新）："专家"功能把指令、模型偏好和 Agent 行为保存为可复用模板。 — [新浪科技: MiniMax Agent 开年更新](https://finance.sina.com.cn/tech/roll/2026-01-27/doc-inhiseys6151740.shtml)
- Lovable：批准的计划归档到 `.lovable/plan/`（带日期），是"已确认事实"随代码库持久化的例子。 — [Lovable Plan mode](https://docs.lovable.dev/features/plan-mode)
- **LangGraph 持久化**：checkpointer 选项 `InMemorySaver`（测试）、`SqliteSaver/AsyncSqliteSaver`（`langgraph-checkpoint-sqlite`，本地）、`PostgresSaver/AsyncPostgresSaver`（`langgraph-checkpoint-postgres`，生产，`from_conn_string()` + `setup()`，`thread_id` < 255 字符）、`MongoDBSaver`、`CosmosDBSaver`；`get_state`/`get_state_history`（新→旧）/`update_state`；durability `"exit" | "async" | "sync"`；节点输出作为 pending write 即时保存，同一 super-step 其他节点失败时成功节点不重跑；子图有独立 checkpoint namespace，父图不一定立刻看到其更新——要共享用 Store 或写回父 checkpoint；长会话 checkpoint 会累积，需清理/保留策略。 — [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers.md)；[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence.md)
- Anthropic 上下文工程（2025-09-29）：compaction（接近窗口上限时总结重启，Claude Code 保留架构决策/未解 bug/实现细节、丢冗余工具输出）；"tool result clearing"是最轻的压缩；结构化笔记持久化到上下文窗口之外、之后再拉回（NOTES.md、文件式 memory tool 公测）；子代理各自干净上下文、"returns only a condensed, distilled summary"；just-in-time 检索——保留轻量标识符（路径/查询/链接）按需加载；选择原则："compaction for extended back-and-forth, note-taking for iterative work with clear milestones, and multi-agent setups for parallel research"。 — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- LangChain 相关 middleware：`SummarizationMiddleware`（接近 token 上限总结旧历史）、`ContextEditingMiddleware`（清理旧工具输出）、`FilesystemMiddleware`（`ls/read_file/write_file/edit_file` 作为上下文与记忆，Deep Agents）、`SubAgentMiddleware`（`task` 工具委派子代理）。 — [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in.md)

### Inferences
- 推荐的 state 设计：**thread 级 checkpoint 存"工作流状态"**（当前阶段、大纲、各页 draft/confirmed、待回答的 interrupt），**Store 存跨任务资产**（品牌包、已确认事实、研究摘要、用户偏好，按 `(user_id, project_id)` namespace）；"已确认事实"用 Anthropic 的"结构化笔记"方式写成独立文档并带来源，而非散落在 messages 里。
- Manus "文件更新只对新任务生效"揭示一个坑：项目知识库变更的生效时机需要明确展示给用户。
- 多轮修改会让 checkpoint 快速膨胀（每次 `update_state` 都是新 checkpoint），需要定期压缩 messages（SummarizationMiddleware/ContextEditing）并设置 checkpoint 保留策略。

### Gaps
- 没有找到任何产品公开"品牌包（brand kit）"作为 Agent 上下文的具体实现（Manus Slides 仅支持上传 .pptx 模板套用）。
- Genspark 工作区的跨任务记忆机制无官方文档。
- LangGraph Store（`BaseStore/PostgresStore`、namespace、语义检索）的页面本次未抓到，仅从 persistence 页确认 `InMemoryStore` 与 `store=` 参数。

---

## 6. 跨场景复用：研究报告 → PPT → 海报/社媒，在同一任务上下文内

### Takeaway
"同一工作区内一次研究、多种产物"是 Skywork（一条指令同时出文档/表格/PPT/网页/播客脚本）、Genspark（研究→slides→配音播客视频、设计稿追加"社媒版本"）、Gemini Canvas（从 doc 一键 Create slide show/infographic/web page/Audio Overview）、Microsoft Researcher（报告可"start a presentation or document"）的共同模式；Manus Slides 则通过"粘贴已有研究报告 + 上传模板/数据"实现复用。

### Cited Findings
- Skywork：示例"小米 su7 汽车市场竞争分析"一条指令产出完整文档、竞品数据表、图表 PPT、网页和播客脚本；通用智能体接入十余个 MCP 可做 MV/宣传片/有声书/绘本。 — [CSDN: 天工 Skywork 一稿过（2025-05-22）](https://blog.csdn.net/sinat_37574187/article/details/148160977)；[PConline](https://www.pconline.com.cn/ai/article/919528.html)
- Genspark：为 deck 收集的研究"直接进入 slides"；同一 deck 主题生成双人播客式配音视频；AI Designer 出 4 个海报变体后建议"社媒版本/更多变体"；AI Sheets 一句话生成多页带图表表格。 — [readray: Genspark review](https://readray.ai/en/notes/gensparkai-20260114/genspark-ai-workspace-review)
- Gemini Canvas：从 doc 用 **Create** 生成 slide show、custom visual、app、Audio Overview、quiz、infographic、web page；导出 Docs/Slides。 — [Gemini Canvas Help](https://support.google.com/gemini/answer/16047321?hl=en&co=GENIE.Platform%3DDesktop)
- Microsoft Researcher：报告可编辑/分享，"or use it to start a presentation or document"。 — [Microsoft Support](https://support.microsoft.com/topic/e63ab760-f3de-4c47-ae87-dad601b0e9c4)
- Manus Slides：输入可为"详细 brief、粘贴已有内容（如研究报告）、上传 .pptx 模板、上传 CSV/Excel"。 — [Manus Slides](https://manus.im/docs/features/slides.md)
- Manus Wide Research：主 agent 把请求拆成子任务并行（付费用户最多 20 个同时），任务可拆分时自动触发。 — [Manus Wide Research](https://manus.im/docs/features/wide-research)；[Help](https://help.manus.im/en/articles/11960169-what-is-wide-research)
- 纳米AI（2025-04/06）：100+ MCP 工具可组合，任务列表→搜索资料→生成图表→撰写报告→生成 PPT。 — [量子位: 人人可用的超级智能体](https://www.qbitai.com/2025/04/278640.html)；[量子位 2025-06](https://www.qbitai.com/2025/06/293324.html)
- 秘塔 AI 深度研究（2025-07 免费开放）：输出网页报告，可含 PPT、表格、思维导图与规范引用；评测认为结果可直接作为汇报 PPT 基础框架。 — [量子位: 国产 Deep Research 黑马](https://www.qbitai.com/2025/07/309591.html)；[智东西](https://zhidx.com/p/491833.html)
- 夸克：深度研究输出结构化报告（曾限 App、邀请码、每日 3 任务）；Word 转 PPT 约 10 分钟、支持润色/遵循原文/美化三种模式、套模板后在线改。 — [PConline: 夸克深度研究使用指南](https://www.pconline.com.cn/ai/1933/19330392.html)；[CSDN: 夸克 Word 转 PPT 实测](https://blog.csdn.net/weixin_43151418/article/details/163338139)
- 百度心响（2025-04）：主智能体理解需求、拆解子任务交子智能体，支持 200+ 任务类型，可生成网页形式报告。 — [腾讯新闻: 百度心响 App 全量上线](https://news.qq.com/rain/a/20250425A07TXC00)
- WorkBuddy 30 天对比："办公表格、报表、PPT 输出，WorkBuddy 完胜，而在内容创作上豆包领先"。 — [今日头条: 测了 30 天 WorkBuddy 和豆包](https://www.toutiao.com/article/7639677961997713971/)

### Inferences
- 复用的技术前提是把"研究产物"存成结构化中间表示（要点 + 来源 + 数据表），而不是只存最终文稿；Skywork "来源列表区分网络/知识库"与 Gemini Canvas "从 doc Create"都是基于已有结构化文档派生的。
- 在 LangGraph 中，"研究 → PPT → 海报"可建模为共享一个 `research_brief` state 字段的三个子图，每个子图有自己的大纲确认 interrupt；由于子图 checkpoint namespace 独立，跨子图共享应走父图 state 或 Store。

### Gaps
- 没有产品公开说明"当研究结论被用户修正后，已生成的 PPT/海报是否自动联动更新"。
- 飞书 Aily 与钉钉 AI 助理：找到的多为厂商文案或小样本横评（如 25 题评测 aily 3.16/4 分），未见跨场景产物复用的具体交互描述。 — [CSDN 横评](https://adg.csdn.net/6a5b3c7410ee7a33f28eb81b.html)；[IT之家 选型指南](https://www.ithome.com/0/936/670.htm)

---

## 7. 技术框架：前端渲染 Agent 状态与 HITL（AG-UI / CopilotKit / 生成式 UI / A2UI / MCP）

### Takeaway
AG-UI（CopilotKit 主导，Microsoft Agent Framework、LangChain 等采用）用 SSE 事件流把 messages / tool calls / state（`StateSnapshot` + JSON Patch `StateDelta`）/ activity / reasoning / subagent 推到前端，HITL 以 `RunFinished(outcome=interrupt)` + 前端 `useInterrupt`/`useHumanInTheLoop` 渲染并 `resolve`。生成式 UI 有三条路线：工具驱动（CopilotKit `render`、Vercel AI SDK tool parts）、声明式（Google A2UI 0.9.1/1.0 RC、OpenAI Open-JSON-UI）、开放式（MCP Apps）。

### Cited Findings
- AG-UI 事件：Lifecycle（`RunStarted/RunFinished`——RunFinished 可带 interrupt outcome、`RunError`、`StepStarted/StepFinished`）；Text（`TextMessageStart/Content/End/Chunk`）；Tool（`ToolCallStart/Args/End/Result/Chunk`）；State（`StateSnapshot` 整体替换、`StateDelta` RFC 6902 JSON Patch、`MessagesSnapshot`）；Activity（`ActivitySnapshot/ActivityDelta`）；Reasoning（`ReasoningStart/End`、`ReasoningMessage*`、`ReasoningEncryptedValue`）；Subagent（`SubagentStarted/Finished/Error`）；`Raw`、`Custom`；draft `MetaEvent`、RunStarted/RunFinished 扩展（`parentRunId/input/outcome/result`）；`THINKING_*` 已弃用并将在 1.0.0 移除。 — [AG-UI Events](https://docs.ag-ui.com/concepts/events)
- CopilotKit 定位："AG-UI handles the wire protocol, CopilotKit handles the UI layer"；两种 HITL：`useHumanInTheLoop`（LLM 决定何时问：定义 `name/description/zod parameters`，`render({args,status,respond})` 渲染组件并 `respond()`，答案作为 tool result 回到 agent；后端 `CopilotKitMiddleware` 加入 `create_agent` 的 `middleware`）与 `useInterrupt`（图决定暂停：节点 `interrupt({...})`，客户端渲染并 resume）；可用 `useAgent + agent.subscribe + copilotkit.runAgent` 做无头自定义 UI；页面标注 "LangGraph (Python) doesn't support Human in the Loop: Interrupts"（需对照框架支持矩阵核实）。 — [CopilotKit: Human-in-the-loop (LangGraph)](https://docs.copilotkit.ai/langgraph/human-in-the-loop)；旧 hook `useLangGraphInterrupt({ render: ({event, resolve}) => ... })`，`event.value` 为 interrupt payload，`resolve()` 回传恢复；正在迁移到 `@copilotkit/react-core/v2` 的 `useInterrupt`（`agentId` 必须与运行时注册的一致，否则 interrupt 永不触发）。 — [useLangGraphInterrupt reference](https://docs.copilotkit.ai/reference/hooks/useLangGraphInterrupt)
- CopilotKit 支持三种声明式生成式 UI 规范：A2UI（Google）、MCP Apps、Open-JSON-UI（OpenAI）；AG-UI 包含与 MCP/A2A 的握手。 — [CopilotKit Agentic protocols](https://docs.copilotkit.ai/agentic-protocols)
- Microsoft Agent Framework 的 AG-UI 集成把 HITL 实现为 "Function approval requests for user confirmation"。 — [MS Learn: AG-UI integration](https://learn.microsoft.com/agent-framework/integrations/ag-ui)
- A2UI（Google，Apache 2.0，2025-12 公开）：agent 发送声明式 JSON（非 HTML/代码），客户端用自家组件渲染，只能用**预批准 catalog** 里的组件；版本：v0.8（structured-output-first）、v0.9（prompt-first，`createSurface`、客户端函数、自定义 catalog）、v0.9.1（当前，MIME `application/a2ui+json`）、v1.0 RC（客户端→服务端 RPC `actionResponse`、action IDs、theme→surfaceProperties）；渲染器 Angular/Flutter/Lit/Markdown + 社区；可经 A2A 扩展传输，也有 AG-UI 脚手架与 "A2UI + MCP" 指南。 — [a2ui.org](https://a2ui.org/)；[SD Times: Google launches A2UI](https://sdtimes.com/ai/google-launches-a2ui-project-to-enable-agents-to-build-contextually-relevant-uis/)
- MCP elicitation（2026-07-28）：通过 Multi Round-Trip Requests——服务端返回 `InputRequiredResult` 内含 `elicitation/create`，客户端收集后**重试原请求**并附 `inputResponses`；form mode 禁止请求密码/API key 等敏感信息（须用 URL mode）；客户端 MUST 清楚显示是哪个 server 在要信息、提供 decline/cancel。 — [MCP Elicitation](https://modelcontextprotocol.io/specification/latest/client/elicitation)
- Vercel AI SDK：`useChat` 的 tool part 状态机（`approval-requested` 等）配合 `addToolApprovalResponse`，是"工具驱动生成式 UI + 审批"的最小实现；Chat SDK（2026-08）另有 `requestApproval` 在 Slack/Teams 线程里发 Approve/Deny 卡片并暂停 Workflow 运行。 — [AI SDK Tool Approvals](https://ai-sdk.dev/docs/agents/tool-approvals)；[createwith: Vercel Chat SDK durable approvals](https://www.createwith.com/tool/vercel/updates/vercel-chat-sdk-adds-durable-human-approval-steps-for-workflows)
- LangGraph 1.0 与 LangChain 1.0 于 2025-10-22 发布，承诺到 2.0 前无破坏性变更（LTS）；`NodeInterrupt` 已弃用、2.0 移除。 — [LangChain blog](https://www.langchain.com/blog/langchain-langgraph-1dot0)；[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts.md)

### Inferences
- 对 React 前端，推荐分工：AG-UI `StateSnapshot/StateDelta` 承载"大纲 + 各页状态 + 预览 URL"的共享 state（`useCoAgent`），`Custom` 事件承载进度条，`useInterrupt` 承载"大纲确认 / 风格选择 / 发布审批"卡片；选项卡片的 schema 直接复用 MCP elicitation 的扁平 JSON Schema 子集（单选 `oneOf`+`title`、多选 `anyOf`、`default`），便于同一套渲染器服务于 LangGraph `interrupt(value, response_schema=...)` 与未来的 MCP 工具。
- A2UI 适合"模型即兴生成表单/卡片"的场景，但对固定流程（大纲确认）用受控组件更稳；CopilotKit 对 Python LangGraph interrupts 的支持声明需要在选型时实际验证。

### Gaps
- CopilotKit `interrupt-flow` 页面与 `useCoAgent/useCoAgentStateRender`（预测性状态更新）页面抓取失败，仅从索引与搜索确认存在。
- Google A2UI 的官方发布日期仅由媒体给出（2025-12），a2ui.org 页面不含日期。
- OpenAI Open-JSON-UI 本次未单独调研。

---

## 8. 各产品 HITL 机制速览（含日期/版本）

### Takeaway
下表把前几节散落的事实按产品压缩；标注"(实测)"的来自媒体/博主评测而非官方文档。

### Cited Findings
**全球**
- **Manus**：Plan Mode（2026-07-22，手动，Markdown 计划、Confirm/Dismiss、可中途开启、缺上下文先问）；Projects（master instruction + 知识库继承）；Slides（对话改或导出改，5–15 分钟，可上传 .pptx 模板/数据）；Wide Research（最多 20 子任务并行）；Manus 2.0（2026-09-28，Cascade 框架）。 — [Plan Mode](https://www.manus.im/blog/manus-plan-mode)；[Projects](https://manus.im/docs/features/projects.md)；[Slides](https://manus.im/docs/features/slides.md)；[Wide Research](https://manus.im/docs/features/wide-research)；[投资界](https://news.pedaily.cn/202609/569798.shtml)
- **Genspark Super Agent**：无追问直接执行(实测)；AI Slides 选中改写/Advanced edit/rich editing；研究→slides→配音播客同工作流；信用点随修改轮次消耗；"revision consistency"为公开抱怨点。 — [readray](https://readray.ai/en/notes/gensparkai-20260114/genspark-ai-workspace-review)；[lindy review](https://www.lindy.ai/blog/genspark-review)
- **OpenAI**：ChatGPT agent（2025-07）支持随时打断/接管浏览器/后果性动作前许可，已下线；ChatGPT Work（2026-07-09，GPT-5.6；三模式 Chat/Work/Codex）产出 sheets/slides/docs/dashboards，"asks before taking important actions"，可定时/触发运行；Canvas 支持选区聚焦与直接编辑；Projects 记忆隔离(二手)。 — [Help: agent](https://help.openai.com/en/articles/11752874-chatgpt-agent)；[thecentral](https://thecentral.ai/p/chatgpt-work-explained)；[VentureBeat: Canvas 发布](https://venturebeat.com/ai/openai-launches-chatgpt-canvas-challenging-claude-artifacts)
- **Anthropic Claude**：Cowork 2026-01-12 预览→04-09 GA（macOS/Windows）→07-07 web/移动端 + 云端后台 + 手机审批→09-16 并入所有对话；Claude Design 04-17；edit-in-place 06-12；Claude Design/Slides/Docs 09-16 全计划可用；记忆可编辑 08-25。 — [Release notes](https://support.claude.com/en/articles/12138966-release-notes)
- **Google Gemini**：Canvas Select & ask / Suggest edits / 长度与语气滑块 / 版本穿梭 / 从 doc Create 多种产物；Gemini Enterprise Canvas 每次更新生成新 artifact 不保留版本；Workspace Studio 无代码 agent 平台已 GA（TechRadar）。 — [Gemini Canvas Help](https://support.google.com/gemini/answer/16047321?hl=en&co=GENIE.Platform%3DDesktop)；[Gemini Enterprise Canvas](https://docs.cloud.google.com/gemini/enterprise/docs/assistant-canvas)；[TechRadar](https://techradar.com/pro/google-wants-to-help-businesses-build-ai-agents-with-no-prior-experience)
- **Microsoft 365 Copilot**：Researcher 追问可 "Go ahead" 跳过，输出可编辑报告并派生演示文稿；Analyst 先澄清数据应回答的问题再进 Excel(会议材料)。 — [Microsoft Support](https://support.microsoft.com/topic/e63ab760-f3de-4c47-ae87-dad601b0e9c4)
- **Notion**：三档审批（Always ask / Review for me 默认 / Skip all），分类列出需审批动作（建/改页面与数据库、批量修改、通过连接应用发送、访问网站；Review for me 只拦删除/发送/读凭据/跑脚本）；Custom Agents（2026-02 上线，05-04 起消耗 credits）各自独立审批设置。 — [Notion Help](https://www.notion.com/help/choose-how-notion-agent-asks-for-your-approval)；[eesel: Notion AI review](https://www.eesel.ai/blog/notion-ai-review)
- **Perplexity Computer**：$200/月 Max 为主的云端多模型 agent（19 模型并行编排，评测口径不一），研究综合为最强用例；审批/追问机制无官方文档。 — [lowcode.agency review](https://www.lowcode.agency/blog/perplexity-computer-review)
- **Lovable**：Chat / Plan / Build 三模式；Plan view 可编辑 + 选段 Comment + undo/redo + Approve/Save & approve/Skip；`.lovable/plan.md`。 — [Plan mode](https://docs.lovable.dev/features/plan-mode)
- **Flowith**：节点画布 + 分支并排比较；Neo 无终端/文件访问。 — [eesel](https://www.eesel.ai/blog/flowith-review)
- **Lindy**："Confirm before sending" 逐技能切换自动化；多 agent 起草→审阅链。 — [automationatlas](https://automationatlas.io/answers/how-to-build-a-lindy-ai-assistant-2026/)；[Lindy docs: talk with other Lindy](https://docs.lindy.ai/skills/by-lindy/talk-with-other-lindy)

**中国**
- **腾讯 WorkBuddy**：2026-01-19 内部→02-06 公测→03-09 正式上线→07-18 独立 App/鸿蒙→07-30 V5.3.5 "人机双写"；Craft/Plan/Ask；实时预览 + 变更记录；随时打断；三层记忆；Skills/SkillHub；WorkBuddy Bench（Code/Web/Office/Security）；DAU 报道超 1300 万(7 月)。 — [实在智能: WorkBuddy 时间线](https://www.ai-indeed.com/encyclopedia/17907.html)；[钛媒体](https://www.tmtpost.com/8088161.html)；[腾讯云教程](https://developer.cloud.tencent.com/article/2735242)；[知乎: WorkBuddy Bench](https://zhuanlan.zhihu.com/p/2068748858801264342)
- **扣子空间 → 扣子（Coze）**：2025-04 内测时探索/规划模式，规划模式展示步骤、可改、点"开始任务"执行；2026-01-18 套餐合并为扣子/扣子编程；3.0（2026-06）强调 Agent 团队协作、项目空间、技能商店；v3.5.2（2026-09-28）文案称"可视化工作台，用户拖动/修改可被 Agent 识别"(未验证)。 — [PConline](https://www.pconline.com.cn/ai/1917/19174972.html)；[docs.coze.cn 套餐升级](https://docs.coze.cn/guides_20260119_coze_premium_upgraded)；[腾讯新闻 3.0](https://news.qq.com/rain/a/20260601A061L200)；[应用宝](https://sj.qq.com/appdetail/com.coze.space)
- **纳米AI 超级智能体**（360）：2025-04 "100+ MCP 自由组合"，06-11 超级搜索智能体，流程全程可视化，产出 Word/PPT/Excel/网页/视频；App Store 文案"全程透明可编辑"。 — [量子位 2025-04](https://www.qbitai.com/2025/04/278640.html)；[量子位 2025-06](https://www.qbitai.com/2025/06/293324.html)；[App Store](https://apps.apple.com/app/id6737763506)
- **天工 Skywork 超级智能体**（昆仑万维，2025-05-22）：5 专家 + 1 通用；Edit 按钮在线编辑；一指令多产物；知识库 50 文件；早期实测在线编辑限文字。 — [PConline](https://www.pconline.com.cn/ai/article/919528.html)；[搜狐实测](https://www.sohu.com/a/898900839_121275984)
- **MiniMax Agent / Mavis**（2026-05）：Leader/Worker/Verifier；plan 模式批准后分段执行、分段停下汇报；运行中可追加要求；不透明等待与"共识成本"为痛点；"专家"模板（2026-01）。 — [PConline Mavis](https://www.pconline.com.cn/focus/2149/21496771.html)；[新浪](https://finance.sina.com.cn/tech/roll/2026-01-27/doc-inhiseys6151740.shtml)
- **智谱 AutoGLM 沉思**（2025-03）：浏览器插件操作网页，登录需用户介入；多轮折腾、等待长、报告偏空(实测)。 — [36氪](https://36kr.com/p/3231306726587524)
- **Kimi OK Computer**（2025-09，K2）：Todo List 进度标记；PPT 高自由度编辑且可下载不丢效果；溢出需再修一轮。 — [量子位](https://www.qbitai.com/2025/09/337099.html)；[知乎](https://zhuanlan.zhihu.com/p/1955688194763884210)
- **阿里系**：夸克深度研究（结构化报告、App 内、曾限额）+ Word 转 PPT（三模式、在线改）；通义千问 PPT 可逐页改布局、"二改成本"得分高；钉钉 AI 助理以审批/日程/会议纪要等流程为主，未见 PPT 修改交互评测；阿里云百炼 Agent 未找到 HITL 相关公开资料。 — [PConline 夸克](https://www.pconline.com.cn/ai/1933/19330392.html)；[KeyNote 横评](https://www.keynote.org.cn/blog/ai-ppt-2026-comparison-china/)；[IT之家](https://www.ithome.com/0/936/670.htm)
- **百度 心响**（2025-04，iOS 后续上线）：主智能体拆任务→子智能体执行，200+ 任务类型，多任务并行、过程可见；文心 Agent 在横评中被评"处理非结构化数据出色"。 — [腾讯新闻](https://news.qq.com/rain/a/20250425A07TXC00)；[腾讯云榜单](https://cloud.tencent.com/developer/article/2625885)
- **字节 豆包工作**（2026-09-01 开售，三入口：豆包 App/豆包工作 App/飞书）：PPT 逐元素可改，多维表格可改维度，网页初版需手调，定时任务写入共享文档，仅 SaaS/中国大陆港澳。 — [CSDN 实测](https://blog.csdn.net/FY122345/article/details/167392888)
- **飞书 Aily**：2025-12 推出"任务模式"（从聊天助手到工作自动化，官网口径）；横评小样本领先；支持 MCP。 — [飞书官网](https://www.feishu.cn/content/article/7597739180457806780)；[知乎实测](https://zhuanlan.zhihu.com/p/2019545339502282274)
- **WPS 灵犀**（2025-07-28 发布；灵犀 3.0 2026-05 实测）：大纲→确认→生成，多轮改大纲/换模板/单页精调；复杂专业内容偏通用。 — [新华网](http://www.news.cn/finance/20250728/64d6e8901aa74e7ebc6942049d353b16/c.html)；[新浪实测](https://k.sina.com.cn/article_7857141524_1d452771401902kwa4.html)
- **秘塔**：深度研究免费、过程透明、网页报告一键出，含 PPT/表格/思维导图/引用；自报 BrowseComp/xbench-DeepSearch 榜首。 — [量子位](https://www.qbitai.com/2025/07/309591.html)
- **华为**：题目中的"华为 WPS/WPS灵犀"实为金山办公产品（见上）；本次未找到华为自有办公 Agent 的 HITL 资料。

### Inferences
- 中国产品的差异化主要在"产物可编辑程度"与"本地文件/IM 生态接入"（WorkBuddy 本地读写 + 腾讯文档双写；豆包工作 + 飞书对象），而"计划确认/追问"的设计几乎都比 Manus/Lovable 粗糙或缺少文档。

### Gaps
- 阿里云百炼 Agent、华为自有办公 Agent、钉钉 "One" 智能体、Google Gems 的 HITL 机制：未找到可靠来源。
- 多数中国产品 2026 年最新版本的交互变化缺少官方更新日志，评测多停留在 2025 年发布期。

---

## 9. 评测中反复出现的坑（供产品规避）

### Takeaway
五类高频问题：过度打断/过度追问、长时间不透明等待、小反馈触发全量重生成（并消耗信用点）、产物不可编辑或导出失真、以及登录/权限导致的流程中断。框架侧另有 interrupt 的工程陷阱（节点重跑、索引匹配、副作用）。

### Cited Findings
- 过度打断：Mavis "stops for check-ins even when users want uninterrupted execution"。 — [PConline Mavis](https://www.pconline.com.cn/focus/2149/21496771.html)
- 过度/不足追问的两极："要么不问、直接瞎猜，要么问太多、把用户当傻子（导致用户弃用）"。 — [Agent用户体验设计（腾讯云社区）](https://cloud.tencent.com/developer/article/2553570)
- 不透明等待：Mavis Worker 12 分钟无回应；AutoGLM "反复多次抓取同一页面，以为程序卡死"；扣子规划模式近 20 分钟；Skywork "太慢"。 — [PConline Mavis](https://www.pconline.com.cn/focus/2149/21496771.html)；[知乎 AutoGLM 实测](https://zhuanlan.zhihu.com/p/1890711944861840552)；[CSDN: 天工太慢](https://blog.csdn.net/w605283073/article/details/148211307)
- 小修改成本高：Genspark "less compelling when the job is ... repeated micro-edits"，信用点被 AI Slides/Deep Research 快速消耗。 — [lindy review](https://www.lindy.ai/blog/genspark-review)；[sortwell](https://sortwell.net/en/ai-tools/genspark-ai-review)
- 产物不可编辑/导出失真：早期 Genspark "wasn't possible to manually edit the copy, adjust the graphs, or update the layout"（2025，后已改善）；Skywork 在线编辑限文字、文字挤压；豆包 PPT 不支持自定义模板、小数被四舍五入；WPS AI 上传模板适配不稳。 — [plusai: Genspark review](https://plusai.com/blog/in-depth-genspark-review)；[搜狐 Skywork 实测](https://www.sohu.com/a/898900839_121275984)；[IT之家: 2026 年 8 款 AI PPT 工具](https://www.ithome.com/0/960/511.htm)
- 登录/权限中断：ChatGPT agent 接管后"may need to re-establish the run or prompt you again"；AutoGLM 未登录站点需人工介入。 — [OpenAI Help](https://help.openai.com/en/articles/11752874-chatgpt-agent)；[网易 AutoGLM](https://www.163.com/dy/article/JS0R68CG05198R91.html)
- 多轮后失真/幻觉：扣子空间"速度较慢且存在幻觉现象"；豆包深度研究结论需人工核对约半小时。 — [人人都是产品经理](https://www.woshipm.com/ai/6220588.html)；[CSDN 豆包工作](https://blog.csdn.net/FY122345/article/details/167392888)
- 框架陷阱（LangGraph/LangChain 官方）：interrupt 前副作用重复执行；多 interrupt 索引匹配被条件分支破坏；`while True` + interrupt 导致指数级重放；`when` 谓词在 resume 时结果翻转会跳过审核；per_call 模式下批量回答中一个无效会导致其他工具已执行但 interrupt 仍显示 pending（重发会跑两次）；`HumanInTheLoopMiddleware` 应放在 `ToolRetryMiddleware`/`ToolErrorMiddleware` 之前否则重试会再次询问；子图作为函数调用时父节点与子图节点都重跑。 — [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts.md)；[LangChain HITL](https://docs.langchain.com/oss/python/langchain/human-in-the-loop.md)
- 安全：OpenAI Agents SDK 与 Vercel AI SDK 均警告客户端可伪造审批（需服务端存储状态 / HMAC 签名）；Microsoft Agent Framework 默认将审批响应绑定到同一 session 的 pending request。 — [OpenAI Agents SDK HITL](https://openai.github.io/openai-agents-python/human_in_the_loop/)；[AI SDK Tool Approvals](https://ai-sdk.dev/docs/agents/tool-approvals)；[MS Agent Framework](https://learn.microsoft.com/en-us/agent-framework/tutorials/agents/function-tools-approvals)

### Inferences
- 对本产品的直接启示：(1) 确认点只放在"大纲"和"发布/导出"两处，执行中默认不停（Notion "Review for me" 式）；(2) 任何 >10 秒的步骤必须有可见子步骤与预估时长，并允许"先看已完成页"；(3) 修改请求默认映射到最小单元（页/元素），并在 UI 上明示"本次只改了 X"；(4) 产物必须有"手工编辑"出口并把手工编辑写回 state；(5) 审批动作在服务端校验来源。

### Gaps
- 没有找到关于"追问数量 vs 用户流失"的定量研究或厂商数据；上述均为定性评测与设计经验。
