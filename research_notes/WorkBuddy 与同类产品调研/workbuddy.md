# WorkBuddy（腾讯）产品与内容创作交互方式调研（截至 2026-10-27）

> 调研范围说明：本笔记只覆盖腾讯云 CodeBuddy 团队推出的 WorkBuddy（workbuddy.cn / workbuddy.ai / codebuddy.cn/work）。所有"已引用事实"均附来源 URL；"推断"为本人基于事实的判断；"空白"为未能查证的问题。部分知乎页面返回 403，只能依赖搜索摘要，已逐条标注。

---

## 关键问题 0：这是哪个产品？是否存在同名产品？

### 结论
WorkBuddy 是腾讯云 CodeBuddy 团队出品的桌面端"AI 原生办公智能体工作台"，**2026 年 3 月 9 日正式发布**（而非 2025 年；2026 年 1 月内部试用、2 月外部内测）。市场上存在拼写相近但无关的产品（WorksBuddy、IBM 内部的 WorkBuddy 项目、WorkoutBuddy），需注意区分。

### 已引用事实
- 官方文档定义："腾讯出品的全场景 AI 工作台"，用自然语言描述需求即可自主规划并执行任务、交付可用结果 — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)
- 开发团队：腾讯云 CodeBuddy 团队；产品负责人王圣杰、首席架构师林强（百度百科英文版列出） — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 时间线：2026-01-19 腾讯内部 2000+ 员工试用；2026-02-06 外部内测；**2026-03-09 正式发布**，上线后因流量远超预期出现登录/服务不稳定，腾讯公开致歉 — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- TechNode 2026-03-09 报道：腾讯称其为"OpenClaw-like desktop AI agent for workplace tasks"，本地安装无需云端部署，兼容 OpenClaw skills，支持 20+ 技能包与 MCP，可切换混元 / DeepSeek / GLM / Kimi / MiniMax — [TechNode 2026-03-09](https://technode.com/2026/03/09/tencent-launches-openclaw-like-workplace-ai-agent-workbuddy/)
- 媒体将其称为"腾讯版小龙虾"（对标 OpenClaw），卖点是"不用部署，安装就能用" — [新浪财经 2026-03-09](https://finance.sina.com.cn/stock/t/2026-03-09/doc-inhqksxn6971236.shtml)
- 2026-05-29 面向全球用户发布（Tencent Cloud 国际版），支持 Slack / Telegram / Discord / WeChat 远程下发任务，通过 MCP 连接 GitHub、Jira、Google Drive、Gmail、Notion、Slack，内置 100+ 专家角色，支持 API Key 接入模型 — [TechNode 2026-05-29](https://technode.com/2026/05/29/tencent-launches-workbuddy-productivity-ai-agent-for-global-users/)
- Tencent Cloud 官方 X 账号："an AI-native agent designed for everyday office professionals… From a single natural-language instruction, it plans, breaks down, and executes complex multi-step tasks" — [Tencent Cloud on X](https://x.com/tencentcloud/status/2059870178264109429?lang=en)
- 腾讯同时有"腾讯企业智能体 WorkBuddy Enterprise"产品页（含研发编码、通用办公智能体、云端智能体托管） — [腾讯云产品页](https://cloud.tencent.com/product/workbuddy-enterprise)
- 腾讯另一本地 AI 助手 QClaw 于 2026-09-24 宣布 2026-12-24 停运，用户可"迁移至 WorkBuddy" — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 同名/近名产品：**WorksBuddy**（带 s，"AI Agent Suite, Built to Run Your Business"，GetApp/Capterra 收录，与腾讯无关） — [GetApp](https://www.getapp.com/all-software/a/worksbuddy/)；**IBM 内部 "WorkBuddy"**（基于 watsonx Orchestrate 的 HR/IT 助手，来源为 lablab.ai 个人项目页，可信度低） — [lablab.ai](https://lablab.ai/u/@Akki_2312)；**WorkoutBuddy** 为 iOS 健身应用，无关 — [App Store](https://apps.apple.com/us/app/-/id6746044801)

### 推断
- 用户提示中的"2025 年发布"不准确；应为 2026 年 3 月发布。若要对标，应按 2026 年产品形态看待。

### 空白
- 未找到腾讯官方关于 WorkBuddy 的独立新闻稿原文（TechNode 引述的腾讯云微信文章未能直接获取）。

---

## 关键问题 1：平台、定位、目标用户

### 结论
WorkBuddy 是桌面优先（Windows / macOS / 鸿蒙 PC / 麒麟 / UOS）的本地 Agent，辅以网页版、手机 App、微信小程序、手表版，并可通过微信/企业微信/QQ/飞书/钉钉远程遥控电脑；定位"职场人的 AI 同事"，目标用户是非技术的普通办公人群，后续扩展到政务、金融、教育垂直版。

### 已引用事实
- 官方文档导航列出 Windows、macOS、UnionTech UOS、Kylin 安装指南，以及小程序和移动 App 文档；助手接入支持微信（标注"推荐"）、企业微信、QQ、飞书、钉钉 — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)
- 平台时间线：微信小程序 2026-03-31；移动 App 2026-07-18；鸿蒙 PC 2026-07-27；麒麟/UOS 2026-09-03；手表版 2026-09-07（华为发布会） — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 国际版：macOS（Intel/Apple silicon）、Windows 与浏览器内版本；集成 Slack、Discord、Telegram、Google Workspace、Zoom、Microsoft 365；官方称"not a coding agent"，属于腾讯 "Buddy" 家族（CodeBuddy 为编码智能体） — [DevelopersDigest 2026-10-16](https://www.developersdigest.tech/blog/tencent-workbuddy-explained-pricing-free-models-2026)
- 官方核心能力列表：自然语言交互（"无需预先拆分复杂操作步骤"）、自主规划执行、文档/表格/PPT/数据分析多模态任务、授权后的本地文件操作、多任务并行、结果审阅与交付 — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)
- 垂直版本：2026-06-16 广东政务"湾区引擎 WorkBuddy"预发布（内置 GLM-5.2）；2026-09-03 金融版；2026-10-19 深圳福田教育公益版（约 110 所学校、1.3 万教师） — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 第三方报告（Q2 2026 中国办公 Agent 平台市场洞察）：2026 年 6 月访问量超 2000 万，国内 PC AI 原生办公智能体市场第一 — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 2026-10-14 与企业微信联合发布，WorkBuddy 以机器人形式进入企业微信通讯录，管理员可集中购买席位、分配额度、选择模型（混元 / DeepSeek）、接入私有 Skill 和 MCP 连接器 — [钛媒体 via 搜狐 2026-10-20](https://www.sohu.com/a/1090078197_122014422)
- 2026-09-02 生态大会发布开放平台（首批 100+ 合作伙伴）；2026-09-18 发布"应用生成能力"；2026-09-24 支持发布微信小程序 — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- V2EX 用户评价：适合"用 Word、Excel、PPT 的普通办公人群"，连接 WPS 与腾讯文档后"日常办公够用" — [V2EX](https://www.v2ex.com/t/1232713)

### 推断
- 产品策略是"桌面本地执行 + 腾讯系 IM 远程入口 + 企业席位"，与 OpenClaw 的差异在于免部署与腾讯生态绑定。

### 空白
- 未找到腾讯官方公布的 DAU/MAU 绝对数字（新闻稿仅称"国内 DAU 最高的生产力 Agent 之一"）。

---

## 关键问题 2：支持哪些内容创作任务（海报、PPT、调研报告、文档、表格等）

### 结论
覆盖文档 / 表格 / PPT / 数据分析 / 网页 / 图片 / 视频 / 3D / 小程序；海报与设计类内容主要通过 2026-06 起接入的腾讯 AI 设计平台 **Ardot** 的"设计创意"模式实现，也可走"HTML 海报 + 截图导出"的通用 Agent 路径。深度研究报告有官网宣传与社区技能，但官方 Changelog 没有专门条目。

### 已引用事实
- 官方 Overview：设计板块"生成图片、PPT、网页等设计内容"；产物面板展示"概览和产物，以及所有文件、变更和预览" — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)
- 官方"设计创意（WorkBuddy × Ardot）"文档支持类型：移动端 App 界面、网站页面 / Landing Page、品牌 Logo、**海报 / Banner**、PPT 演示文稿 — [官方 Design-Idea 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Design-Idea)
- 秋叶PPT 2026-06-05 文章列出设计创意 9 类：网站设计、移动 App、设计系统、Web App、PPT 设计、交互原型、品牌设计、图标与插画、**视觉海报** — [网易/秋叶PPT 2026-06-05](https://www.163.com/dy/article/KUL6RNNN05169CIL.html)
- Changelog 相关版本：5.3.3（2026-07-21）新增 PPT 生成内置插件、Ardot 画布内嵌预览；5.3.6（2026-07-28）Ardot 画布图片生成新增"真实图片"与"文本占位图"两种模式；5.4.0（2026-08-20）3D 模型生成、创意设计 PPT 模板、本地 PPT AI 编辑撤销/重做；5.5.0（2026-09-01）图片处理（去水印/去文字、修复、增强、抠图）；5.5.1（2026-09-02）视频生成（画幅与音频选项）；5.5.5（2026-09-10）"新增创意设计能力，支持图片生成、视频生成和品牌设计等场景"；5.9.1（2026-10-22）"优化 PPT 生成的素材核对，减少内容与配图不符" — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 官网宣传：深度研究 Agent 可自动检索多渠道公开资料与本地参考文档，输出结构化报告、竞品矩阵、策略建议，支持 .md / .pptx / .docx，称"15 分钟交付报告"（营销表述，未独立验证） — [codebuddy.cn/work](https://www.codebuddy.cn/work/)
- 社区"Deep Research"技能：拆分问题、查资料、判断来源、整理带引用的报告，依赖第三方 GitHub 技能包 — [知乎技能推荐（搜索摘要）](https://zhuanlan.zhihu.com/p/2083296987194118600)
- 百度百科列出的办公任务：报告、文件整理、发票、演示文稿、数据清洗、内容创作、竞品分析；另有"科学文献洞察智能体"（与国家超算互联网，2026-09） — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 多媒体工作流案例（官方开发者社区教程，2026-07-10）：一条指令串联"写公众号文章 → 生成封面与插图 → 由文章生成短视频脚本 → 推送到公众号草稿箱"；视频仍需在剪映完成剪辑 — [腾讯云开发者社区 2026-07-10](https://cloud.tencent.com/developer/article/2707346)
- 公文一键排版为宣传场景之一（营销性质内容） — [今日头条](https://www.toutiao.com/article/7635197853345923635/)

### 推断
- 海报有两条路径：(a) Ardot 画布（可编辑设计稿，PNG/SVG/PDF 导出）；(b) 通用 Agent 直接写 HTML 并用 Chrome headless 截图成 PNG（企业版实测案例）。两条路径交互方式差异很大，调研同类产品时应分别对标。
- "深度研究"更像一个技能/专家角色而非独立产品模块，官方 Changelog 中未见专门迭代。

### 空白
- 未找到官方关于"黑色星期五/促销海报"类的 demo 或案例；仅有小红书营销海报的第三方实测。

---

## 关键问题 3：生成前的需求澄清流程（问什么、以什么形式、何时跳过）

### 结论
WorkBuddy 有三种模式（Ask/问一问、Plan/想一想、Craft/做一做）。Craft 模式默认直接执行；Plan 模式先出执行计划待用户确认。客户端存在"问答选择器（AskUserQuestion）"卡片，4.24.0 起支持选项之外的自定义输入；PPT 功能（2026-09-14 升级）官方称"信息缺失时只追问必要问题（受众/场景/目标），用户已说明的不再追问"。但设计创意（Ardot）和图片生成路径在文档与实测中都没有澄清步骤，风格靠提示词或模板选择。

### 已引用事实
- 三模式定义：Ask "仅问答与信息查看，不修改文件"；Craft 直接执行并修改文件；Plan "先生成执行计划，确认后再操作"。默认权限下起始为只读，编辑/命令需显式批准 — [奇连 AI](https://iqilian.com/learn/workbuddy-sanzhong-gongzuo-moshi/)
- Plan 模式 UI（搜索摘要）：规划后用户可点"调整计划"修改范围，确认后点"开始执行"；"待确认项"多通过提示词要求实现 — [搜索摘要：腾讯云开发者社区《三种运行模式怎么选》](https://developer.cloud.tencent.com/article/2705139)（页面未能直接抓取，以摘要为准）
- Changelog：4.24.0 "新增问答选择器自定义输入支持，选择和补充信息更灵活"；5.2.6（2026-07-12）修复"权限审批和 AskUserQuestion 等待状态被流式帧覆盖"；5.3.11（2026-08-07）修复"企业智能体弹出的问答选择框自动消失后界面卡住"；5.1.0（2026-06-13）修复 "plan 模式自动进入时 UI 未同步切换"；5.4.5（2026-08-30）"历史任务卡在待确认时可正常停止并继续对话" — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- PPT 升级（腾讯 2026-09-14 官方宣布，AIbase 转述）：流程为"给主题和页数（素材可选）→ 系统追问受众、场景、目标 → 搭建主线（问题-论证-结论-行动）→ 读取研报/PDF/Word 提炼结论、转图表、生成演讲备注 → 按场景（答辩、路演、政务、科技报告）套风格"；"**只在信息缺失时追问必要问题，用户已经说明的不再重复问**，减少返工" — [AIbase 2026-09](https://www.aibase.com/zh/news/31036)
- 5.3.3（2026-07-21）"新增 PPT 生成内置插件，支持生成前预览步骤" — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 技能版 PPT（tencent-pptx）流程：描述需求后先生成大纲（每页标题、要点、页面类型：封面/数据/对比/总结），用户可在生成前调整（如"某页改成对比图表""拆成两页"）；文章未描述追问步骤，只有大纲确认这一个检查点；提示"做个PPT"这类模糊请求结果易偏差 — [网易 2026](https://www.163.com/dy/article/L7MQ8A3G0534J1O1.html)
- 任务型"幻灯片"入口（2026-04 实测）：新建任务 → 选"幻灯片"→ 从模板库（工作汇报/产品介绍/培训课件/趋势报告）选模板 → 自动出大纲 → 逐页填充 → 套样式 → 输出；**无澄清提问**，风格、页数靠提示词（如"科技风/商务风/简约风/创意风"） — [腾讯云开发者社区 2026-04-25](https://cloud.tencent.com/developer/article/2660456)
- Ardot 设计创意官方文档：用自然语言描述后"AI 将实时在 Ardot 画布上生成"；风格可选"设计风格模板"（品牌风格：TDesign、Apple、Notion、Linear 等；PPT 专属：优雅杂志、极简灰白等），**"未选择时 AI 会根据你的描述自动匹配风格"**；图片素材可选"色块占位"或"AI 生图"；文档未提及先追问或先出大纲 — [官方 Design-Idea 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Design-Idea)
- 图片生成教程（官方开发者社区 2026-07-10）：直接按描述生成，无确认步骤；推荐提示词三要素 = 主题（必填）、风格（重要：现代/复古/插画/写实/扁平/科技/温暖）、色调（可选：蓝/暖黄/黑白/红金/马卡龙） — [腾讯云开发者社区 2026-07-10](https://cloud.tencent.com/developer/article/2707346)
- 小红书海报实测（企业版，2026-07-21）：未出现任何澄清提问或风格菜单，WorkBuddy 自动把任务拆为设计、文案、违禁词规避、导出四步并执行 — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- 第三方指南：下指令后 AI "解析并展示执行步骤"，确认后开始执行 — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)
- 官方对话文档：执行中显示回复、阶段说明、结果摘要与可展开的中间步骤；运行时可中断，中断后可补充指令、调整后重发或在现有内容上继续 — [w3cschool 镜像官方《任务对话》](https://www.w3cschool.cn/workbuddydocs/workbuddy-conversation.html)
- 个性化设置中有风格/语调选项（默认、专业严谨、亲和友善、直言不讳），仅改措辞不提升能力 — [知乎设置篇（搜索摘要）](https://zhuanlan.zhihu.com/p/2072101736542647091)
- 用户可通过"自定义指令"自行定规则，如"信息足够即停、每次只问一个问题"，属于用户自定义而非内置 — [腾讯云开发者社区](https://cloud.tencent.com/developer/article/2754715)

### 推断
- 澄清行为是"按模式 + 按入口"分化的：通用 Agent 对话（Craft）倾向直接执行，由模型自行决定是否调用 AskUserQuestion 选择器；PPT 专用流程在 2026-09 版本中内置了"受众/场景/目标"的条件式追问；Ardot 设计路径把"风格"做成了可选模板下拉而不是对话式追问。
- "问答选择器 + 自定义输入"（4.24.0）在形态上就是用户关心的"推荐选项 + 自由文本"模式，但官方文档未给出截图和触发规则；它应是模型工具（AskUserQuestion）驱动而非固定表单。

### 空白
- 未找到官方文档或截图明确说明问答选择器的样式（单选/多选、是否带推荐标记）及触发条件。
- 未找到海报/Ardot 路径中"先问风格再生成"的证据；现有证据都指向"不问、靠提示词/模板"。
- 未找到 PPT 追问的具体 UI 形态（卡片还是纯文本）以及"已提供信息则跳过"的可验证实测。

---

## 关键问题 4：生成后如何修改（局部编辑、重新生成、画布/编辑器、多轮保留）

### 结论
有三层修改机制：(1) 对话式局部修改（PPT 单页、海报元素），基于上下文继续处理；(2) Ardot 画布"智能框选 + 对话"和完整编辑器（拖拽、图层、像素对齐），与 WorkBuddy 双向实时同步；(3) PPT 内置"重新生成 / 更换模板 / 撤销重做"。官方称 PPT 单页修改不影响其他页（"人机双写"）。HTML 海报路径的修改是改 HTML 源文件后刷新预览，溢出等排版问题需 3–4 轮迭代。

### 已引用事实
- Ardot 官方文档修改方式：**"智能框选 + 对话"**（画布中选中区域后描述修改，用于局部精准调整）；**"纯语言指挥"**（全局调整）；**"粘贴参考"**（网页链接或参考图借鉴风格）；每轮修改后 Agent 说明本轮具体改动，"过程可追溯"；点"用浏览器打开进行编辑"进入 Ardot 完整编辑器（拖拽修改、像素级对齐、图层管理、组件化编辑）；"在 Ardot 里的任何改动实时回流 WorkBuddy"；示例指令："把这个按钮整体放大""首页背景改成渐变蓝色""换成极简风" — [官方 Design-Idea 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Design-Idea)
- 秋叶PPT 文章：PPT 设计中确认大纲后 Ardot 搭结构、填内容、排版，每页"分图层展示"，可在 WorkBuddy 对话继续精修或进 Ardot 后台调布局/文字/图片，编辑器内容自动同步；图标可逐个编辑并存为模板，"喜欢某个图标可按同风格再生成" — [网易/秋叶PPT 2026-06-05](https://www.163.com/dy/article/KUL6RNNN05169CIL.html)
- PPT 升级（2026-09-14）：可点击或框选标题、文本框、图片、图表；指令如"这一页拆成两页""表格转图表""这页风格对齐上一页""每页加 30 秒演讲备注"；"改一页不影响其他页"；官方称之为"人机双写" — [AIbase](https://www.aibase.com/zh/news/31036)；品玩同日报道标题"支持自定义模板与局部编辑"，称可"拆页、调图表、换数据而无需整份重做" — [品玩 2026-09-15](https://www.pingwest.com/w/317420)
- Changelog 5.4.0（2026-08-20）"支持本地 PPT AI 编辑撤销与重做"；5.8.0（2026-10-09）独立文件浏览器窗口，本地文件可与主窗口分开预览和 AI 编辑 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 任务型"幻灯片"流程预览界面提供"重新生成 / 更换模板 / 下载导出"，可按页浏览，初稿后用自然语言要求具体修改（如某页加图表） — [腾讯云开发者社区 2026-04-25](https://cloud.tencent.com/developer/article/2660456)
- 技能版 PPT：逐页预览后在对话中要求改动（换图表类型、改标题措辞），无需手工编辑 — [网易](https://www.163.com/dy/article/L7MQ8A3G0534J1O1.html)
- 官方对话文档：追问示例"再加一个饼图""表格按销售额排序""帮我导出成 PDF"，WorkBuddy "会基于之前的对话上下文继续处理"，无需重述背景 — [w3cschool 镜像官方文档](https://www.w3cschool.cn/workbuddydocs/workbuddy-conversation.html)
- HTML 海报实测：修改请求如"标题放大""卡片改两列""加场景标签栏"，WorkBuddy 直接编辑 HTML 文件并刷新预览；内容溢出修了 3–4 轮（缩 padding、缩字号、精简文案、改两列）；建议"一次说清所有要求、批量提修改、简单修改用轻量模型（MiniMax）、约 15 轮后开新对话" — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- 图片生成教程：不满意则换措辞重新描述（例："改成插画风、更温暖，之前太抽象"），无局部编辑说明 — [腾讯云开发者社区 2026-07-10](https://cloud.tencent.com/developer/article/2707346)
- Changelog 5.5.0（2026-09-01）新增图片处理：去水印/去文字、修复、增强、抠图 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 多轮上下文弱点：V2EX 用户反映"忘记前文"；奇连指南称长会话会"前说后忘"或跑偏，建议开新任务重述关键上下文 — [V2EX](https://www.v2ex.com/t/1232713)；[奇连 AI](https://iqilian.com/learn/workbuddy-sanzhong-gongzuo-moshi/)

### 推断
- 对海报类：只有走 Ardot 画布才有真正的元素级局部编辑；走 HTML/图片生成路径则是"改源码/重生成"，没有画布。
- 对 PPT：2026-09 之后的内置 PPT 流程已具备"选中元素 + 指令"的局部修改，是目前最成熟的局部修订体验。

### 空白
- 未找到对"多轮修改中已确认内容是否被意外改动"的系统性评测；只有"改一页不影响其他页"的官方声明。
- 未找到 Ardot 画布局部修改的质量实测（知乎实测文返回 403）。

---

## 关键问题 5：输出文件格式及可编辑性

### 结论
PPT 输出为标准 .pptx（PowerPoint / WPS / Keynote 可编辑，可转入腾讯文档协作），也可导 PDF；Ardot 设计稿可导 PNG / SVG / PDF 并支持切图标注，PPT 可导 PPTX/PDF；通用 Agent 可产出 docx / xlsx / md / html / csv / pdf，HTML 海报需经 Chrome 截图转 PNG。Ardot 文档未提 HTML 导出。

### 已引用事实
- Ardot 官方文档：PPT 可导出 PPTX 或 PDF；编辑器"专业导出"列出 PNG、SVG、PDF 并支持切图标注；PPTX 可用 PowerPoint 或 WPS 继续编辑；页面未提及 HTML；设计稿"存储在云端"；另可"生成应用"（基于画布状态生成代码，可继续用对话改代码，如"用 React 重写""加 dark mode"） — [官方 Design-Idea 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Design-Idea)
- Ardot 平台本身（2026-05-18 公测）：生成"可编辑设计稿"而非普通图片，文字、颜色、布局可改；支持 App 页面、官网、海报、插画、PPT；可导入 Figma 文件保留布局/样式/组件；可调用团队组件库；设计稿连同变量/组件/布局数据可进 CodeBuddy 生成代码，兼容 Cursor、Claude Code 等 MCP IDE；支持多人评论、版本对比、权限中心 — [IT之家 via 新浪科技 2026-05-18](https://finance.sina.com.cn/tech/digi/2026-05-18/doc-inhyhpys0880787.shtml)
- 知乎实测称设计创意模式对标 Figma AI / Canva AI，可在 Ardot 编辑器拖拽修改，支持 PNG、SVG、PDF 导出 — [知乎实测（搜索摘要，页面 403）](https://zhuanlan.zhihu.com/p/2067043420254868920)
- PPT：标准 .pptx 保存到本地磁盘，兼容 PowerPoint、WPS、Keynote；不支持复杂动画和交互效果 — [网易](https://www.163.com/dy/article/L7MQ8A3G0534J1O1.html)；2026-09 升级后可在 PowerPoint/WPS 编辑并进腾讯文档协作 — [AIbase](https://www.aibase.com/zh/news/31036)
- 海报（HTML 路径）：产出 1080×1440（3:4）HTML + .txt 文案文件（标题、正文、10 个话题标签、5 个备选标题）；导出方式一（推荐）让 WorkBuddy 用 Chrome headless 截图为 PNG，方式二手动截图；**必须安装 Chrome**；重复导出会读到缓存旧图（需加 `?v=2` 或删旧 PNG） — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- 产物面板：5.4.4 "保留最终交付文件、隐藏中间文件"；5.7.0 右侧面板新标签页直接查看产物，Markdown/HTML 预览支持缩放、HTML 产物支持页内查找；5.8.0 统一 Markdown、CSV、HTML 产物工具栏；5.5.3 本地 Office 预览 Word/表格最高 100MB、演示文稿 500MB；4.21.0 PDF/CSV 预览 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 支持上传的输入类型：PDF、Word、TXT、Markdown、Excel、CSV、PPT、常见图片、ZIP/RAR、代码文件 — [w3cschool 镜像官方文档](https://www.w3cschool.cn/workbuddydocs/workbuddy-conversation.html)
- 官网深度研究产物：.md / .pptx / .docx — [codebuddy.cn/work](https://www.codebuddy.cn/work/)
- 交互式课件做法：用 HTML 嵌入 WPS 演示文稿（社区教程） — [CSDN](https://blog.csdn.net/2302_81075415/article/details/159803020)

### 推断
- 若用 Ardot 路径做海报，可获得矢量/图层级可编辑稿（SVG/PDF）；若让通用 Agent 做，产物是 HTML/PNG，不可在设计软件中分层编辑。
- 未见 Figma 导出（只有 Figma 导入），因此"在 Figma 中继续编辑"目前无官方证据。

### 空白
- 未找到 Ardot 是否支持 .fig/.sketch 导出，以及 PNG 导出的分辨率/倍率说明。
- 未找到官方对 PPT 内字体嵌入、母版/版式是否保留的说明。

---

## 关键问题 6：定价、免费额度、企业版

### 结论
个人版 2026-07-01 起分体验版（免费，500 积分/月）、标准版 99 元/月（2000 基础积分）、高级版 199 元/月（4000）、旗舰版 999 元/月（20000），连续包月 7 折、连续包年 5.6 折，加油包 50 元/1000 积分；企业 SaaS 版 198 元/人/月起，专有云版 316 元/人/月（100 席起）。曾长期提供免费 Hy3 模型与新用户 5000 Credits 等促销。用户普遍反映积分消耗快、计费不透明。

### 已引用事实
- 官方定价页（2026-07-01 起生效）：体验版 0 元 / 标准版 99 / 高级版 199 / 旗舰版 999 元每月；连续包月 70 / 140 / 700 元；连续包年 672 / 1344 / 6720 元；基础积分 500 / 2000 / 4000 / 20000 每月，限时加赠后 500 / 4000 / 9000 / 50000；自动任务数 3 / 15 / 30 / 99；项目数 5 / 10 / 15 / 20；资料库 5 / 10 / 20 / 50 GB；可托管应用 10 / 30 / 50 / 99；个人助手 3 / 5 / 8 / 10；免费版仅 Auto 模型调度（限时全模型可选），付费版全模型可选；免费版无 CLI；加油包 1000 积分 50 元、有效 1 个月、不退款；老付费用户 58 元/月延续至 2026-12-31，2027-01-01 起 70 元/月；免费版 500 积分于 2026-07-01 终止并在 7 月一次性补偿 — [官方 Pricing](https://www.workbuddy.cn/docs/workbuddy/Pricing)
- 企业版：2026-04-28 宣布 5 月 15 日起"企业旗舰版"更名"SaaS 企业版"（78 → 198 元/人/月），"企业专属版"更名"专有云企业版"（158 → 316 元/人/月） — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)；企业版按席位 198 元/人/月起、每席 2000 积分团队共享，专有云 316 元/人/月、100 席起 — [搜索摘要（WorkBuddy 手册/企动云）](https://neohoy.github.io/workbuddybook/reference/pricing.html)
- 发布时（2026-03）商业模式：基础功能免费、新用户送 5000 Credits、增值服务付费、企业版按团队规模收费 — [新浪财经 2026-03-09](https://finance.sina.com.cn/stock/t/2026-03-09/doc-inhqksxn6971236.shtml)
- 促销与事件：2026-04-02 登录异常补偿 1000 Credits；2026-08-04 免费 Hy3 延期至 8-31；2026-10-12 阶跃 Step 5 Preview 限免 — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)
- 国际版：主页"start free"但无公开套餐表；TokenHub（国际模型网关）Lite $7 / Standard $17 / Pro $51 / Max $103 每月，新账号每模型 100 万免费 token（至 2026-12-31） — [DevelopersDigest 2026-10-16](https://www.developersdigest.tech/blog/tencent-workbuddy-explained-pricing-free-models-2026)
- 用户反馈：2000 积分 26 个问题用掉约 1900；"只是便宜，免费期过了就换千问办公"；企业席位价格是个人会员两倍而积分只有一半，计费规则不透明 — [V2EX](https://www.v2ex.com/t/1232713)；[V2EX 开源替代品帖（搜索摘要）](https://www.v2ex.com/t/1246295)
- 海报案例积分：含多轮修改约 200–300 积分 — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- 企业微信版：免费试用额度有限，用完需向管理员申请 — [钛媒体 via 搜狐 2026-10-20](https://www.sohu.com/a/1090078197_122014422)

### 推断
- 大宇云等导购站仍写"专业版 58 元/月"，属 2026-07 之前旧方案，不应采信。

### 空白
- 官方页面未公布各操作的积分消耗表（指向单独"积分说明"页，未抓取）；设计创意/Ardot 生成的积分消耗未公开。

---

## 关键问题 7：技术/产品架构（模型、Skills、MCP、专家团、沙箱、浏览器）

### 结论
架构 = 本地桌面 Agent（~/.workbuddy 配置目录，与 CodeBuddy 同构）+ Auto 模型路由（国内版以混元 Hy3/Hy4 为核心，可切 DeepSeek / GLM / Kimi / MiniMax / Step，支持自带 API Key）+ SKILL.md 技能体系（兼容 OpenClaw 格式，有技能市场与安装前安全扫描）+ MCP 连接器（OAuth、mcp.json）+ 100+ 专家角色与 Teams 多智能体 + 文件夹级授权沙箱 + 内置浏览器 + Ardot 设计画布。

### 已引用事实
- 模型：发布时可切换混元、DeepSeek、GLM、Kimi、MiniMax — [TechNode 2026-03-09](https://technode.com/2026/03/09/tencent-launches-openclaw-like-workplace-ai-agent-workbuddy/)；默认"Auto 模型"按任务类型自动路由，国内版以混元为核心，进阶用户可配置外部 API — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)；腾讯称 Hy3 Preview 使 WorkBuddy 首响速度提升 54%、平均任务时长缩短 47%（2026-06-05 新闻稿，厂商口径） — [DevelopersDigest](https://www.developersdigest.tech/blog/tencent-workbuddy-explained-pricing-free-models-2026)；GLM-5.2 内置（政务版 2026-06-17）、Step 5 Preview（2026-10-12）、2026-07-08 Hy3 算力排队并于 7-09 扩容 — [Baidu Baike (en)](https://baike.baidu.com/en/item/WorkBuddy/1915792)；企业微信管理员可选混元/DeepSeek — [钛媒体 via 搜狐](https://www.sohu.com/a/1090078197_122014422)
- Skills：技能以 SKILL.md 定义方法、输出格式与注意事项，兼容 OpenClaw 社区格式，可由 AI 生成草稿或手写放入 `~/.workbuddy/skills/` — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)；技能市场 4.5.10（2026-03-13）、SkillHub 4.6.2（2026-03-20）、安装前安全扫描与版本管理 4.7.5（2026-03-30）、插件管理 5.7.0（2026-09-28） — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；官方文档有"技能市场"与"创建你自己的技能"实践 — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)
- MCP/连接器：MCP OAuth 授权 4.7.3（2026-03-28）；自定义 MCP 可编辑 mcp.json 5.6.0（2026-09-19）；连接器含 QQ 邮箱、Outlook、Gmail（海外） — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；内置连接器含 QQ 邮箱、腾讯文档、腾讯地图、有道云笔记 — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)；与 CodeBuddy 的 skills/ 目录和 mcp.json 结构基本相同，可直接复制迁移，连接器需在 UI 点 Trust 激活 — [Jet C++ Blog（搜索摘要）](https://jycpp.github.io/2026/26-08-16-CodeBuddy%E4%B8%8EWorkBuddy%E5%AF%B9%E6%AF%94%E5%8F%8ASkill%E5%92%8CMCP%E8%BF%81%E7%A7%BB.html)；可把腾讯混元生图封装为 MCP Server 以 SSE 方式接入 — [CSDN（搜索摘要）](https://blog.csdn.net/weixin_29050513/article/details/167333827)
- 专家与多智能体：专家 = "人设 + 方法论 + 工具链"；Teams 由 lead agent 协调多个子 Agent 并行 — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)；专家团自动路由 4.22.12（2026-04）；Teams 协作、项目看板、成员间任务交接 5.0.0（2026-06-03） — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；100+ 内置专家角色 — [TechNode 2026-05-29](https://technode.com/2026/05/29/tencent-launches-workbuddy-productivity-ai-agent-for-global-users/)
- 安全/沙箱：文件处理"默认在本地完成"，只能访问显式授权文件夹，高危操作二次确认 — [w3cschool 镜像官方文档](https://www.w3cschool.cn/workbuddydocs/workbuddy-conversation.html)；官方文档有"默认权限与安全沙箱"页 — [官方 Overview](https://www.workbuddy.cn/docs/workbuddy/Overview)；沙箱拦截卡片显示原因并支持允许/拒绝列表 5.1.0（2026-06-13）；全权限模式下高积分工具也需确认 5.9.1（2026-10-22） — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；国际版声称传输 TLS、静态 AES-256 — [DevelopersDigest](https://www.developersdigest.tech/blog/tencent-workbuddy-explained-pricing-free-models-2026)
- 浏览器：详情面板含内置浏览器 — [w3cschool 镜像官方文档](https://www.w3cschool.cn/workbuddydocs/workbuddy-conversation.html)；HTML 预览新窗口改在内置浏览器打开 5.9.1 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；海报导出依赖本机 Chrome headless — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- 设计层：Ardot 由腾讯云 CodeBuddy 团队开发，2026-05-18 公测 — [搜索摘要（华尔街见闻转述）](https://www.sohu.com/a/1024763681_121123989)；WorkBuddy 通过手机号自动关联 Ardot 身份，"无需单独注册腾讯设计账号"，设计稿云端存储 — [官方 Design-Idea 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Design-Idea)
- 远程遥控：微信 ClawBot 4.6.4（2026-03-22）、企业微信/钉钉/飞书/QQ 机器人 4.6.0（2026-03-19）、企业微信助手流式回复 5.3.11 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)；本地定时任务需电脑开机且客户端运行 — [钛媒体 via 搜狐](https://www.sohu.com/a/1090078197_122014422)
- 最新版本：5.9.2（2026-10-26） — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)

### 推断
- WorkBuddy 本质上是 CodeBuddy（编码 Agent）的办公化外壳：同一 harness、同一技能/MCP 配置格式，加上办公文件产物面板、专家人设、IM 入口与 Ardot 设计层。
- "海报生成"的底层并非单一图像模型，而是三种实现：Ardot 可编辑设计稿、文生图模型（官方教程未点名模型）、以及 LLM 写 HTML。

### 空白
- 未找到官方架构文档；多篇第三方文章把 MCP 误写为 "Model Control Protocol"。图片生成底层模型（是否混元生图）未被官方教程点名。

---

## 关键问题 8：用户评价、对比与已知弱点

### 结论
正面集中在"免部署、对普通办公人群友好、腾讯系打通、专家团/多智能体"；弱点集中在：幻觉（尤其日期/时间）、复杂计算出错并"编理由圆谎"、长文档截断（>30 页 PPT / >50 页文档失败）、中文乱码（流式切断 UTF-8，5.3.6/5.3.7 已修）、算力排队、文件误删、上下文遗忘、积分消耗快、营销过猛。海报/PPT 相关的具体弱点：模糊提示产出空洞、HTML 海报排版溢出需多轮、PPT 配图与内容不符（5.9.1 修复）。

### 已引用事实
- 幻觉：企业微信周报把日期写成 2025 年 10 月（早一年），模型先否认后承认"幻觉"；次日复测仍错，因搜索关键词本身用错年月，显式写"2026年10月16日"才修复；10:10–10:47 定时任务反复"模型队列已满"失败，标"空闲"的模型也失败；企业微信消息单向同步 — [钛媒体 via 搜狐 2026-10-20](https://www.sohu.com/a/1090078197_122014422)
- 数码博主吐槽"需要严谨性的内容容易出错" — [新浪新闻（搜索摘要）](https://www.sina.cn/weibo/detail/5357702775572940.html)
- 踩坑记录（2026-07-06）：模糊需求 → 冗长低价值输出；一个窗口叠多任务 → 越来越乱；超长文档上传 → 关键内容被截断；复杂数据计算出错并"编造理由圆谎"；自动清理文件会误删重要文件 — [腾讯云开发者社区 2026-07-06](https://cloud.tencent.com/developer/article/2704221)
- V2EX：30 页以上带严格模板的 PPT 从未成功，50 页以上文档出胡话，30 页以上 PDF 效果差，但"短的营销海报和文章效果不错"；知识库单文件 10MB 限制；忘记上文；写代码"思考半小时告诉你 1+1=2"；手机端收不到 PC 任务；电梯广告与营销电话；对比：Trae Solo、千问办公被部分用户认为更好用，OpenClaw 则不如 WorkBuddy 方便 — [V2EX](https://www.v2ex.com/t/1232713)
- 中文乱码：生成文件随机出现 �（U+FFFD），长文档、中文密集场景高发，根因为模型网关流式输出切断 UTF-8 多字节字符 — [知乎（搜索摘要）](https://zhuanlan.zhihu.com/p/2067020351893254539)；官方 5.3.6（2026-07-28）修复"UTF-8 字符被流式分块切断导致的偶发乱码"、5.3.7（2026-07-29）修复"SSE 流式传输中文乱码" — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- PPT 配图不符：5.9.1（2026-10-22）"优化 PPT 生成的素材核对，减少内容与配图不符"；5.4.4 修复"部分设备无法生成、公式重算结果不准确"；5.3.11 修复 Windows 上 tencent-pptx 技能无法启用 — [官方 Changelog](https://www.codebuddy.cn/docs/workbuddy/Changelog)
- 海报实测踩坑：模糊请求出空洞内容；HTML 超过 1440px 被截断；重复导出读缓存旧图；违禁词（"最强""神器"）需显式要求规避 — [腾讯云开发者社区 2026-07-21](https://cloud.tencent.com/developer/article/2713388)
- Craft 模式会"过度改动"（删掉想保留的内容、破坏格式、跑偏）；桌面清理 FAQ 案例中文件疑似丢失；全盘设为工作目录风险最高 — [奇连 AI](https://iqilian.com/learn/workbuddy-sanzhong-gongzuo-moshi/)
- 正面：专家团多智能体协作超预期、UI 干净、接入微信小程序方便；连接 WPS 与腾讯文档后够用 — [V2EX](https://www.v2ex.com/t/1232713)；企业微信场景上下文感知好（"那个项目"可解析），1 小时视频摘要约 14 分钟 — [钛媒体 via 搜狐](https://www.sohu.com/a/1090078197_122014422)
- 其他体验：偶尔卡死需重启；功能入口较深、任务依赖只支持线性；复杂任务偶尔"只做一半"、错误恢复一般；首次处理大量文件云端启动慢 — [搜索摘要：知乎/CSDN/AIXQ 多篇](https://www.aixq.cc/26228.html)
- 国际视角弱点：英文文档与定价信息稀薄；桌面 Agent 把本地文件内容发往托管模型 API，合规需审视；Agent 循环重复发送长上下文导致免费 token 耗尽更快 — [DevelopersDigest 2026-10-16](https://www.developersdigest.tech/blog/tencent-workbuddy-explained-pricing-free-models-2026)
- 通过 IM 远程派发任务时"任务描述和产出文件会经过 IM 中转"，企业敏感数据需注意 — [cfanzp 指南](https://cfanzp008.github.io/workbuddy-core-usage-guide/)
- 出现开源替代品"魔力工作台"（V2EX 用户项目） — [V2EX（搜索摘要）](https://www.v2ex.com/t/1246295)

### 推断
- 对于海报/PPT 类创作，WorkBuddy 的主要短板不在"能不能生成"，而在：缺少前置需求澄清（海报路径）、排版需多轮试错、长内容稳定性、以及对事实/日期的幻觉；这些正是竞品可差异化的点。

### 空白
- 未找到针对 Ardot 生成海报中"中文文字渲染"质量的公开评测（图片生成路径的中文字渲染问题也无直接证据）。
- 未找到独立媒体（36氪/量子位/机器之心）对 WorkBuddy 创作功能的系统性横评；36kr 英文页抓取失败。
