# AI PPT / 演示文稿生成产品调研（全球 + 中国，截至 2026-10）

> 调研日期：2026-10-27。所有"引用发现"均附来源 URL；"推断"为基于引用做出的判断，未经独立验证。产品名保留原文。
> 说明：多数第三方评测来自竞品博客（Plus AI、Presentations.ai、Alai、ChatSlide、WPS 等），已在条目中标注立场；官方文档优先。

---

## 问题 1：各产品从"请求/素材"到"可编辑 PPT"的交互流程（输入模式、澄清问题、大纲确认、模板/风格选择、轮次）

### Takeaway
2025–2026 年头部产品已收敛为"输入（主题/文档/模板）→（可选）澄清问题 → 大纲确认 → 风格/模板选择 → 渲染 → 对话式/元素级修改"的 3–5 步流程；Gemini in Slides、WorkBuddy、扣子空间、讯飞智文 Vision Agent、NotebookLM(Ultra) 都把"大纲确认"做成显式 checkpoint，而 Manus、NotebookLM(非 Ultra) 则跳过大纲直接出稿，被评测点名为缺点。

### Cited Findings

**全球产品**

- **Gemini in Google Slides（2026-06-30 发布）**：流程为 ①侧栏输入 prompt ②附加 Drive 文件作内容依据 ③附加现有 deck 作风格参考 ④回答 Gemini 的追问（语气/风格/内容/受众）⑤审阅并编辑/批准大纲 ⑥生成后幻灯片"fully editable"；Gemini 还会主动建议相关文件、邮件、聊天记录；上线时仅支持 English (United States)；Business Standard/Plus、Enterprise Standard/Plus、Google AI Pro/Ultra 可用；2026-08-01 前为促销高额度，之后按用户限额 — [Google Workspace Updates 2026-06-30](https://workspaceupdates.googleblog.com/2026/06/create-fully-native-and-editable-presentations-with-Gemini-in-Google-Slides.html)
- **Microsoft Copilot in PowerPoint**：官方支持页：新建或打开现有演示文稿（沿用其主题），Home > Copilot > "Create a presentation from [File]"，选文件后 Send，Copilot 起草 — [Microsoft Support: Create a presentation from existing files](https://eus.prod.support.services.microsoft.com/en-us/topic/create-a-presentation-from-existing-files-beb15deb-3241-4711-a480-9cbd8dc4d755)；另一官方页面：Copilot 可能追问受众、长度、模板/设计方向、配图，可回答或跳过；使用所选模板的版式，从组织模板开始即表示保留主题；可选 Brand Kit（需 Publish）；Word 文档建议用 Word Styles 标记结构、内嵌图片可被引用；Word 文档建议 <24MB — [Microsoft Support（同一链接 fetch 结果）](https://support.microsoft.com/en-us/topic/create-a-presentation-from-existing-files-beb15deb-3241-4711-a480-9cbd8dc4d755)；第三方教程称文件选择器显示 Word/Excel/PDF，一次最多引用 5 个文件，生成大纲后可用手动编辑或追加 prompt 迭代 — [DataCamp: Copilot in PowerPoint](https://www.datacamp.com/tutorial/copilot-in-powerpoint)；Microsoft 365 Roadmap 560694：2026 年 5 月末起 Windows 版可从 PowerPoint Home 直接用 Copilot 创建演示文稿，默认开启 — [mc.merill.net RM560694](https://mc.merill.net/message/RM560694)
- **Gamma**：AI 编辑入口：左侧 chat 输入；工具栏 **Agent** 模式点选要改的元素；每页左缘 "Edit with Agent"；快捷键 Cmd/Ctrl+E；可指定整份/单页/单元素，选中元素显示在 chat 区供确认；可读 URL 建页、改主题、从图片提取内容、加页、改拼写、翻译、总结、联网搜索；修改后 Original/Modified 对比，整体接受或回退（不能逐条接受）；chat 历史不过期；生成中不能发消息 — [Gamma Help: Can I edit my content using AI?](https://help.gamma.app/en/articles/8033284-can-i-edit-my-content-using-ai)；Gamma 以"card"而非固定 slide 为单位，各套餐限制单次 prompt 生成卡数（Free 10 → Ultra 75）；Free 400 一次性积分，付费月刷新 1,000（Plus）到 20,000（Ultra） — [Presentations.ai: Gamma pricing（竞品）](https://www.presentations.ai/pt-br/blog/gamma-pricing)；Gamma Agent 位于 Pro 档，"研究、设计、精修 deck 一分钟内" — [aiproductivity.ai Gamma pricing（第三方）](https://aiproductivity.ai/pricing/gamma/)；第三方价格追踪称 2025-09 由无限 AI 改为"席位+积分"，2026-08 重做 Team/Business 档并取消 Pro/Ultra 的单次卡数上限（未验证） — [flexprice.io Gamma](https://flexprice.io/pricing-index/gamma)
- **Manus Slides**：描述主题或上传文档 → 导入模板或选内置模板 → 下拉选页数 → 可选启用 Nano Banana Pro 配图 → Manus 自动起草大纲，**不提供审阅/批准大纲的步骤**，要改方向只能等或停止；首版约 5 分钟；三种编辑：对话式、元素级 AI 编辑（点元素描述修改）、手动编辑 — [Plus AI: Manus review 2026-04-03（竞品）](https://plusai.com/blog/manus-ai-slide-generator-review)
- **Genspark AI Slides**：自然语言 prompt + 可选 PDF/Word/Excel；agent 抽取内容、组织叙事、加图表/图片/音频；2025-04 发布，2025-05-01 "Rich Editing" 更新支持直接改/删/复制内容、调字体字号；导出 PPTX/PDF；原生 PowerPoint 动画导出尚在开发 — [TestingCatalog 2025-06-15](https://www.testingcatalog.com/icymi-genspark-lets-you-create-full-slide-decks-from-a-single-prompt.md)；第三方称 AI Slides 基于 HTML，编辑常走 PDF→Canva 或 html.to.design→Figma 的变通 — [Plus AI: Genspark review（竞品）](https://plusai.com/blog/in-depth-genspark-review)
- **NotebookLM Slide Deck**：Studio 面板 → Slide Deck → 自定义格式、输出语言、长度、来源、自由描述（受众/风格）→ Generate now / later；**大纲共创仅 Google AI Ultra 订阅者**：开启 "Review outline notes" 后生成，审阅大纲时需一次性给完反馈再 Proceed，之后不可再改大纲；Revise：对单页输入指令，汇总为 Pending Changes，再 "Generate revised deck"（修订不再参考 sources）；可删/恢复/拖拽排序幻灯片；导出 PDF 与 PowerPoint(.pptx)；每次修订生成全新 deck，修订次数受配额限制；仅 18+ — [Google NotebookLM Help](https://support.google.com/notebooklm/answer/16757456)
- **Plus AI**（Google Slides/PowerPoint 插件）：从 prompt 或预设开始，可上传 PDF/.docx/.pptx/文本；选设计师模板（Renew、Tennis、Aurora 等）与数百版式，团队可导入自定义模板并添加 logo/字体/颜色；在 Google Slides 或 PowerPoint 内生成原生文件，含图片、图标、可编辑图表和表格；逐页工具：Insert 加页、Rewrite 改措辞语气、Remix 换版式；支持自定义指令与预设；7 天试用；企业版 SOC 2 Type II、白手套模板转换 — [plusai.com](https://plusai.com/)
- **Beautiful.ai DesignerBot**：从文本 prompt、上传 PDF 或 URL 生成整份 deck，填入模板文字与库存图 — [Presentations.ai: Beautiful.ai review（竞品）](https://presentations.ai/blog/beautiful-ai-review)；Smart Slides 自动重排（加第 5 条 bullet 时间距自动重算） — [toolcenter.ai Beautiful.ai Review 2026](https://www.toolcenter.ai/en/articles/beautiful-ai-review-2026)
- **Pitch**：Pitch 2.0 加入 AI 生成器，可从零或模板起草；Pitch Agent 按品牌模板生成、精修、分析 deck，50+ 模板、20+ AI Actions（第三方目录） — [Subscribed.FYI Pitch 2.0](https://subscribed.fyi/blog/pitch-2-0-turbocharge-your-presentations-with-ai-new-animations-and-streamlined-sharing/)；[Stork: Pitch Agent](https://www.stork.ai/tool/pitch-agent)
- **Canva**：Magic Design 从文本 prompt（可语音）生成整份 deck，"输出需编辑后才能用"；Canva Docs 可用 "Convert to Presentation" 把文档转幻灯片 — [Presentations.ai: Canva AI guide 2026-05（竞品）](https://www.presentations.ai/blog/how-to-create-a-presentation-with-canva-ai-step-by-step)；[Trupeer tutorial](https://trupeer.ai/tutorials/how-to-create-ai-presentation-in-canva)
- **Claude for PowerPoint（官方插件，GA 于 Pro/Max/Team/Enterprise）**：在已加载客户/企业模板的 deck 中描述需求，Claude 读取 slide master、版式、字体、配色生成；可选中单页做"pinpoint edits"而不重生成整份；把 bullet 转为流程图/原生图表；支持 Connectors 拉外部上下文、Skills；侧栏 Instructions 设持久品牌规则；与 Excel/Word/Outlook 共享上下文；不支持 PowerPoint 2016/2019 永久许可、iPad、Android；官方警告 prompt injection 风险 — [claude.com/docs/office-agents/powerpoint](https://claude.com/docs/office-agents/powerpoint)
- **ChatGPT**：第三方称 2026 年 Agent Mode 可从大纲到设计到 .pptx 一条龙，仅付费计划；结果"基础/排版简陋" — [SlideUpLift 2026-09-04](https://slideuplift.com/blog/how-to-use-chatgpt-to-make-a-presentation/)；[Plus AI: ChatGPT to PowerPoint（竞品）](https://www.plusai.com/blog/how-to-use-chatgpt-to-create-powerpoint-presentations)

**中国产品**

- **腾讯 WorkBuddy（2026-09-14 升级 PPT 能力）**：对话框说"帮我生成一份 PPT"或点输入框上方"幻灯片"进入；已装 PPT 技能可输 `/tencent-pptx`；**只给主题和页数时先问汇报对象、场景、目标**，再搭"问题—论证—结论—落地"主线；信息不足只问必要问题，已明确的不重复确认；可传调研报告/PDF/Word，压缩长段落、提取结论、把合适数据转图表、生成演讲备注；提交公司/学校模板后保留主题色、字体层级、封面/目录/流程页结构只换内容；可整体套用某份 PPT 配色版式或只复用其中一页排版；按答辩/路演/政务/科技汇报切换风格；"人机双写"：点选/框选标题、文本框、图片、图表后下指令（拆页、表格转图表、按上一页风格重排、每页加 30 秒备注），改一处不影响其他页；输出标准 .pptx，可在 PowerPoint/WPS 继续编辑，也可导入腾讯文档协作评论 — [AIbase 2026-09-14](https://news.aibase.com/zh/news/31036)；实测文章：默认"PPT 设计"效果偏朴素且不能直接导出 PPT，装技能商店 PPT 技能后视觉明显改善，底层模型 DeepSeek-V4-Pro — [besthub.dev WorkBuddy 3 solutions tested](https://www.besthub.dev/articles/creating-professional-ppts-without-design-skills-3-workbuddy-solutions-tested-8caac6dbba46)；WorkBuddy 3-9 公测，上线 3 个月月活 2000 万、DAU 破 1300 万（厂商口径） — [itiger 转载](https://www.itiger.com/hans/news/2654008710)
- **Kimi Slides（月之暗面）**：输入：文本（自动转结构化大纲）、文档（PDF/Word/Excel/Markdown，可解析长文档并提取内嵌图片复用）、图片（照片/截图/设计稿复刻为可编辑 PPT）、模板（上传企业 PPT 模板，基于其母版和版式生成）、风格参考（预设或上传 PPT/图片学习配色字体版式）；编辑：文本/图片/形状/图表/SmartArt 可单独选中调整；多轮对话在当前文稿迭代；在页面或元素上**加批注定点修改**；每轮 AI 修改自动存为独立版本可对比回退；导出 PowerPoint、PNG，可在线演示 — [kimi.com/zh-cn/features/slides](https://www.kimi.com/zh-cn/features/slides)；创建模式：Smart Layout（AI 生成版式与风格，5–10 分钟，适合数据/图片多）vs Classic Templates（内容填入模板，3–5 分钟，版式更稳定，适合文字多）；Smart Layout 按场景（学术研究/商业洞察/营销推广）预设模板；自定义模板上传 PPTX + 描述即可 — [Kimi Help: PPT creation mode](https://www.kimi.com/en-cn/help/ppt/ppt-creation-mode)；帮助页另称两种模式 "Adaptive: deep research, structured delivery" 与 "Visual: powered by nano banana pro"，模型 K3（旧页写 K2.6），可下载可编辑 PPTX 或在线演示 — [Kimi Help: Slides overview](https://www.kimi.com/help/slides/ppt-overview)；2024 版"新PPT助手"去掉了大纲修改步骤，模板按场景与风格细分，免费不限量 — [ai-bot.cn Kimi PPT 教程](https://ai-bot.cn/ai-tutorials-kimi-ppt/)；2024-08 评测时 Kimi PPT 助手由 AiPPT 提供支持 — [澎湃/科技新知 2024-08-28](https://www.thepaper.cn/newsDetail_forward_28535491)
- **AiPPT.cn**：一句话生成、导入文件/链接、粘贴文字、从模板开始、导入 Markdown 与思维导图（免费版）；20 万+ 模板，支持导入自有模板；AI 编辑：生成单页、多页合成、优化内容、翻译总结、一键替换模板、美化布局、统一字体与 Logo；导出 PPTX/PDF/PNG、长图、数字人视频；免费版每天 2 个 AI PPT、注册赠 1000 积分；企业版含 API、私有化部署、SSO、单机离线版、企业模板定制（open.aippt.cn）；"20+ AI 模型和智能体" — [aippt.cn](https://www.aippt.cn/)
- **WPS AI PPT**：支持主题、文档或大纲输入；可套用公司品牌模板；**单页重新生成**；"全文智能排版"后可逐页微调 — [36氪 AI 测评 2026-09-07（推广性质）](https://eu.36kr.com/zh/p/3973165784740354)；CSDN 对比称 2026-05 更新"智能布局模式"，支持多人实时协作 — [CSDN 2026-06](https://adg.csdn.net/6a3de2c9662f9a54cb848ce3.html)；官网示例 prompt"帮我写年中述职汇报PPT大纲"、"PPT大纲生成"功能 — [ai.wps.cn](https://ai.wps.cn/)
- **百度文库 自由画布**：上传素材（本地/文库/网页链接/百度网盘）→ 画布上圈选素材 → "智能PPT" → 先生成大纲，确认/微调后继续 → 从画布 PPT 模板中挑选；可基于上传的 PPT 模板生成 — [爱范儿 2025-01-20](https://www.ifanr.com/1612646)；基于文心多模态大模型 — [ai-bot.cn](https://ai-bot.cn/sites/29570.html)
- **讯飞智文**：Vision Agent (Beta) 四步：意图洞察 → 大纲构建 → 内容精炼 → 设计渲染，每步可干预（风格可选、大纲/内容可编辑）；Beta 版只支持 PDF 下载，可编辑 PPTX "跑步入场中"（时间未注明） — [人人都是产品经理 评测](https://www.woshipm.com/evaluating/6104563.html)（注：fetch 该页时未见 Vision Agent 内容，此条来自搜索摘要，需核）；ai-bot.cn 描述：先洞察意图，自主搭建逻辑大纲、逐页精炼内容，经"五步设计引擎"打磨输出 — [ai-bot.cn 讯飞智文](https://ai-bot.cn/sites/6024.html)；量子位实测：一句需求 17 页直接出 — [BAAI hub 转载](https://hub.baai.ac.cn/view/54485)；MCP 接口把流程拆为 `create_outline_by_doc` → 选模板 ID → `create_ppt_by_outline` → 轮询取下载地址 — [glama.ai zwppt-mcp](https://glama.ai/mcp/servers/@Alex-Smith-1234/zwppt-mcp)
- **扣子空间 / 扣子 PPT（字节）**："一键优化"按钮把粗糙想法改写成清晰指令；**在关键步骤暂停让用户确认大纲**；先联网搜索补充素材（信源多为权威媒体/官网）；大纲定后按章节搜图；根据数据生成图表；版式按主题定制（教育/医疗等行业）；用户在 checkpoint 选风格；编辑：插图/文本、加页、加删背景、移动缩放改样式单个区块、导入 Word/PDF/链接重组逻辑；PPT 下载不限次 — [36氪 2025-09-08](https://eu.36kr.com/zh/p/3457962457470345)；生成前可预览并修改大纲，可下载 .pptx 用 PowerPoint/WPS 精修 — [ai-bot.cn 扣子PPT](https://ai-bot.cn/coze-ppt/)；个人体验：只能在线编辑文字/换图，"不能在线删除" — [掘金](https://juejin.cn/post/7529747836407185459)
- **秘塔 AI 搜索 PPT（2025-12-09 上线）**：三个入口："学点啥"按钮、上传文件建知识库后"给我讲讲"、搜索结果末尾"生成幻灯片"；流程：上传附件/粘贴链接/关键词检索 → "塔子老师"询问知识掌握程度、画面风格、讲解风格偏好 → 确认后自动理解材料、设计教案、绘制 PPT 并生成语音讲解，全程无需干预（Nature 论文示例 <5 分钟）；近 20 种画面风格（漫画风/像素风）；免费免申请，每天刷新 100 积分≈100 页 — [量子位 2025-12-09](https://www.qbitai.com/2025/12/359845.html)
- **Skywork 天工超级智能体（昆仑万维）**：选 PPT 模式 → 输主题与需求或上传文件 → 自动创建；"澄清卡片"和"待办清单"在生成前确认需求；生成内容带可追溯来源；在线编辑，导出 pptx/pdf；底层 Skywork Deep Research Agent v2 多 Agent 协同 — [ai-bot.cn Skywork](https://ai-bot.cn/skywork/)；[AIbase](https://news.aibase.com/zh/news/18256)；[ai-bot.cn Deep Research Agent v2](https://ai-bot.cn/skywork-deep-research-agent-v2/)
- **歌者 PPT（彩漩）**：主题生成、Word/TXT 转 PPT、多语言、模板库、在线编辑分享（第三方介绍） — [ai-bot.cn 歌者](https://ai-bot.cn/sites/1142.html)；开放平台 API：pptx/pdf 渲染为图片或视频（异步）、格式转换、导出可播放 HTML、"无损排版"（传入 Markdown 返回排版好的 PPT，不扩写不改内容）、提取 PPT 文本及位置大小 — [open.gezhe.com](https://open.gezhe.com/api-216735780)
- **MindShow（麦当秀）**：模板 1000+、版式 500+，导出 PDF/PPTX（PPTX 保留动画）；厂商称 150 万+ 个人用户并有 B2B 版 — [App Store](https://apps.apple.com/us/app/mindshow-ai-ppt-slides/id6670523729)；[MWC Shanghai 展商页](https://www.mwcshanghai.com/exhibitors/33337-mindshow-information-technology-co-ltd)
- **纳米 AI（360）**：App 描述"提供主题或上传文件即可生成 PPT"，免费下载含内购 — [App Store 纳米AI](https://apps.apple.com/cn/app/%E7%BA%B3%E7%B1%B3ai-%E9%A6%96%E5%88%9B%E5%A4%9A%E6%99%BA%E8%83%BD%E4%BD%93%E8%9C%82%E7%BE%A4/id6737763506)
- **秒出PPT / 笔格PPT / 博思AIPPT（2025-09 横评）**：秒出PPT 以"内容说明书"机制先生成每页内容与设计说明再出稿，导入 Word 后很快生成 20 多页但"像流水账"；笔格PPT 大纲逻辑完整、20 万+ 模板含"国企风"、支持自定义模板上传、可生成数字人讲解；博思AIPPT 白板/思维导图拖拽生成，模板偏互联网年轻化 — [掘金 2025-09-24](https://juejin.cn/post/7553245651938869283)；[bilibili 2026 七款免费 AI PPT](https://www.bilibili.com/read/cv47794755)
- **DeepSeek 系**：DeepSeek 本身不出 PPT 文件，常见两步法"DeepSeek 深度思考生成 Markdown 大纲 → Kimi/AiPPT 排版"，缺点是体验割裂；博思AIPPT 集成豆包/通义 turbo/DeepSeek R1 可切换模型 — [掘金 DeepSeek+Kimi](https://juejin.cn/post/7473167460263870476)；[CSDN DeepSeek PPT](https://deepseek.csdn.net/6a2f6314662f9a54cb7f2b18.html)；36氪 2026-06 比较认为 Kimi 比 DeepSeek 更适合做 PPT — [36kr EN](https://eu.36kr.com/en/p/3858105820861444)

### Inferences
- "澄清问题"正在从可选变为标配，但形式分化：Copilot/Gemini/WorkBuddy 用对话追问（受众、场景、目标、长度、风格），Skywork 用结构化"澄清卡片+待办清单"，秘塔用固定三问（掌握程度、画面风格、讲解风格）。对 LangGraph 设计而言，"只问缺失信息、已明确的不重复确认"（WorkBuddy 表述）是值得借鉴的 interrupt 策略。
- 大纲确认是目前 UX 共识的"唯一硬 checkpoint"；Manus 和 NotebookLM 非 Ultra 跳过它都被评测列为缺点，Google 甚至把大纲共创作为 Ultra 付费特权。
- 国内产品的"模板"概念更重（AiPPT 20 万+、笔格 20 万+、MindShow 1000+），全球产品更偏"主题/风格"（Gamma theme、Plus AI 模板导入、Beautiful.ai Smart Slides），而 2026 年两边都在加"上传自家 PPTX 作为模板/风格参考"。

### Gaps
- 通义千问 / 夸克 的 PPT 功能：仅找到 CSDN 通用教程，无官方说明或可靠实测；未能确认夸克是否有独立 PPT 生成入口。
- 美图 AI PPT：未找到任何有效资料。
- 腾讯文档"智能PPT"：仅 2024-01 公测新闻，无当前流程说明。
- Kimi Slides 是否仍与 AiPPT 合作：官网未提，仅 2024 评测可证当时由 AiPPT 支持。
- Gamma 官方生成流程（大纲步、卡数、文字量、图片来源）未从官方帮助页直接获取；Gamma "3.0/5" 版本号来自第三方。

---

## 问题 2：渲染/生成技术路线（模板填充、HTML→导出、LLM 写代码、图片生成）及公开技术资料

### Takeaway
公开可见的四条路线：①模板占位符填充（AiPPT/WPS/讯飞/Kimi Classic）；②HTML/Web 先渲染再转 PPTX（Gamma、Genspark、Manus 旧管线、PPTAgent Skill）；③LLM 直接写 pptxgenjs/python-pptx 或原生 OOXML（Anthropic pptx skill、Claude for PowerPoint、Manus 新管线、SlideDeck AI）；④整页图像生成（NotebookLM 用 Nano Banana Pro、Kimi Visual 模式）。学术界 PPTAgent 证明"编辑参考页而非从零生成"+"HTML 表示 + 代码 API + 自纠错"能显著提高成功率。

### Cited Findings
- **Manus 管线切换**：旧管线"web 格式生成、导出时转 .pptx，会有格式问题"；新管线"从第一页起就写原生 .pptx，没有转换步骤"；图表背后是可点击编辑的数据表，表格是真实单元格（可合并/缩放），版式遵循标准 slide master；Manus 内图表工具栏 Elements（轴/数据标签/网格/图例实时开关）与 Edit Data（表格覆盖层+实时预览）；PowerPoint 模式 Beta，限 1.6/Max 模型 — [Manus blog（页脚 ©2026，7月14日）](https://manus.im/blog/manus-ppt-slides)
- **Genspark**："HTML 演示文稿而非原生 PPTX"（竞品评测），导出 PPTX/PDF — [Plus AI Genspark review](https://plusai.com/blog/in-depth-genspark-review)
- **NotebookLM**：由 Nano Banana Pro 图像模型驱动，"幻灯片生成为静态图片"，不能编辑文本框/换图/调版式；Reddit 用户称无法导出到 Google Slides/PowerPoint（因为是图片序列）；用户报告纠错后错误仍在、配图不匹配、伪造来源引用 — [eesel.ai 2026-01-06](https://www.eesel.ai/blog/notebooklm-slide-deck-feature)；但 Google 官方帮助页现已列出 PowerPoint(.pptx) 导出 — [Google Help](https://support.google.com/notebooklm/answer/16757456)（两者冲突：pptx 导出可能仍是每页一张图，见推断）
- **Gemini in Slides**：官方措辞"fully native and editable"，Workspace 博客未进一步定义 native — [Google Workspace Updates](https://workspaceupdates.googleblog.com/2026/06/create-fully-native-and-editable-presentations-with-Gemini-in-Google-Slides.html)
- **Anthropic 官方 pptx skill**：三条路径——新建用 **pptxgenjs** 脚本；编辑/模板用 unzip → 改 `ppt/slides/slideN.xml` → zip（OOXML）；读取用 `markitdown` + `thumbnail.py`；图表用 `addChart()` 保持原生，仅 Sankey/网络/弦图用图片；验证：`validate.py`（模板派生须 `--original`）+ LibreOffice headless 转 PDF + `pdftoppm` 出图逐页目检（文字溢出是最常见缺陷）；字体白名单（Arial/Calibri/Cambria 等，禁 Aptos）；python-pptx 局限：不能复制页、`text_frame.text=` 会丢格式、不读 SVG/EMF — [anthropics/skills pptx SKILL.md](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)
- **Claude for PowerPoint**：读取 slide master/版式/字体/配色生成；"产出可直接编辑的可视元素而非静态图片"；原生 PowerPoint 图表 — [claude.com docs](https://claude.com/docs/office-agents/powerpoint)
- **PPTAgent（EMNLP 2025, ICIP-CAS）**：Stage I 分析参考 deck：结构页由 LLM 标注角色，内容页转图后用 ViT 嵌入层次聚类（阈值 0.65）再由 MLLM 命名版式模式，抽取每页 schema（元素类别/描述/内容）；Stage II：LLM 生成大纲（每页配参考页 + 源文档片段），通过 5 个编辑 API（`del_span`/`del_image`/`clone_paragraph`/`replace_span`/`replace_image`）编辑参考页；**页面以 HTML 渲染呈现给模型**而非原始 XML（一页 XML 约 1006 行）；动作在 REPL 执行，报错回传 LLM 最多 2 轮自纠；PPTEval 用 GPT-4o 评 Content/Design/Coherence（1–5），与人类 Pearson 0.71；Qwen2.5+Qwen2-VL 成功率 95.0%，平均 3.67 vs DocPres 2.87、KCTV 3.00；消融：去 HTML 渲染成功率降至 74.6%，去 schema 降至 78.8%；Zenodo10K 数据集 10,448 份；局限：输出依赖参考 deck 质量、视觉信息利用不足导致元素重叠 — [arXiv 2501.03936v3](https://arxiv.org/html/2501.03936v3)
- **PPTAgent 仓库现状（2026-09）**：MIT，5.2k stars；当前 Skill 形态"幻灯片写为可编辑 HTML 源 → 渲染预览 → 导出 PPTX"，依赖 Node、LibreOffice、Playwright/Chromium；支持 Claude Code/Codex/OpenCode，输出 `answer.pptx` + HTML 源 + 预览 + review 报告；`config.yaml` 可选 text/multimodal 视觉审查；时间线：2025-09 MCP、2025-12 DeepPresenter（ACL 2026，arXiv 2602.22839，含 deep research、自由视觉设计、文生图、20+ 工具沙箱）、2026-01 PPTX 导出与离线模式、2026-03 微调模型上 HF、2026-09 Atria Dawn Preview 模型 — [github.com/icip-cas/pptagent](https://github.com/icip-cas/pptagent)
- **SlideCoder（EMNLP 2025）**：从设计参考图生成可编辑幻灯片代码：颜色梯度分割 + 层次 RAG；分割块坐标从设计分辨率缩放到幻灯片坐标注入 prompt；Slide2Code 基准、SlideMaster 7B 模型；比 SOTA 高至多 40.5 分 — [arXiv 2506.07964](https://arxiv.org/html/2506.07964v1)；代码 github.com/vinsontang1/SlideCoder
- **SlideDeck AI（MIT）**：LLM 输出遵循预定义 schema 的 JSON → 关键词搜图 → python-pptx 按 JSON 建页；支持追加指令增改页并保留指令历史；LiteLLM 接多家模型，Ollama 离线；模板用 0–3 参数选 — [slidedeckai.readthedocs.io](https://slidedeckai.readthedocs.io/en/latest/usage.html)
- **Presenton（Apache 2.0，约 11k stars）**：prompt/文档/数据/API 输入 → 审阅大纲 → 用可复用模板生成 → "结构化画布"编辑（排版/图片/形状/表格/图表/版式/组件级 AI 辅助）→ 导出可编辑 PPTX/PDF；REST API、异步任务、webhook、自托管 MCP；Docker `ghcr.io/presenton/presenton`；支持 Ollama 与 OpenAI/Gemini/Anthropic 自带 key；可上传现有 PPTX 生成模板 — [docs.presenton.ai overview](https://docs.presenton.ai/general/overview.md)；[AlternativeTo](https://alternativeto.net/software/presenton/about/)
- **讯飞智文 MCP**：大纲生成与按大纲建 PPT 分两步，建 PPT 须先选模板 ID，生成后轮询下载地址 — [glama.ai zwppt-mcp create_ppt_by_outline](https://glama.ai/mcp/servers/Alex-Smith-1234/zwppt-mcp/tools/create_ppt_by_outline)
- **百度千帆**："指令智能生成PPT"接口返回标题+大纲并附 PPT 下载地址 — [ai.baidu.com AppBuilder](https://ai.baidu.com/ai-doc/AppBuilder/3mfgfqqxy)
- **Kimi Slides**：Smart Layout 由 AI 生成版式与风格；Classic Templates 把内容填入模板 — [Kimi Help](https://www.kimi.com/en-cn/help/ppt/ppt-creation-mode)；"Visual: powered by nano banana pro" — [Kimi Help overview](https://www.kimi.com/help/slides/ppt-overview)
- **基于 Marp 的开源 skill**：内容输入→大纲（封面/目录/内容/结论页类型）→填充→渲染导出 PDF/HTML/PPTX — [playbooks.com tokendance/ppt](https://playbooks.com/skills/hxk622/tokendance/ppt)
- **社区 Claude PPT skills**：frontend-slides、guizang、dashiAI、codex-ppt-skill、ppt-master，输出从 HTML deck 到可编辑 PPT 不等 — [opendesigner.io](https://opendesigner.io/blog/claude-ppt-skills)

### Inferences
- "HTML 中间表示"是当前最常见的折中：既便于 LLM 理解与布局（PPTAgent 消融证实），又能用浏览器截图做视觉 QA；但 HTML→PPTX 的转换是可编辑性损失的主要来源（Gamma 单 bullet 单文本框、Genspark、Manus 旧管线）。Manus 2026 转向"原生 pptx 优先"、Anthropic skill 用 pptxgenjs+OOXML 直写，说明头部团队正在从"HTML 再转"退回"直接产出 OOXML 并用 LibreOffice 渲染做 QA"。
- NotebookLM 的 .pptx 导出大概率仍是每页一张位图（图片模型驱动），与 Google 自家 Gemini in Slides 的"原生可编辑"形成两条路线。
- PPTAgent 的"参考页编辑 + 5 个原子 API + REPL 自纠错"与 Claude for PowerPoint 的"读 master 生成"本质相同：**把品牌/版式约束变成可执行环境而非 prompt 描述**，这是 LangGraph 设计可直接复用的架构（节点：模板分析 → 大纲（配参考版式）→ 逐页编辑动作 → 执行+校验 → 渲染审查）。

### Gaps
- Gamma、Beautiful.ai、AiPPT、WPS、讯飞、扣子空间、Skywork 的内部渲染技术无公开资料；仅能从导出行为反推。
- Genspark 官方未确认 HTML 路线；Kimi Smart Layout 的实现方式未公开。
- Manus 博客未写年份（页脚 ©2026），"7 月 14 日"疑为 2026-07-14，需核。

---

## 问题 3：生成后编辑（单页重生成、原位编辑、保留已确认页、协作、版本历史）

### Takeaway
2026 年主流已支持"选中元素/单页下指令、其余页不动"（WorkBuddy、Gamma Agent、Manus、Claude for PowerPoint、Kimi 批注、Plus AI Remix/Rewrite、WPS 单页重生成）；版本历史以 Kimi（每轮 AI 修改自动存版本）和 Gamma（Original/Modified 对比、整体接受/回退）最明确；NotebookLM 是反例（每次修订重建整份 deck，受配额限制）。

### Cited Findings
- WorkBuddy：点选/框选元素后下指令，"修改某一处不会影响其他页面"；可导入腾讯文档协作评论 — [AIbase](https://news.aibase.com/zh/news/31036)
- Kimi Slides：批注定点修改；每轮 AI 修改自动保存为独立版本可查看/对比/回退；对话式多轮迭代不重生成 — [kimi.com features/slides](https://www.kimi.com/zh-cn/features/slides)
- Gamma：Agent 模式指定整份/单页/单元素；Original/Modified 对比；一次请求的改动整体接受或回退，不能逐条选择 — [Gamma Help](https://help.gamma.app/en/articles/8033284-can-i-edit-my-content-using-ai)
- Manus：对话式 / 元素级 AI 编辑 / 手动编辑三种 — [Plus AI review](https://plusai.com/blog/manus-ai-slide-generator-review)；Manus 内图表 Elements/Edit Data 工具 — [Manus blog](https://manus.im/blog/manus-ppt-slides)
- Claude for PowerPoint："pinpoint edits to specific slides without regenerating entire decks"；"Restructure the storyline across slides 4 to 7"；"Combine slides 5 and 6" — [claude.com docs](https://claude.com/docs/office-agents/powerpoint)
- Plus AI：Insert/Rewrite/Remix 逐页工具 — [plusai.com](https://plusai.com/)
- WPS AI：单页重新生成；全文智能排版后逐页微调 — [36氪 2026-09-07](https://eu.36kr.com/zh/p/3973165784740354)；多人实时协作（CSDN 转述） — [CSDN](https://adg.csdn.net/6a3de2c9662f9a54cb848ce3.html)
- NotebookLM：Revise 对单页输入指令 → Pending Changes → Generate revised deck；每次修订生成全新 deck，受配额限制；可删/恢复/拖拽排序 — [Google Help](https://support.google.com/notebooklm/answer/16757456)
- Genspark：2025-05 起可改/删/复制内容与字体字号 — [TestingCatalog](https://www.testingcatalog.com/icymi-genspark-lets-you-create-full-slide-decks-from-a-single-prompt.md)；更早评测称无法在 Genspark 内手动改文案/图表/版式 — [Plus AI Genspark review](https://plusai.com/blog/in-depth-genspark-review)
- 扣子空间：在线改文字、换图、加页、移动缩放区块；"不能在线删除"（个人体验） — [掘金](https://juejin.cn/post/7529747836407185459)
- AiPPT：AI 生成单页、一键替换模板、美化布局、统一字体与 Logo；协作权限管理、访客数据 — [aippt.cn](https://www.aippt.cn/)
- Copilot：生成大纲后可手动编辑或追加 prompt 迭代 — [DataCamp](https://www.datacamp.com/tutorial/copilot-in-powerpoint)
- Gamma 2024 评测提到有版本控制与实时数据集成 — [少数派 2024-08-08](https://sspai.com/post/91272)

### Inferences
- "保留已确认页"在产品层表现为两种实现：对话式 agent 把选中元素作为 scope 约束（Gamma/WorkBuddy/Claude），或在模板填充系统里暴露"单页重生成"按钮（WPS/AiPPT）。LangGraph 设计可把 slide 状态置为 `confirmed/draft`，重生成节点只处理非 confirmed 页。
- 版本历史的粒度是差异化点：Kimi 按"AI 轮次"存版本，Gamma 按"单次请求"做 diff；两者都不支持逐条接受，这是可超越的空白。

### Gaps
- 未找到任何产品对"锁定已确认页"的官方描述，均为间接推断。
- 协作编辑：除 WPS、腾讯文档、Pitch、AiPPT 有提及外，其余产品协作能力未核实。

---

## 问题 4：导出与可编辑性（原生 .pptx 文本框 / 图片 / 仅 Web）

### Takeaway
"导出 PPTX"几乎人人都有，但可编辑质量差异巨大：原生编辑器内生成（Copilot、Gemini in Slides、Plus AI、Claude for PowerPoint、WPS）和"原生 pptx 优先"（Manus 新管线、WorkBuddy）最好；Gamma 导出不含 slide master、历史上每个 bullet 单独文本框；NotebookLM 为位图；Genspark 为 HTML 转换；国内评测中 Gamma 和 Beautiful.ai 的 PPTX 兼容性被列为中/差。

### Cited Findings
- **Gamma 官方导出说明**：格式 PDF/PNG/PPTX，Google Slides 需先导 PPTX 再上传；表格默认导出为可编辑 PowerPoint 表格（可关 "Editable tables"），但失去圆角；嵌入主题标题/正文字体（含粗体）与自定义上传字体，斜体为合成斜体；包含演讲者备注；**不包含 slide master**（PowerPoint 母版/Google Slides Theme Builder 为空）；Google Slides 忽略嵌入字体；Free 工作区导出带 "Made by Gamma" 徽章；渐变标题可能导出为单色、毛玻璃背景用回退样式、AI 图片导为 PNG；不支持 .docx — [Gamma Help: export](https://help.gamma.app/en/articles/8022861-what-s-the-easiest-way-to-export-my-gamma)
- **Gamma 用户反馈板**：每个 bullet/段落导出为独立文本框，连换行都各自一框，字号不一致、框体变形；Gamma 已部署修复"连续列表项合并到同一文本框"；表格曾为"对齐的文本框"，用户要求原生表格，Gamma 设计师回应征集样例 — [ideas.gamma.app: bullets](https://ideas.gamma.app/ideas/p/improve-powerpoint-export-so-that-bullets-are-not-exported-individually)；[ideas.gamma.app: line by line](https://ideas.gamma.app/ideas/p/text-exports-to-powerpoint-as-line-by-line-instead-of-one-big-text-box)；[ideas.gamma.app: native tables](https://ideas.gamma.app/ideas/p/export-tables-as-native-powerpoint-tables-editable)
- **国内 6 款横评（2026-06-04，3000 字 Word → 20 页）PPTX 兼容性**：即触AI PPT 高、几乎无错位；aippt 高、基本保留版式；百度文库 高；Beautiful.ai 中、复杂版式会错位；讯飞智文 中；Gamma "常出现文本框偏移、字体丢失，厂商建议用网页版演示" — [CSDN/gitcode 2026-06-04（作者自称个人测试，页脚称"模拟测试"）](https://gitcode.csdn.net/6a2132d1662f9a54cb79820f.html)
- Manus：新管线原生 pptx，图表数据表在 PowerPoint/Google Slides 中保持可编辑 — [Manus blog](https://manus.im/blog/manus-ppt-slides)；旧评测称 PowerPoint 导出"图层凌乱难编辑" — [tech-now.io Manus review 2025](https://tech-now.io/en/blogs/manus-slides-review-2025-is-ai-powered-presentation-creation-finally-seamless/)
- NotebookLM：静态图片，PDF 转 PPT 需第三方工具且常破坏格式 — [eesel.ai](https://www.eesel.ai/blog/notebooklm-slide-deck-feature)；官方导出 PDF 与 .pptx — [Google Help](https://support.google.com/notebooklm/answer/16757456)
- Genspark：HTML 演示，导出 PPTX/PDF，无原生动画导出 — [TestingCatalog](https://www.testingcatalog.com/icymi-genspark-lets-you-create-full-slide-decks-from-a-single-prompt.md)
- WorkBuddy：输出标准 .pptx，PowerPoint/WPS 可编辑 — [AIbase](https://news.aibase.com/zh/news/31036)
- Kimi：导出 PowerPoint、PNG；可编辑 PPTX — [kimi.com](https://www.kimi.com/zh-cn/features/slides)；[Kimi Help](https://www.kimi.com/help/slides/ppt-overview)
- AiPPT：PPTX/PDF/PNG/长图/数字人视频 — [aippt.cn](https://www.aippt.cn/)
- MindShow：PDF/PPTX（PPTX 保留动画） — [App Store](https://apps.apple.com/us/app/mindshow-ai-ppt-slides/id6670523729)
- 讯飞智文 Vision Agent Beta：仅 PDF，PPTX 待上线 — [woshipm（搜索摘要）](https://www.woshipm.com/evaluating/6104563.html)
- Napkin AI：PPTX 导出仅付费（Plus 及以上），免费 PNG/PDF 带水印；PPT 输出为可编辑元素 — [Presentations.ai vs Napkin（竞品）](https://www.presentations.ai/zh/compare/napkin)；[dupple Napkin review 2026-08](https://dupple.com/reviews/napkin)
- Canva：PowerPoint 导出 Pro 专属，打开后版式常漂移 — [Presentations.ai Canva guide（竞品）](https://www.presentations.ai/blog/how-to-create-a-presentation-with-canva-ai-step-by-step)
- 歌者 API：导出可播放 HTML、渲染视频/图片 — [open.gezhe.com](https://open.gezhe.com/api-216735780)
- Skywork：导出 pptx/pdf — [ai-bot.cn](https://ai-bot.cn/skywork/)

### Inferences
- 可编辑性的三个硬指标（可据此设计验收）：①文本是否按段落/列表合并为一个文本框而非逐行；②表格/图表是否为原生对象（Gamma 表格默认原生但图表未提；Manus/Claude/Plus AI 图表原生）；③是否带 slide master/layout（Gamma 无；Manus/Claude 有）。
- "在宿主编辑器内生成"（Copilot/Gemini/Plus AI/Claude 插件/WPS）天然规避导出损失，是企业场景的主流方向；独立 Web 产品则靠"原生 pptx 直写"追赶。

### Gaps
- 未找到对 Kimi、WPS、AiPPT、WorkBuddy 导出文件结构（文本框粒度、图表原生性、母版）的逐项拆解。
- Gemini in Slides 图表是否为原生 Google Slides 图表未见说明。

---

## 问题 5：质量（排版一致性、图表、中文排版、演讲备注、数据准确性、引用）

### Takeaway
数据保真是国内评测 2026 年的新焦点：多数工具会改写数字（aippt、Gamma、Beautiful.ai），仅"保持原文"模式能 100% 保留；2024 年四大国产工具"要求含图表却无一生成图表"，到 2025–2026 扣子空间/WorkBuddy/Manus 已明确"数据转图表"；配图相关性和文字溢出重叠仍是普遍问题。

### Cited Findings
- **数据保真（2026-06 六款横评，32 个数据点）**：即触AI PPT "保持原文"模式跳过 LLM 改写，32 个数据/单位/小数全保留；aippt 把精确数字改成约数、具体百分比改成"显著下降"；Gamma 润色措辞并取约数；百度文库 保留约 95% 但四舍五入；Beautiful.ai 改写文字和数字；讯飞智文 语音转写+生成出现 2 处错误；除即触外均无"原文模式" — [CSDN/gitcode 2026-06-04](https://gitcode.csdn.net/6a2132d1662f9a54cb79820f.html)
- **自定义模板适配（同评测）**：即触 上传 .pptx 后标注页类型一次 2–3 分钟，免费 8 套；aippt 免费 1 套，标题偏移需约 5 分钟手修；Gamma、百度文库 无自定义模板；Beautiful.ai 品牌主题仅 Team/Enterprise；讯飞 需标注 5 种页类型 6–8 分钟，有标题字体错位 — 同上
- **2024-08 四款国产实测（WPS AI、Kimi、百度文库、讯飞智文）**：统一 prompt、默认设置、一次生成不修改；主题测试要求含图表，**四款均未生成图表**；百度文库在《傲慢与偏见》deck 中用了《小妇人》剧照；WPS AI 配图与内容无关；讯飞智文 AI 配图最好但相关性仍需改进，且多页文字重叠、文字过多；Kimi 文档版正文与小标题互换，"像半成品模板"；百度文库大纲两段 100% 重复；全部 2.5 分钟内完成，讯飞约 1 分钟，WPS 文档模式 29 秒但内容最弱；四款均无清晰隐私政策 — [澎湃/科技新知 2024-08-28](https://www.thepaper.cn/newsDetail_forward_28535491)
- 多工具测评：部分产品大纲逻辑不如豆包/文心一言等通用模型，建议支持直接导入大纲；图文相关性不高普遍 — [人人都是产品经理：10+ 款 AI PPT 测评](https://www.woshipm.com/share/6017712.html)
- WorkBuddy：把合适数据转为图表、生成演讲备注、多页一致性、"表格转图表"指令 — [AIbase](https://news.aibase.com/zh/news/31036)
- 扣子空间：联网搜索信源"权威媒体或官网"，根据数据生成图表，版式按行业定制、"不易撞脸" — [36氪](https://eu.36kr.com/zh/p/3457962457470345)
- Skywork：生成内容带可追溯来源 — [AIbase](https://news.aibase.com/zh/news/18256)
- Gamma：导出包含演讲者备注 — [Gamma Help](https://help.gamma.app/en/articles/8022861-what-s-the-easiest-way-to-export-my-gamma)
- NotebookLM：用户报告纠错后错误仍在、配图不匹配、**伪造来源引用** — [eesel.ai](https://www.eesel.ai/blog/notebooklm-slide-deck-feature)
- Napkin：复杂多层关系内容会被"压平"为简单结构 — [dupple Napkin](https://dupple.com/reviews/napkin)
- Anthropic skill 的 QA 清单：文字溢出（最常见）、重叠、页脚碰撞、块间距 <0.3"、边距 <0.5"、对齐不一致、低对比、窄文本框、残留占位符 — [anthropics/skills pptx](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)
- 秒出PPT 对多层因果业务逻辑"处理吃力"；笔格PPT 专业数据分析结论偏笼统需手补数据来源 — [掘金 2025-09-24](https://juejin.cn/post/7553245651938869283)
- Kimi 评测：20 页以上复杂 PPT 表现最好；豆包 5 页以内最快（CSDN 文，无测试方法，可信度低） — [CSDN 横向测评](https://damodev.csdn.net/6a437aac10ee7a33f284d86c.html)

### Inferences
- "原文模式/数据锁定"（不让 LLM 改写用户数字）是一个被评测直接奖励、且头部大厂尚未普遍提供的功能点；可作为 LangGraph 中"素材 → 大纲"节点的硬约束（数字与单位逐字保留 + 自动比对校验）。
- 中文排版未见任何专门测评（字体、标点、行高）；评测关注点集中在"文字重叠/溢出"，推断国内产品通过模板固定文本框并截断来规避，而非做排版引擎。
- 引用/溯源目前只有 research-agent 系（Skywork、扣子空间、秘塔）明确做，PPT 专用工具基本不做。

### Gaps
- 无中文字体/标点排版的专项评测。
- 无各产品演讲备注生成质量的对比；仅 WorkBuddy、Gamma 导出含备注有据可查。
- 2026 年六款横评页脚自称"模拟测试，数据仅供参考"，且作者推荐即触，数字需谨慎。

---

## 问题 6：定价与国内企业使用

### Takeaway
全球产品以席位+积分计费（Gamma Free 400 积分/Plus $10/Pro $20/Ultra $100；Manus $34–167/月；Beautiful.ai $12 起无免费档；Pitch €10–20/席）；国内以免费额度+会员为主（Kimi 月度免费积分、秘塔每日 100 积分、AiPPT 每日 2 份、WorkBuddy 免费公测），企业侧 AiPPT 提供 API/私有化/SSO/离线版，WorkBuddy 三个月 DAU 1300 万（厂商口径）。

### Cited Findings
- Gamma：Free/Plus $10/Pro $20/Ultra $100（月），Team $20/席、Business $40/席；Free 400 一次性积分；Gamma Agent 在 Pro；Free 导出带徽章 — [eesel.ai Gamma pricing](https://www.eesel.ai/en/blog/gamma-pricing)；[aiproductivity.ai](https://aiproductivity.ai/pricing/gamma/)；[Presentations.ai](https://www.presentations.ai/pt-br/blog/gamma-pricing)；[Gamma Help](https://help.gamma.app/en/articles/8022861-what-s-the-easiest-way-to-export-my-gamma)（注：dupple 称 Gamma 不公开价格需联系销售，与其他来源冲突 — [dupple](https://dupple.com/pricing/gamma)）
- Manus：个人 $34/月（年付，8000 积分/月+300/日）、$167/月（40000 积分）、Team $20/席起；评测估 10 页约 200 积分 — [Plus AI Manus review](https://plusai.com/blog/manus-ai-slide-generator-review)
- Beautiful.ai：个人约 $12/月（年付）、Team 约 $40/用户/月，无永久免费仅 14 天试用（需信用卡自动续年费）；G2 4.7/5 (195) vs Trustpilot 2.9/5 (236) — [toolcenter.ai](https://www.toolcenter.ai/en/articles/beautiful-ai-review-2026)；[fast.io](https://fast.io/resources/beautiful-ai-review-2026/)
- Pitch：免费 100 一次性 AI 积分；Plus €10/Team €15/Business €20 每席每月（年付） — [Subscribed.FYI](https://subscribed.fyi/blog/pitch-2-0-turbocharge-your-presentations-with-ai-new-animations-and-streamlined-sharing/)
- Gemini in Slides：Business/Enterprise Standard+Plus、Google AI Pro/Ultra、AI Pro for Education — [Google Workspace Updates](https://workspaceupdates.googleblog.com/2026/06/create-fully-native-and-editable-presentations-with-Gemini-in-Google-Slides.html)
- NotebookLM 大纲共创仅 Ultra — [Google Help](https://support.google.com/notebooklm/answer/16757456)
- Claude for PowerPoint：Pro/Max/Team/Enterprise；可经 Bedrock/Vertex/Azure AI Foundry/LLM 网关部署无需个人账号；M365 Admin Center 组织部署 — [claude.com docs](https://claude.com/docs/office-agents/powerpoint)
- Napkin：付费约 $12/月起，PPTX 仅付费 — [techpoint.africa](https://techpoint.africa/guide/napkin-ai-review/)
- Canva：免费 200 AI 积分，PPTX 导出 Pro — [Presentations.ai Canva guide](https://www.presentations.ai/blog/how-to-create-a-presentation-with-canva-ai-step-by-step)
- Presentations.ai：免费 5 份 — [Stork](https://www.stork.ai/compare/presentations-ai-vs-plus-ai-3)
- AiPPT：免费每天 2 份、注册 1000 积分，标准/专业版价格页面未显示，企业版咨询；30,000,000+ 用户（官网口径）；企业版 API/私有化/SSO/离线/模板定制 — [aippt.cn](https://www.aippt.cn/)；第三方：Plus 600 积分/月最多 20 页、Pro 1200 积分/月最多 50 页，价格"未公布" — [geekchamp](https://geekchamp.com/products/aippt/)；App Store 标题从"超 2000 万"升至"超 3000 万用户" — [App Store](https://apps.apple.com/cn/app/id6504657264)
- Kimi：帮助中心称每月免费积分可体验全部功能，数量未写 — [Kimi Help](https://www.kimi.com/help/slides/ppt-overview)；2024 时 PPT 助手免费，其他三家（WPS/百度文库/讯飞）付费会员 — [澎湃 2024-08](https://www.thepaper.cn/newsDetail_forward_28535491)
- 秘塔：免费，每日 100 积分≈100 页 — [量子位](https://www.qbitai.com/2025/12/359845.html)
- 讯飞智文 2023：新用户 1000 积分，每次生成 50 积分（已过时） — [IT之家 2023](https://www.ithome.com/0/741/591.htm)
- WorkBuddy：3-9 公测，3 个月月活 2000 万、DAU 1300 万，"加速商业化" — [itiger](https://www.itiger.com/hans/news/2654008710)
- 百度文库：模板多为 VIP，免费模板下载也要付费（2024） — [澎湃](https://www.thepaper.cn/newsDetail_forward_28535491)
- 国内场景推荐：工作汇报选即触、团队协作选 WPS AI（IT之家 2026-07-28） — [IT之家](https://www.ithome.com/0/982/629.htm)
- 笔格PPT 有"国企风"模板分类；博思 模板偏互联网风，政务国企选择少 — [掘金](https://juejin.cn/post/7553245651938869283)

### Inferences
- 国内企业采购关注点可归纳为：私有化/离线、品牌模板固化、SSO、政务国企风格模板、数据不出域（2024 评测指出四大厂无隐私政策）——AiPPT 企业版菜单正是对此的回应。
- 国内 C 端几乎零价格敏感（免费额度泛滥），差异化只能靠"模板套用可信度"和"企业流程集成"（WPS/腾讯文档/飞书）。

### Gaps
- AiPPT、WPS AI、讯飞智文、Kimi 的 2026 年确切会员价格均未获得官方页面数字。
- 国内企业客户数、合同规模无公开数据。

---

## 问题 7：评测中的已知缺陷

### Takeaway
反复出现的缺陷：内容泛化/流水账、改写用户数据、配图无关或错误、文字溢出重叠、导出 PPTX 文本框碎片/字体丢失/母版缺失、位图式不可编辑、跳过大纲无法纠偏、伪造引用。

### Cited Findings
- 内容泛化：秒出PPT "像流水账"、笔格 结论笼统 — [掘金 2025-09](https://juejin.cn/post/7553245651938869283)；WPS AI 把分析留给用户、占位式句子 — [澎湃 2024-08](https://www.thepaper.cn/newsDetail_forward_28535491)；ChatGPT 原生文件"plain/minimally formatted" — [SlideUpLift](https://slideuplift.com/blog/how-to-use-chatgpt-to-make-a-presentation/)
- 数据被改写：aippt/Gamma/Beautiful.ai/百度文库 — [CSDN 2026-06](https://gitcode.csdn.net/6a2132d1662f9a54cb79820f.html)
- 配图错误：百度文库 《小妇人》剧照；WPS 无关配图 — [澎湃](https://www.thepaper.cn/newsDetail_forward_28535491)；NotebookLM 配图不匹配、伪造引用 — [eesel.ai](https://www.eesel.ai/blog/notebooklm-slide-deck-feature)
- 文字重叠/溢出：讯飞智文 多页重叠 — [澎湃](https://www.thepaper.cn/newsDetail_forward_28535491)；Anthropic 列为最常见缺陷 — [anthropics/skills](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)
- 导出碎片化：Gamma 逐 bullet 文本框、字号不一致、表格变文本框 — [ideas.gamma.app](https://ideas.gamma.app/ideas/p/text-exports-to-powerpoint-as-line-by-line-instead-of-one-big-text-box)；Gamma 无母版 — [Gamma Help](https://help.gamma.app/en/articles/8022861-what-s-the-easiest-way-to-export-my-gamma)；Manus 旧版图层凌乱 — [tech-now.io](https://tech-now.io/en/blogs/manus-slides-review-2025-is-ai-powered-presentation-creation-finally-seamless/)；Canva 版式漂移 — [Presentations.ai](https://www.presentations.ai/blog/how-to-create-a-presentation-with-canva-ai-step-by-step)
- 不可编辑：NotebookLM 静态图片 — [eesel.ai](https://www.eesel.ai/blog/notebooklm-slide-deck-feature)；Genspark 早期不能在内编辑 — [Plus AI](https://plusai.com/blog/in-depth-genspark-review)
- 无大纲 checkpoint：Manus — [Plus AI](https://plusai.com/blog/manus-ai-slide-generator-review)；NotebookLM 非 Ultra — [Google Help](https://support.google.com/notebooklm/answer/16757456)
- 图表缺失：2024 四款国产均未生成 — [澎湃](https://www.thepaper.cn/newsDetail_forward_28535491)
- 自定义模板设置成本：讯飞 6–8 分钟标注 5 种页类型；aippt 标题偏移 — [CSDN 2026-06](https://gitcode.csdn.net/6a2132d1662f9a54cb79820f.html)
- Beautiful.ai 版式僵硬、无像素级控制、Trustpilot 2.9 分集中在计费 — [toolcenter.ai](https://www.toolcenter.ai/en/articles/beautiful-ai-review-2026)
- WorkBuddy 默认风格朴素需装技能 — [besthub.dev](https://www.besthub.dev/articles/creating-professional-ppts-without-design-skills-3-workbuddy-solutions-tested-8caac6dbba46)
- 扣子空间 复杂任务偶尔卡顿、不能在线删除 — [ai-bot.cn](https://ai-bot.cn/sites/56314.html)；[掘金](https://juejin.cn/post/7529747836407185459)
- SlidesAI 固定三 bullet、Google Slides 与 PowerPoint 版本功能差异 — [tooliverse](https://tooliverse.ai/compare/plus-ai-vs-slidesai)
- Claude for PowerPoint 上线初期 Marketplace 用户报错（The Decoder，可能过时） — [the-decoder](https://the-decoder.com/?p=32304)；官方列出 prompt injection 风险 — [claude.com docs](https://claude.com/docs/office-agents/powerpoint)

### Inferences
- 缺陷清单可直接映射为 LangGraph 的校验节点：数字一致性校验、图文相关性（MLLM 判）、溢出/重叠检测（渲染后视觉审查）、OOXML 结构校验（文本框合并、原生表格/图表、母版存在）、引用真实性（仅允许来自素材/检索结果）。

### Gaps
- 缺少 2026 年对 Kimi Slides、WorkBuddy、Gemini in Slides 的独立缺陷测评（上线时间较短）。

---

## 问题 8：可用于自建的开源项目与库

### Takeaway
构建栈的公开最佳实践：生成层用 pptxgenjs（原生图表、JS）或 python-pptx（局限多）直写 OOXML，或 PPTAgent 式"参考页编辑动作"；中间表示用 HTML（便于 LLM 与视觉 QA）；渲染预览用 LibreOffice headless → PDF → pdftoppm；校验用 OOXML 结构校验 + MLLM 视觉审查；Presenton/SlideDeck AI 可作整体参考实现。

### Cited Findings
- **PPTAgent / DeepPresenter**：MIT，5.2k stars；HTML 源 → 渲染 → PPTX；依赖 Node、LibreOffice、Playwright/Chromium；MCP server；DeepPresenter（ACL 2026）含沙箱 20+ 工具 — [github.com/icip-cas/pptagent](https://github.com/icip-cas/pptagent)；论文 — [arXiv 2501.03936](https://arxiv.org/html/2501.03936v3)；[arXiv 2602.22839 DeepPresenter](https://arxiv.org/abs/2602.22839)
- **SlideCoder**：设计图 → 代码，层次 RAG，7B 模型 — [arXiv 2506.07964](https://arxiv.org/html/2506.07964v1)
- **Presenton**：Apache 2.0，Docker，REST/MCP，可编辑 PPTX/PDF，模板可由 PPTX 生成 — [docs.presenton.ai](https://docs.presenton.ai/general/overview.md)；[Product Hunt](https://www.producthunt.com/products/presenton-data-presentations/launches/presenton-open-source-ai-presentations)
- **SlideDeck AI**：MIT，JSON schema → python-pptx，LiteLLM/Ollama — [slidedeckai.readthedocs.io](https://slidedeckai.readthedocs.io/en/latest/)
- **Anthropic pptx skill**：pptxgenjs 创建 + OOXML 编辑 + validate.py + LibreOffice/pdftoppm 视觉 QA；pptxgenjs 坑位清单（颜色无 #、选项对象被原地修改、无渐变、堆叠图 `outEnd` 会损坏文件、组合图需双轴配置）；python-pptx 不能复制页 — [github.com/anthropics/skills pptx](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)；Agent Skills API 文档 — [platform.claude.com Agent Skills quickstart](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/quickstart.md)
- **paper-ppt-agent**：从论文 PDF/LaTeX 自动生成可编辑 PPT — [sourcepulse paper-ppt-agent](https://www.sourcepulse.org/projects/29593245)
- **ai-to-pptx（SmartSchoolAI）**：国内开源 AI 生成 PPTX 项目 — [sourcepulse ai-to-pptx](https://www.sourcepulse.org/projects/1843044)；GitHub topic `ai-ppt` — [github.com/topics/ai-ppt](https://github.com/topics/ai-ppt)
- **Marp 路线 skill**：内容 → 大纲 → 填充 → 渲染导出 PDF/HTML/PPTX — [playbooks tokendance/ppt](https://playbooks.com/skills/hxk622/tokendance/ppt)
- **社区 Claude/Codex PPT skills**：frontend-slides、guizang、dashiAI、codex-ppt-skill、ppt-master — [opendesigner.io](https://opendesigner.io/blog/claude-ppt-skills)
- **讯飞智文 MCP（zwppt-mcp）**：大纲/模板/生成/轮询 API 形态 — [glama.ai](https://glama.ai/mcp/servers/@Alex-Smith-1234/zwppt-mcp)
- **歌者 Open API**：Markdown 无损排版、pptx 渲染视频/图片、HTML 导出 — [open.gezhe.com](https://open.gezhe.com/api-216735780)
- **AiPPT 开放平台**：API 文档与企业后台 open.aippt.cn — [aippt.cn](https://www.aippt.cn/)
- **百度千帆 PPT 生成接口** — [ai.baidu.com](https://ai.baidu.com/ai-doc/AppBuilder/3mfgfqqxy)
- 基础库（本次未逐一重新核验，为常识性引用）：pptxgenjs — https://github.com/gitbrent/PptxGenJS ；python-pptx — https://python-pptx.readthedocs.io ；Marp — https://github.com/marp-team/marp ；Slidev — https://github.com/slidevjs/slidev ；reveal.js — https://github.com/hakimel/reveal.js ；LibreOffice headless 转换见 Anthropic skill 的 `soffice --headless --convert-to pdf` 用法。

### Inferences
- 对 LangGraph 产品最可直接复用的组合：PPTAgent 的"参考 deck 分析 → schema → 大纲配版式 → 编辑动作 → REPL 执行 + 自纠"节点拓扑；Anthropic skill 的 OOXML 直写 + validate + LibreOffice 视觉 QA 作为渲染/校验子图；Presenton 的"大纲审阅 → 模板生成 → 结构化画布编辑"作为 UX 参考；讯飞 MCP 的"大纲/模板 ID/异步轮询"作为 API 形态参考。
- HTML 作为"给模型看的表示"而 OOXML 作为"交付格式"的双表示架构，是避免 Gamma 式导出损失同时保留 LLM 布局能力的折中。

### Gaps
- 未核验 PPTAgent Skill 的 HTML→PPTX 转换细节（是否自研转换器或依赖 LibreOffice）。
- 未找到 html2pptx 的独立开源项目官方页面；仅在社区对 Anthropic skill 的描述中被提及。
- Presenton 内部版式表示（HTML/React vs 直写）官方 overview 未说明。
