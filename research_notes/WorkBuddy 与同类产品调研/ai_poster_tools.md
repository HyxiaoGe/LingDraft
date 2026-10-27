# AI 宣传海报 / 营销图生成产品调研（中国 + 全球，截至 2026-10-27）

> 说明：本笔记按"关键问题"组织，每节分 Takeaway / Cited Findings（带 URL 的可查证事实）/ Inferences（推断）/ Gaps（未能查证）。产品名保留原文。标注"厂商自述"的数字未经独立验证。多篇国内 2026 年横评（CSDN/AtomGit 上署名"金山毒霸电脑医生"的系列文章）明显在推广 EasyClaw，其排名不可信，但其对各工具交互流程的描述仍有参考价值，已单独标注。

---

## 关键问题 1：全球主流工具的完整工作流（输入 → 生成 → 编辑 → 品牌 → 导出）

### Takeaway
全球工具已明显分化为两条路线：**"模板/图层优先"**（Canva Magic Design、Adobe Express Generate Template、Figma Buzz、Kittl、AdCreative.ai）从一开始就输出文字为文字层的可编辑设计；**"图像模型优先"**（Ideogram、Recraft、Nano Banana Pro、GPT Image、Lovart）输出平面位图，再用"拆层"类后处理（Canva Magic Layers、Ideogram Layerize、Lovart Edit Elements、Seedream 5.0 Pro 图层分离）把位图重新变成可编辑层。2026 年的标志性变化是 Canva Craft（2026-10-22）把"先规划版式/文字，再渲染像素"做进了图像模型本身。

### Cited Findings

**Canva（Magic Design / Magic Studio / Magic Layers / Canva Craft）**
- Magic Layers 于 2026-03-11 发布公开测试（美/英/加/澳），把平面图/AI 生成图转换为 Canva 内的多图层可编辑设计：文字恢复为可编辑文本框、对象拆成独立可移动元素、背景保留为底层、保持原版面关系；属于 Canva 自研基础模型 "Canva Design Model" 的能力，该模型"从一开始就生成可编辑的设计"，并支撑 ChatGPT、Claude、Microsoft Copilot 集成 — [Canva Newsroom](https://www.canva.com/newsroom/news/magic-layers/)；[Business Wire 2026-03-11](https://www.businesswire.com/news/home/20260311951174/en/Canva-Introduces-Magic-Layers-Turning-Static-AI-Outputs-Into-Editable-Designs)；[PetaPixel 2026-03-11](https://petapixel.com/2026/03/11/canvas-new-ai-tool-turns-flat-images-into-editable-layers/)
- Magic Layers 发布时只接受 PNG/JPG 输入 — [Yahoo Tech](https://tech.yahoo.com/apps/articles/kind-magic-canva-launches-magic-160158018.html)
- 2026 年 6 月 Magic Layers 接入 Gemini 与 ChatGPT：Nano Banana 生成的图可直接送入 Magic Layers 变为可编辑设计 — [Canva Newsroom](https://www.canva.com/newsroom/news/magic-layers-ai-assistants/)；[AlternativeTo 2026-06](https://alternativeto.net/news/2026/6/canva-introduces-magic-layers-for-editing-chatgpt-generated-images/)
- Canva Craft（Canva 首个自研图像模型）于 2026-10-22 发布：两阶段架构——"图像规划模型"先解析提示词写出详细构图计划（版式、文字、层级），再由独立"渲染模型"出图；可生成 55 种语言文字；2K 分辨率、30+ 宽高比；训练数据为 Canva 内容库中数百万人工制作模板（海报、社媒、信息图、菜单等），后经设计师反馈 SFT+RL；可通过 Dream Lab 和 Public API 使用，MCP Server "即将推出"；页面称支持"JSON boundary boxing 把图像转为可编辑图层"，架构"非常适合 Magic Layers" — [Canva Newsroom: Introducing Canva Craft](https://www.canva.com/newsroom/news/canva-craft/)；[TechCrunch 2026-10-22](https://techcrunch.com/2026/10/22/canva-releases-its-first-image-generation-model/)；[WinBuzzer 2026-10-23](https://winbuzzer.com/2026/10/23/canva-craft-plans-layouts-before-generating-images-xcxwbn/)
- Canva 自称 Craft 在 DesignArena 榜单首发第 4 名（厂商自述，未独立验证）；未发技术报告，未公布定价 — [tech-insider](https://tech-insider.org/canva-craft-first-image-model-designarena-2026/)
- Magic Design 一次生成约 8–12 个完整可编辑版式（第三方说法，无 Canva 官方数字） — [buildfastwithai](https://www.buildfastwithai.com/ai-tools/canva-magic-design)；[webisai](https://webisai.com/canva-magic-the-complete-guide-to-ai-powered-design-in-2026/)
- Brand Kit 为 Pro 功能，存 logo/色板/字体/品牌指南；评测指出 Brand Kit 只作用于"图像周围的设计"（颜色、字体、logo），不深入到图像生成本身；无定制时产出"和其他 Canva 帖子一个样" — [Marqeable 评测](https://www.marqeable.com/blog/canva-ai-image-generator-review-marketing/)；[sawankr](https://sawankr.com/courses/canva/canva-ai-features-2026-magic-studio-guide)
- Canva AI 2.0 于 2026 年 4 月进入研究预览，加入可后台执行周期性设计任务的 agentic 工具 — [Wikipedia: Canva](https://en.wikipedia.org/wiki/Canva)
- 国内 2026-05 评测：Canva AI 描述海报后约 5 秒给出 3 个模板选项，文字可直接编辑；免费版有水印限制，AI 生图质量中等 — [CSDN 2026-05-19（推广文，仅取流程描述）](https://blog.csdn.net/2401_89926894/article/details/161106579)

**Adobe Express + Firefly（Generate Template）**
- 官方帮助页（2026-04-30 更新）：用文字描述生成社媒帖/传单/海报/卡片的**可编辑模板**，从结果中选一个后进入 "Edit template" 定制；可点 "Generate more results" 获取更多，或对某个结果 "See variations" 生成相似设计；无固定数量 — [Adobe Help: Create editable templates with generative AI](https://helpx.adobe.com/express/web/create-with-templates/text-to-template.html)
- AI 传单页：输入提示词（建议 5 词以上，风格词如 bright/neon/minimalistic 写在提示里）；可同时上传图片/图形使生成模板包含它；默认尺寸 8.5×11 英寸，Resize 可改自定义尺寸；选中模板后替换文字、品牌、logo、颜色、字体、照片；页面未提及品牌套件自动应用与积分消耗 — [Adobe Express AI Flyer](https://www.adobe.com/express/create/ai/flyer)；[AI Template Generator](https://www.adobe.com/express/create/ai/template-generator)
- 移动端 2025-12 版本注明仅支持英/中/日/韩/德/法六种语言 — [Adobe Help (mobile, my_ms)](https://helpx.adobe.com/my_ms/express/web/adobe-express-on-mobile/create-and-edit-designs/text-to-template-mobile.html)
- 第三方评测称 Premium 每人每月 250 生成积分；重度 AI 视频/本地化需单独 Firefly 计划（无官方来源） — [work-management.org](https://work-management.org/marketing/creators/adobe-express-review/)
- 国内评测：Firefly 英文提示最佳、中文理解弱；生成的是素材，需到 PS/AI 里排版 — [CSDN 2026-05-19](https://blog.csdn.net/2401_89926894/article/details/161106579)；[agent.csdn 2026-05-19](https://agent.csdn.net/6a17f9d510ee7a33f276180c.html)

**Microsoft Designer**
- 官方 FAQ：Brand Kit 功能根据品牌名+描述生成 logo/色板/字体/语气建议，一次只能保存一个品牌套件；产品许可为个人非商业用途 — [Microsoft Support FAQ](https://support.microsoft.com/en-us/designer/frequently-asked-questions-about-microsoft-designer)；[Microsoft Tech Community](https://techcommunity.microsoft.com/blog/microsoft365insiderblog/elevate-your-brand-identity-with-ai-powered-microsoft-designer/4221979)
- 评测：生成版式含文字+图片+配色；建议"先生成不含关键文案的视觉，再把最终文案作为可编辑画布文字加上"（因 AI 文字不可靠）；编辑工具有去背景/换背景、擦除、裁切、Restyle；模板库比 Canva 小，复杂图层/精确排版受限；一条用户评价称无法输出其他宽高比/文件格式（存疑）；输出带 C2PA 凭证 — [toolsforhumans](https://www.toolsforhumans.ai/ai-tools/microsoft-designer)；[therundown](https://www.therundown.ai/tools/microsoft-designer)；[aitoolsofthetrade](https://aitoolsofthetrade.com/reviews/microsoft-designer)
- 积分数字各源冲突（免费 15/月 vs M365 订阅 60/月共享） — [therundown](https://www.therundown.ai/tools/microsoft-designer)；[whataidoineed](https://whataidoineed.com/tool/microsoft-designer)
- 企业侧品牌一致性已转移到 Microsoft 365 Copilot Brand Kit：2026-07 起可在 Copilot in PowerPoint 直接套用 logo/图标/主题/字体；上传品牌指南 PDF 可自动抽取 logo/色彩/字体/语气；2026-09 报道有"strict brand adherence"模式限制为批准模板 — [What's New in M365 Copilot 2026-07](https://techcommunity.microsoft.com/blog/microsoft-copilot-blog/what%E2%80%99s-new-in-microsoft-365-copilot--july-2026/4538332)；[sympmarc 2026-08-04](https://sympmarc.com/2026/08/04/brand-kits-in-microsoft-365-two-features-one-goal-and-why-you-should-care-about-both/)

**Figma（Figma Buzz / Make / Agent）**
- Figma Buzz 是面向营销团队的模板化素材工具：设计师在模板中锁定哪些部分可编辑；上传 .xlsx/.csv 把列映射到可编辑图层做批量生成（"数千张"）；2026-05-30 更新：表格视图多选批量改文案/换图/改尺寸，Resize 支持单张或整组按预设/自定义渠道尺寸；2025-10 模板接入设计系统组件属性；AI 功能为翻译/改写文案和生成背景图 — [Figma Buzz](https://www.figma.com/buzz/)；[Figma Blog: Introducing Figma Buzz](https://www.figma.com/blog/introducing-figma-buzz/)；[Figma Blog: new features](https://www.figma.com/blog/new-features-in-figma-buzz/)；[DevDash Labs 评测](https://devdashlabs.com/insights/figma-buzz-a-game-changer-for-smb-marketing-teams-(hands-on-review))
- Figma App in ChatGPT 可把提示词转为 Buzz 视觉素材和 Slides — [Figma Blog](https://www.figma.com/blog/slides-buzz-figma-app-in-chatgpt/)
- Figma Make 定位是 prompt → 可用代码原型 / Auto Layout 帧，Make Image 做图像生成；未检索到 Make 针对海报的专门能力 — [LogRocket 2026](https://blog.logrocket.com/ux-design/figma-ai-2026-quick-overview/)；[banani](https://www.banani.co/blog/figma-ai-features-review)

**Lovart（"AI 设计 Agent"）**
- 首页：Lovart 2.0，在提示框输入设计需求（示例：咖啡店菜单海报、婚礼海报）；有 Projects/Skills/Assets/Delegated tasks/Clipper 模块 — [lovart.ai](https://www.lovart.ai/)
- Edit Elements（2025-11-13）：一键把成品海报拆为文字/主体/背景独立层，文字可改内容、字体、颜色、排版；局限：小字、密集图标、光影重叠区易误判；描边/渐变等字效可能丢失；测试中一张中文 banner 的字体被换掉、部分图标未干净分离；"5 积分可对同一张图无限微调" — [agiyes](https://www.agiyes.com/ainews/lovart-edit-elements/)
- Touch Edit：在画面上标记区域+自然语言描述修改；项目可导出分层 PSD — [filmora 评测](https://filmora.wondershare.com/video-editor-review/lovart-ai-review.html)；[pollo 评测](https://pollo.ai/hub/lovart-ai-review)
- 底层路由第三方模型（GPT Image-1、Flux Kontext 等）；一次可批量出最多 40 张 — [pixai 评测](https://blog.pixai.art/en/lovart-ai-review-2026-what-it-does-well-where-it-falls-short-and-alternatives/)
- 定价各源不一（Starter $19 / Basic $32 / Pro $72；App Store 显示 Pro $90；也有 $19–$169 五档说法）；积分不滚存；有评测称实际消耗远超文档 — [framia](https://framia.converge.ai/blog/lovart-ai-review/)；[App Store](https://apps.apple.com/dz/app/lovart/id6746544072)；[myclaw](https://myclaw.ai/blog/lovart-ai-review)

**Recraft（V4 / V4.1）**
- V4 于 2026 年 2 月发布；V4.1 于 2026-05-14 成为默认 — [Wikipedia: Recraft](https://en.wikipedia.org/wiki/Recraft)；[invideo](https://invideo.io/blog/recraft-ai-image-generator/)
- 官方文档：文字渲染面向短/中等长度短语，对版式和排印保真较好，适用于信息图、菜单、标识、包装 — [Recraft Docs V4](https://www.recraft.ai/docs/recraft-models/recraft-V4)
- 原生 SVG 输出（非描边位图），可在 Illustrator/Figma/Sketch 编辑；文字"内置于构图"而非后贴；产品线 Standard/Pro/Vector/Pro Vector，Pro 原生 2048² — [Replicate Blog](https://replicate.com/blog/recraft-v4)；[therundown](https://www.therundown.ai/tools/recraft-v4)
- 可用参考图创建自定义 Style 并跨生成复用（品牌一致性核心卖点）；评测称段落级/多行版式仍落后 Ideogram；V4.1 仍不能只动一个对象或只改一块颜色，需重提示或外部编辑 — [versely](https://www.versely.studio/blog/recraft-v3-vs-ideogram-v3-brand-graphics-showdown-2026)；[digitalbydefault](https://digitalbydefault.ai/blog/recraft-ai-design-tool-review-2026)；[therundown](https://www.therundown.ai/tools/recraft-v4)

**Ideogram（3.0 / 4.0 / 4.5 / Layerize Text）**
- Layerize Text（Beta，所有计划含免费可用）：检测位图中的文字，从图中移除并在 Studio 中重建为可编辑文本层（可改文案/字体/字号/颜色/对齐/位置）；文字下方区域被重绘，背景可能与原图不同；只有文字可编辑，照片/插画/logo 形状仍是位图；可能漏字（漏掉的字留在图里，需检查小字/日期/页脚）；不保证字体匹配，不支持上传字体；Studio 不导出 PSD/SVG，仅 JPEG/PNG/WEBP；**Layerize Text API 返回结构化文字数据 + 去字底图 + 渲染结果**，供开发者自建工具 — [Ideogram: Text Layers](https://ideogram.ai/features/text-layers/)
- Ideogram 4.0（2026-06-03，9.3B 开源权重）：聚焦图内文字、bounding-box 版式控制、色板条件、2K；官方称后续版本将直接从推理返回 alpha 通道和可编辑文本层 — [Ideogram 4.0](https://ideogram.ai/models/4.0/)；[Morphic](https://morphic.com/resources/models/ideogram-4)
- Ideogram 4.5（2026-09-30）：面向编辑——精确编辑复制未触及像素、遮罩、最多 4 张参考图、超大图裁切拼接编辑、**就地改写/翻译风格化文字并保持设计**；权重截至 2026-10-09 未发布 — [Morphic](https://morphic.com/resources/models/ideogram-4-5)；[GeneratorNest](https://generatornest.com/blog/ideogram-4-5)；[Ideogram LinkedIn](https://www.linkedin.com/posts/ideogram-ai_introducing-ideogram-45-the-most-precise-activity-7511092977612681218-uKyY)
- 3.0 官方页自称 95% 文字渲染准确率（营销数字）；评测称阿拉伯文/中文等非拉丁文字仍是短板 — [Ideogram 3.0](https://ideogram.ai/models/3.0/)；[mindstudio](https://www.mindstudio.ai/blog/what-is-ideogram-v3)

**Kittl**
- 核心是"实时文字引擎"：字效（弯曲、弧形、阴影）应用后每个字符仍可编辑；形状可双击进锚点编辑；AI Vector Generator 实际是把位图转矢量路径而非原生 SVG（评测建议生产级 SVG 用 Recraft）；约 $10–15/月，付费计划可导出 SVG/PDF — [Style Factory](https://www.stylefactoryproductions.com/blog/kittl-review)；[Fritz AI](https://fritz.ai/kittl-ai-review/)；[whataidoineed](https://whataidoineed.com/tool/kittl%20ai)

**AdCreative.ai / Pencil（广告素材生成）**
- AdCreative.ai 官方流程：Brand profile（logo、品牌色、描述、可连广告账户）→ 选尺寸（1080×1080、Story 等）→ 输入/生成标题文案 → 选图（含 iStock）→ "数秒生成数百个创意"，并自动生成社交/搜索/展示所有尺寸、直推 Meta/Google/LinkedIn；可改标题、视觉、颜色、版式、logo/按钮/punchline；每个创意有 0–100 Conversion Score（厂商称 >90% 准确，未验证）；**生成不限量，仅下载扣积分**；Starter $39/月 20 积分 1 品牌、Professional $249/月 300 积分 10 品牌、Ultimate $999/月 — [AdCreative.ai](https://www.adcreative.ai/ad-creatives)
- Pencil：Core $14/月 50 次、Growth $55/月 250 次、Pro 定制含基于 feed 的批量生成；"生成"按模型交互计数而非按成品广告；有 Asset & Brand Library 与内置编辑器；未找到可编辑图层的确证；Trustpilot 有计费投诉 — [tooljunction](https://www.tooljunction.io/ai-tools/pencil-ai)；[soku](https://soku.ai/alternatives/pencil-ai/review)；[adlibrary](https://adlibrary.com/posts/pencil-ai-review-2026)

### Inferences
- "先规划后渲染"正在成为共识架构：Canva Craft（规划模型→渲染模型）、GPT Image 2 的 thinking 模式（见问题 4）、CreatiPoster（协议模型 JSON→背景模型）本质相同；新产品若自建，LLM 负责版式/文案 JSON 的路线与头部厂商方向一致。
- 可编辑性方面，模板驱动产品（Canva/Adobe/Figma Buzz）天然是文字层；图像模型产品则统一靠"拆层"补课，而拆层普遍存在漏字、字效丢失、背景重绘失真问题（Ideogram、Lovart 均自述），说明"先位图后拆层"是次优路径，仅适合处理外部来图。
- 品牌套件在所有全球产品里都是"套用层面"（色/字/logo），未真正约束图像模型生成；Figma Buzz 的"锁定可编辑区"+表格批量是目前最严格的品牌控制机制。

### Gaps
- Canva Magic Design 每次生成数量、Brand Kit 是否自动注入 Magic Design 无官方文档确认（help 页 404）。
- Adobe Express Generate Template 的积分消耗、是否自动套用 Brand Kit 未查到官方说明。
- Lovart 官方定价页与 User Guide 未能抓取（首页无内容），导出 PSD 细节仅来自第三方。
- Figma Make 针对海报/印刷品的能力无任何来源覆盖。
- Microsoft Designer "Design Creator" 一次生成几个变体无官方说明（第三方均为 2023–2024 旧文）。

---

## 关键问题 2：中国市场主流工具的完整工作流

### Takeaway
国内产品分三类：**模板型 SaaS**（稿定设计、创客贴、美图设计室、可画、图怪兽/千图）——输入一句话或活动信息 → AI 匹配模板并填充 → 在图层编辑器里改，文字天然是文字层，中文准确度最高但"更像 AI 辅助选模板"；**模型型**（即梦/Seedream、通义万相/Qwen-Image、混元图像 3.0）——文字渲染进像素，中文能力强于海外模型但仍会出错，2026 年即梦靠 Seedream 5.0 Pro 的"图层分离"补可编辑性；**电商 Agent 型**（堆友、京点点 Oxygen Vision、稿定 Agent、美图 Agent、鹿班/羚珑）——输入商品图/SKU/Excel → 一键出整套主图/套图/海报，并带平台尺寸规范，是国内 活动促销 场景的主力。

### Cited Findings

**稿定设计 / 稿定 AI**
- 官方 AI 页：一句话生成海报、封面、宣传物料 — [稿定 AI 设计](https://www.gaoding.com/tools-ai-design)
- 新域名 gaoding.art 定位 "AI 创作平台 / AI 生图视频与电商工作流"，提供 Agent 模式、图片生成、视频生成；电商模板含 "4K 商拍精修"、"全套主图直出"、"详情页整套生成"、"直播间套装生成" — [gaoding.art](https://www.gaoding.art/)
- 电商产品页："上传产品图，生成全套主图"，电商 Agent + 场景化 Skill — [稿定 电商 Agent](https://www.gaoding.com/product)
- 海报功能页：按类型/行业/用途浏览模板，拖拽替换文字图片；"发丝级"AI 抠图；"一键智能变换设计尺寸"；导出格式与价格页面未列 — [稿定 AI 海报](https://www.gaoding.com/features/ai-poster-design)
- 2026-05-28 文章：稿定 AI 由文字提示生成**分层商业海报**，"智能图文排版"调整图文层级；可导出**分层 PSD** 或在网页编辑器改文字/图形/字效；付费会员才能用高级模板、特殊字体与分层导出 — [搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- 国内横评（推广文，仅取描述）：输入活动信息 → AI 匹配模板并自动填充 → 用户微调；中文文字 5 星；原创能力有限；免费版有水印，高清下载付费；部分模板单独售卖；"更像 AI 辅助选模板" — [agent.csdn 2026-05-19](https://agent.csdn.net/6a17f9d510ee7a33f276180c.html)；[AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)
- 对比文称稿定 AI 能力集中在"模板套用"，无现成模板的新品类/个性化风格受限 — [知乎（摄图推广文）](https://zhuanlan.zhihu.com/p/2081340366612186309)
- 稿定另有 PS 插件"稿定设计助理" — [pszhuli.com](https://www.pszhuli.com/)

**即梦 AI（Jimeng / Dreamina）+ Seedream**
- 即梦 2.1 时期即把中文文案直接写在提示词里生成；"改文字、做海报"功能调用字节 DreamPoster 模型而非通用生图模型；提供擦除/超分/局部修复/扩图 — [火山引擎开发者文章](https://developer.volcengine.com/articles/7542490624042696713)；[53AI](https://www.53ai.com/news/neirongchuangzuo/2024120543915.html)
- 官方海报功能页流程：选"文字/图片转图片"→ 输入提示词（可上传参考海报）→ 选模型/画质/宽高比或自定义宽高 → 生成；后处理：升级 4K、修饰、局部重绘（涂抹+重新提示）、进入画布加排版/图形；提到"多层编辑"但未说明导出是否保留图层；未列导出格式 — [即梦 AI 海报制作](https://jimeng.jianying.com/features/resource/poster-making-ai)
- 智能画布集成文生图、局部重绘、智能扩图、擦除、多层素材叠加、多版本对比与历史回退 — [即梦智能画布教程](https://runyoung0613.github.io/jimeng-tutorial/charpter/ch09-%E6%99%BA%E8%83%BD%E7%94%BB%E5%B8%83.html)；[CSDN 智能画布详解](https://blog.csdn.net/Makecucy/article/details/148848940)
- **Seedream 5.0 Pro（2026-07-08）**：文字描述即可把一张图拆成独立图层（示例海报拆出 10+ 层：文字、主体鹦鹉、背景、装饰），被遮挡背景自动补全，图层保留透明度可拖拽缩放，主体可替换；点选/套索/框选/涂鸦转为局部编辑指令；颜色可用 hex 或色卡参考指定、材质可替换；草图渲染可把文字放入指定坐标区域；示例含"冬季促销海报：标题+折扣条款+活动日期"的密集文字；支持中英+法德俄日韩西阿等 10 余种语言，阿拉伯文从右到左正确；自述"更细粒度文字渲染仍有提升空间"；无基准数据 — [ByteDance Seed Blog](https://seed.bytedance.com/en/blog/beyond-generation-it-understands-design-introducing-seedream-5-0-pro)；[腾讯新闻 2026-07-13](https://news.qq.com/rain/a/20260713A08PLY00)
- 国内横评（推广文）：即梦"全场5折"出现字形错误、偶发标题乱码；中文文字 5/10；"不是完整海报工具，文字和排版需另加"；免费额度有限 — [AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)；[CSDN 2026-05-19](https://blog.csdn.net/2401_89926894/article/details/161106579)
- 即梦 Agent 模式可做电商产品图全流程 — [塔猴](https://www.tahou.com/article/193930770432616453)

**美图设计室（DesignKit）**
- App Store 功能列表：「AI 海报」智能排版、一键换图改文字；海报模板适配短视频封面/公众号/社群，一键套用各平台规范尺寸；「AI agent」一句话生成种草文案+图文排版；名片/宣传单/包装样机/店铺视觉"一键套用模板，搭建统一品牌视觉体系" — [App Store 美图设计室](https://apps.apple.com/us/app/%E7%BE%8E%E5%9B%BE%E8%AE%BE%E8%AE%A1%E5%AE%A4-%E8%AE%A9%E5%95%86%E4%B8%9A%E8%AE%BE%E8%AE%A1-%E5%A5%BD%E7%9C%8B%E5%8F%88%E8%A7%81%E6%95%88/id875654777?l=zh-Hans-CN&platform=mac)
- 官方 Agent 提示词教程："一句话搞定电商套图" — [美图设计室官网文章](https://www.designkit.cn/article/agenttishici-dianshangtaotu)
- 国际站 designkit.com：Product Listing Generator、一张商品图生成主图/A+/全套 marketplace 图、Batch Photo Editor、Virtual Try-On、AI Fashion Model；未说明品牌套件与导出格式 — [designkit.com](https://www.designkit.com/)
- 2026-05-28 文章：上传商品图 → AI 生成促销拼图/banner；价签、折扣标签、标题为**文字层+矢量装饰**，点击文本块即可改；AI 擦除；抠图精度强，艺术/品牌级深度有限 — [搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- 2026 电商 8 款工具评测：抠图强、移动端方便，大图清晰度一般，适合个人卖家白底图/简易主图 — [知乎 2026 电商 AI 作图评测（页面 403，取自搜索摘要）](https://zhuanlan.zhihu.com/p/2086030235938584484)
- 横评（推广文）：选海报类型 → 输入需求或选模板；简单文字稳定，复杂版式有偏移；免费版水印，去水印约 28 元/月 — [AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)
- 美图自述 AI 商品图在行业比赛"快消美妆""家装家电"双赛道冠军（厂商自述） — [美图官方](https://www.meitu.com/zh/media/398)

**创客贴**
- 应用商店描述：输入一句话生成宣传海报；输入文案后批量生成并自动排版；生成后可再编辑；v5.0.9（2026-04） — [百度手机助手](https://mobile.baidu.com/appitemp/3846464)；[小米应用商店](https://app.mi.com/details?id=com.chuangkit)
- "AI 创作 2.0"：语义改图支持像素级元素替换，自然语言指令（如"把字母 A 改为 C"）完成局部重绘并保持风格 — [知乎 创客贴 AI 创作 2.0](https://zhuanlan.zhihu.com/p/1924409234541486910)
- AI 改图：创建设计后在工具面板点 "AI 改图"，案例把标题"山野寻味"改为"山野特菌"，排版样式不变 — [知乎 创客贴 AI 改图](https://zhuanlan.zhihu.com/p/2004844250890781190)
- 第三方称 2026-02 推出圈选文字直接替换功能（时间点无其他佐证） — [aiyizu](https://www.aiyizu.cn/ai-tool/%E5%88%9B%E5%AE%A2%E8%B4%B4ai/)

**Canva 可画（中国版）**
- 2024-08 "魔力工作室"在中国上线，文字描述生成插画、花字、演示 — [新浪财经 2024-08-17](https://finance.sina.com.cn/roll/2024-08-17/doc-incixcsn5013988.shtml)
- 可画海报页：AI 生成海报入口 — [canva.cn/posters](https://www.canva.cn/posters/)
- 中国版入口 canva.cn，AI 入口为"可画 AI / 魔力工作室"；第三方称 canva.com 中文字体仅限非商用、canva.cn 图库支持商用（需核实） — [readaitime](https://www.readaitime.com/tools/canva-ai)
- 2026-05-28 文章：Magic Design 由提示/素材生成草稿，AI 生成的标题、正文、图片、背景为**独立图层**；依赖素材库匹配，独特视觉较难 — [搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- 横评（推广文）：可画 AI 7.2/10，中文文字 8/10，AI 质量中等依赖模板，高级素材与导出格式付费 — [AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)

**通义万相 / Qwen-Image / Wan**
- Qwen-Image：20B MMDiT，Apache 2.0，2025-08-04 开源，主打中英文文字渲染（厂商称 9 项基准第一、中文渲染"商用级"） — [新浪科技 2025-08-05](https://finance.sina.com.cn/tech/csj/2025-08-05/doc-infiwxmc1129205.shtml)；[知乎问题](https://www.zhihu.com/question/1936010879314397027)
- 实测：中英混排、多字体、复杂布局可用；文字与画面融合好；小字仍有错误 — [知乎实测](https://zhuanlan.zhihu.com/p/1936168049158907558)；[CSDN](https://blog.csdn.net/yihuaixu/article/details/150470483)
- Qwen-Image-Edit：首个可直接编辑中文海报的模型，大标题到小字均可精确修改；2511 版可增删改文字并保持原字体/字号/风格，支持中英 — [Hugging Face Qwen-Image-Edit](https://huggingface.co/Qwen/Qwen-Image-Edit)；[Replicate qwen-image-edit-2511](https://replicate.com/qwen/qwen-image-edit-2511)
- 阿里云百炼：万相 2.6 image 支持图像编辑与图文混排输出，纯文生图建议 wan2.6-t2i；官方称可生成海报、图文说明、信息图表并处理中英长文本 — [阿里云 万相 2.6 API](https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference)；[通义 Wan2.6 发布](https://tongyi.aliyun.com/news?id=pxwhvf%2Fsuodqg%2Fqkhh70wdrlgwogs2)
- 横评（推广文）：通义万象 7.6/10，有"海报模式"，文字中等需多次重试，每日免费额度 — [AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)

**腾讯 混元图像 3.0 / 智影 / 元宝**
- 混元图像 3.0 于 2025-09-28 开源（80B，原生多模态），可解析千字级语义、生成长文本与小字；首发仅开放文生图，图生图/编辑/多轮后续发布；已上线元宝 App，一句话生成创意海报 — [腾讯云](https://cloud.tencent.com/developer/article/2572759)；[阿里云开发者社区](https://developer.aliyun.com/article/1683724)；[腾讯新闻 2025-10-09](https://news.qq.com/rain/a/20251009A05OYU00)
- 对比评测称混元 3.0 在多样中文文字上比 Seedream 4.5 更一致 — [WaveSpeed 对比](https://wavespeed.ai/blog/posts/hunyuan-vs-seedream-comparison-2026/)
- 腾讯智影定位为视频创作（数字人、配音、字幕、文章转视频），未发现海报设计专门功能 — [ai-bot 腾讯智影](https://ai-bot.cn/sites/360.html)

**阿里 堆友（d.design）与 鹿班**
- 堆友 Agent 实测（2026-05-21）：输入一句话卖点 + 上传商品实拍图，或上传产品 Excel 批量；输出：主图+场景图、亚马逊套图 6 张（白底主图/场景/细节/包装/居家/卖点总览，按亚马逊 PC 尺寸）、详情页、同一产品延展 3 种尺寸海报（618 活动、朋友圈 1:1、社群 16:9）、小红书文案+标题+配图；选中图片用"编辑元素"自动把图片与文字分层，可单独编辑/提取/消除文字而不改样式；不足：竖排文字在手机端偏小，仍需人工把关 — [umaax 堆友 Agent 实测](https://umaax.com/duiyouagentshiceaiyijuhuashengchengshangyejidianshangshejiha/)
- 海报 Agent 把提示语中的文字内容提取出来分层分类，可替换主体图/二维码/logo，文字可选中改字号/颜色/描边 — [知乎 阿里最像 PS 的设计 Agent](https://zhuanlan.zhihu.com/p/1988181406933882617)；[腾讯新闻 2025-12-26](https://news.qq.com/rain/a/20251226A05JBO00)
- 平台背靠阿里设计体系，接入鹿班；提供 AI 模特视频、虚拟试衣、图片翻译、营销海报直出；ComfyUI/WebUI 节点工作流；素材免费商用（第三方说法） — [ai-bot 堆友](https://ai-bot.cn/sites/61378.html)；[fly63](https://ai.fly63.com/design)
- 鹿班（阿里智能设计实验室）：上传白底图或详情页链接生成宣传图，可在线改文字/颜色/图片（导航站描述，可信度有限） — [ai-bot 鹿班](https://ai-bot.cn/sites/1986.html)

**京东 羚珑 / 京点点 Oxygen Vision**
- 羚珑：京东集团统一营销素材设计工具，年均输出超 50 亿设计结果（厂商自述），向全体商家开放；海报/DM/易拉宝模板，一键抠图，根据商品图与文案配置位置/比例/颜色智能合图 — [ai-bot 羚珑](https://ai-bot.cn/sites/2048.html)；[京东 AIGC 平台](https://9.jd.com/)
- 京点点 Oxygen Vision 跨境套图（2026-04-13）：输入京东 SKU → 自动读取商品名/卖点/规格/外观（新品可传实拍图）→ 选目标平台（Joybuy/亚马逊/TikTok Shop）、销售区域、语言 → 匹配平台图片规范与文化禁忌 → 分钟级生成 10 张套图（白底主图、细节、场景、尺寸对比、卖点说明、参数标注）→ 在线预览对比、高清下载；基于 Oxygen 大模型；自述效率提升 90%+，成本降幅数字前后不一致 — [京东云开发者 2026-04-13](https://developer.jdcloud.com/article/4484)
- 京东称其为"零售行业首个全自动 AI 设计智能体"，覆盖约 90% 零售设计场景；2026 年 618 推积分翻倍 — [北京科技报](http://www.bkweek.com/Index/detail?id=897787957590466560)；[证券市场周刊](https://static.weeklyonstock.com/26/0520/qdy105917.html)
- 官网 ai.jd.com 抓取时仅显示加载占位 — [ai.jd.com](https://ai.jd.com/)

**千库 / 图怪兽 / 千图设计室**
- 图怪兽：约 200 万商用模板；一句话生成海报/长图/小红书封面/PPT 并在线修改；第三方注明"AI 生成文字偶有错别字，商用前人工校对" — [aigc.cn](https://www.aigc.cn/sites/80626.html)；[hbzgn](https://hbzgn.com/archives/5997.html)
- 千图设计室 AI 海报：千图网推出的一句话生成海报工具 — [AIWW](https://www.aiww.com/aitool/qiantuaihaibao)

**其他国内新形态**
- 01Agent（超空间科技）："智能画布"保留文字/图片/样式层，双击改字、拖拽图层、局部重生成、风格替换、技能商店封装提示词、多平台尺寸适配 — [搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- EasyClaw："视觉设计创作"技能 + 对话式多轮修改（Windows），在上述推广系列中被排第一，不可作为客观依据 — [AtomGit 2026-05-14](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)

### Inferences
- 国内电商场景的产品形态已收敛为"商品图/SKU 进 → 套图出"的 Agent，且普遍内置平台尺寸规范（淘宝/京东/拼多多/小红书/亚马逊）；对新产品而言，"按渠道输出一组尺寸"是基础能力而非亮点。
- 国内模板型产品的中文准确度优势来自"文字是文字层"，而非模型更强；模型型产品（即梦/通义）在"全场5折"这类促销字上仍会错，验证了"促销关键信息不要交给像素渲染"。
- 堆友/美图/稿定三家都在 2026 年上线 Agent 模式，并都提供"编辑元素/拆层"，说明"Agent 一键出图 + 画布拆层微调"已是国内标配交互。

### Gaps
- 稿定 gaoding.art 的 Agent 模式具体交互（是否追问、一次几版）无独立评测。
- 即梦网页端是否已集成 Seedream 5.0 Pro 的图层分离并能导出分层文件，官方页未说明。
- 美图设计室 x-design.com 抓取失败；品牌资产库机制、导出 PSD 与否未确认。
- 京点点、羚珑的文字渲染方式（图像模型 vs 版式引擎）文章未说明；定价仅知"积分"。
- 堆友是否导出 PSD 未见任何来源。
- 创客贴 AI 改图圈选替换的上线时间仅一个第三方来源。

---

## 关键问题 3：哪些工具产出"文字为文字"的分层设计，版式如何实现

### Takeaway
三种实现路径并存：(a) **模板 + AI 填充**（Canva Magic Design、Adobe Express、稿定、创客贴、美图、可画、图怪兽）——版式来自人工模板，AI 选模板+写文案+抠图填入，天然分层；(b) **整图生成 + 拆层**（Canva Magic Layers、Ideogram Layerize、Lovart Edit Elements、Seedream 5.0 Pro、堆友编辑元素）——先像素后分层，文字恢复有损；(c) **结构化规划 + 分离渲染**（Canva Craft 规划/渲染两段、CreatiPoster JSON 协议 + 背景模型、PosterLLaVA JSON→SVG）——文字/版式由 LLM/协议模型以 JSON 给出，背景由图像模型生成，前端合成。未发现主流商业产品以 HTML/CSS 作为海报画布。

### Cited Findings
- Canva Design Model "从一开始就生成可编辑的设计"；Magic Layers 把外部位图转多层 — [Canva Newsroom](https://www.canva.com/newsroom/news/magic-layers/)
- Canva Craft：规划模型写构图计划→渲染模型出图；"JSON boundary boxing 把图像转为可编辑图层" — [Canva Craft](https://www.canva.com/newsroom/news/canva-craft/)
- Ideogram Layerize：只有文字变成层，其余保持位图；API 返回结构化文字数据+去字底图 — [Ideogram Text Layers](https://ideogram.ai/features/text-layers/)
- Ideogram 4.0 计划从推理直接返回 alpha 通道与可编辑文本层 — [Ideogram 4.0](https://ideogram.ai/models/4.0/)
- Seedream 5.0 Pro 文字描述即拆 10+ 层并补全遮挡背景 — [Seed Blog](https://seed.bytedance.com/en/blog/beyond-generation-it-understands-design-introducing-seedream-5-0-pro)
- Lovart Edit Elements 拆文字/主体/背景层，可导出 PSD；字效可能丢失 — [agiyes](https://www.agiyes.com/ainews/lovart-edit-elements/)
- Recraft 以原生 SVG 输出矢量，但不是分层设计稿；V4.1 无法单独改一个对象 — [Replicate](https://replicate.com/blog/recraft-v4)；[therundown](https://www.therundown.ai/tools/recraft-v4)
- Kittl 实时文字引擎保证字效后仍可逐字编辑 — [Style Factory](https://www.stylefactoryproductions.com/blog/kittl-review)
- 稿定可导出分层 PSD（付费）；可画 Magic Design 标题/正文/图/背景为独立层；美图价签/折扣标签为文字层+矢量装饰 — [搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- Figma Buzz 用表格列映射到可编辑图层做批量 — [DevDash Labs](https://devdashlabs.com/insights/figma-buzz-a-game-changer-for-smb-marketing-teams-(hands-on-review))
- CreatiPoster（arXiv 2506.10890，v1 2025-06-12，v2 2026-07-22）：RGBA 大多模态"协议模型"输出每一层（文字/素材）的 JSON（内容、字体、字号、x/y/w/h、RGBA 颜色、描边、旋转、弯曲、粗斜体、下划线、对齐、行距、字距；层序即 z-order），附简短背景提示；再由条件背景模型生成背景；支持画布编辑、文字叠加、响应式改尺寸（re-layout）、多语言适配、动态海报；发布 10 万张无版权多层设计语料；自称超过 Canva Magic Design — [arXiv 2506.10890](https://arxiv.org/abs/2506.10890)；[themoonlight 综述](https://www.themoonlight.io/en/review/creatiposter-towards-editable-and-controllable-multi-layer-graphic-design-generation)；[GitHub creatiposter](https://github.com/graphic-design-ai/creatiposter)
- PosterLLaVA（arXiv 2406.02884）：LLM 以 JSON 编码版面，PosterGen 系统据此生成可编辑 SVG 海报 — [arXiv 2406.02884](https://arxiv.org/html/2406.02884v1)
- PosterCraft（港科大广州+美团）：端到端像素级生成文字与排版，图层不可拆 — [CSDN PosterCraft](https://blog.csdn.net/AIGCer/article/details/149160356)
- MOSS-Image（5B 开源）：用结构化字段描述场景/元素/文字/位置，坐标为画布中心为原点的 yxyx，但输出仍是像素 — [aiposthub](https://www.aiposthub.com/moss-image-5b-layout-text-comfyui/)
- Paper2Poster/PosterAgent：多智能体，Planner 用二叉树切分页面分配矩形坐标，视觉反馈迭代 — [知乎 Paper2Poster](https://zhuanlan.zhihu.com/p/1914378091763046218)
- 迅排设计（palxiao/poster-design，MIT）：仿稿定的开源海报编辑器，存两份 JSON（整页 Schema + 扁平元素数组），Vue3 前端、Puppeteer 服务端渲染出图，支持 PSD 导入解析为模板，AI 抠图用 rembg/u2net — [GitHub poster-design](https://github.com/palxiao/poster-design)；[作者博客](https://www.palxp.cn/2023/08/06/2023/05-xunpai-design/)
- LandPPT（开源 PPT）：大纲规划→并行生成 HTML 页→对话打磨；导出可编辑 PPTX 需商业 Key — [GitHub LandPPT](https://github.com/sligter/LandPPT)

### Inferences
- 业界最新的两个方向（Canva Craft、CreatiPoster）都把"文字/版式决策"与"像素渲染"拆开，前者在模型内部拆，后者以显式 JSON 拆；显式 JSON 对需要校验价格/日期、品牌约束、多尺寸重排的营销产品更可控。
- "拆层"产品的普遍缺陷（漏字、字效丢失、背景重绘）表明：若产品自己生成，应从一开始保留层，而不是生成后再拆；拆层只应用于用户上传的外部图。
- 迅排设计的"Schema + 扁平元素数组 + Puppeteer 渲染"是国内开源里最接近"LLM 产 JSON → 前端画布渲染 → 服务端出 PNG"的现成底座。

### Gaps
- CreatiPoster 与 Canva Craft 均未给出可复现的定量对比；Craft 无论文。
- 未找到任何商业产品公开说明其使用 HTML/CSS 或 Fabric.js 作为海报合成层的证据。

---

## 关键问题 4：文字准确性（价格/日期/品名）与图像模型级文字渲染能力

### Takeaway
所有模板型产品（Canva/Adobe/Figma Buzz/稿定/创客贴/美图/可画/AdCreative.ai）都是 **LLM 写文案 → 放进文字层**，关键信息可逐字核对；图像模型渲染文字的准确率在 2025–2026 年大幅提升（GPT Image 2、Nano Banana Pro、Seedream 4.5/5.0、Qwen-Image、混元 3.0、Ideogram 4.x、Canva Craft 均主打多语言文字），但独立评测仍一致报告：**小字、长段落、非拉丁文字、促销数字**仍会出错，厂商自身也承认"细粒度文字仍需改进"。通用做法是把要渲染的文字放在引号里并指定字体/位置。

### Cited Findings
- Microsoft Designer 评测建议：先生成不含关键文案的视觉，再把最终文案作为可编辑画布文字加上 — [toolsforhumans](https://www.toolsforhumans.ai/ai-tools/microsoft-designer)
- Ideogram Layerize 提示"检查小字、日期和页脚"，漏字会留在图里 — [Ideogram](https://ideogram.ai/features/text-layers/)
- 图怪兽第三方页："AI 生成文字偶有错别字，建议人工校准后再商用" — [hbzgn](https://hbzgn.com/archives/5997.html)
- 即梦"全场5折"字形错误、标题偶发乱码 — [AtomGit 2026-05-14（推广文）](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)

**Seedream 4.0 / 4.5 / 5.0**
- 火山方舟提示词指南（2026-05-11 更新，覆盖 5.0 lite/4.5/4.0）：建议将要生成的文字放在双引号中，有"海报标题"写法示例；可用文字增删替换修改画面元素，也可用箭头/线框/涂鸦等视觉信号划定编辑区域，并建议写明哪些部分保持不变；doubao-seedream-4-5-251128 最多 14 张参考图，参考图数+生成数 ≤ 15 — [火山方舟 Seedream 提示词指南](https://docs.volcengine.com/docs/ark/seedream-4-0-5-0-prompt-guide?lang=zh)；[方舟模型广场 Seedream 4.5](https://ark.volcengine.com/region:cn-beijing/model/detail?Id=doubao-seedream-4-5)
- Seed 官网称 4.5 小字渲染清晰，适合海报与品牌视觉 — [Seedream 4.5](https://seed.bytedance.com/en/seedream4_5)
- 4.0 小字常模糊/变形，4.5 改善 — [felloai](https://felloai.com/new-chinese-model-seedream-4-5-is-challenging-nano-banana-pro-and-gpt-in-ai-image-quality/)
- 一篇独立对比称 Seedream 4.0 多次测试中中文"完全失败"，三语咖啡袋标签非拉丁文字差，Qwen-Image 中文强得多 — [nanoimagine](https://nanoimagine.art/blog/seedream-4-vs-qwen-image)；与 getimg.ai 称"中英短标题可读"相冲突 — [getimg.ai](https://getimg.ai/blog/guide-to-bytedance-seedream-4-ai-image-model)
- 5.0 Pro 自述支持 10+ 语言且"更细粒度文字仍有改进空间" — [Seed Blog](https://seed.bytedance.com/en/blog/beyond-generation-it-understands-design-introducing-seedream-5-0-pro)

**GPT Image 1.5 / GPT Image 2（ChatGPT Images 2.0）**
- gpt-image-1.5 提示指南：文字渲染"字形清晰、版式一致"，建议把准确文案放引号并要求逐字渲染；产品类建议"透明背景抠出产品并精确保留几何与标签可读性" — [OpenAI Cookbook](https://cookbook.openai.com/examples/multimodal/image-gen-1.5-prompting_guide)
- 独立测试：gpt-image-1.5 遮罩编辑会重绘整图，遮罩只是"引导"，紧遮罩外仍有 7–9% 像素可见变化；OpenAI 论坛开发者亦报告同样现象，建议用提示描述而非遮罩；更新的 gpt-image-2.5 遮罩外改动少得多 — [understandingdata](https://understandingdata.com/ai-engineering-visualised/image-editing-masks/)；[OpenAI Community](https://community.openai.com/t/gpt-image-1-5-rolling-out-in-the-api-and-chatgpt/1369443?page=2)
- Microsoft Foundry 称 1.5 "更一致地保留品牌 logo 与关键视觉"（厂商说法） — [Microsoft Foundry Blog](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-openai%E2%80%99s-gpt-image-1-5-in-microsoft-foundry/4478139)
- gpt-image-2（2026-04-21 发布，API 同日可用）：改进多语言文字渲染，更强的图表/信息图/海报/漫画；"thinking"模式先读提示、规划版式再渲染；2K 输出；宽高比 3:1–1:3；日/韩/中/印地/孟加拉文为头部语种；1.5 保留透明 PNG、fidelity 开关与最低单价 — [OpenAI Community: Introducing gpt-image-2](https://community.openai.com/t/introducing-gpt-image-2-available-today-in-the-api-and-codex/1379479)；[neurohive](https://neurohive.io/en/news/chatgpt-images-2-0-openai-launches-image-generation-model-with-reasoning-2k-resolution-and-multilingual-text/)；[fal 对比](https://fal.ai/learn/tools/gpt-image-2-vs-gpt-image-1-5)

**Nano Banana Pro（Gemini 3 Pro Image）**
- 2025-11-20 发布：短标语到长段落均可读；多语言；可把图中文字本地化/翻译（示例把易拉罐英文翻成韩文而其余不变）；局部编辑、改机位/景深/光线、改宽高比；最多 14 张输入图、保持最多 5 人一致；2K/4K；品牌一致性"by design"；全部输出含 SynthID，免费/Pro 有可见水印 — [Google Blog](https://blog.google/innovation-and-ai/products/nano-banana-pro/)；[DeepMind 模型页](https://deepmind.google/models/gemini-image/pro/)
- Google 提示技巧：先和模型对话生成文案，再要求把该文案放进图；自认"文字渲染、事实准确性、复杂编辑仍需改进" — [Google Prompting Tips](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/)
- 第三方提及更新的 Nano Banana 2（Gemini 3.1 Flash Image）（未经 Google 页面核实） — [nano-banana.ai](https://www.nano-banana.ai/)

**FLUX Kontext / FLUX.2**
- FLUX.1 Kontext 论文：支持 logo 微调、拼写纠正、风格适配等文字编辑并保留上下文 — [arXiv 2506.15742](https://arxiv.org/pdf/2506.15742)
- FLUX.2 改进文字编辑但"生成字母仍需人工检查"；建议原文与替换文都放引号；用亮色框引导需移动/缩放的文字编辑；产品图编辑先列出必须固定的瓶形/盖/标签/logo — [magichour](https://magichour.ai/blog/how-to-edit-photos-with-flux-kontext)；[zeely](https://zeely.ai/blog/flux-kontext-pro-for-product-image/)
- 视频测试：FLUX.1 Kontext 难以保持产品上小字一致 — [Creative Pad Media](https://www.creativepadmedia.com/can-flux-ai-kontext-handle-product-placement-when-it-comes-to-small-text/)

**Qwen-Image / Qwen-Image-Edit** — 见问题 2；要点：开源中文渲染最强口碑，小字仍不稳；Edit-2511 可保字体改文字 — [Replicate](https://replicate.com/qwen/qwen-image-edit-2511)

**混元图像 3.0** — 千字级语义、长文本与小字；评测称中文多样文字一致性优于 Seedream 4.5 — [腾讯云](https://cloud.tencent.com/developer/article/2572759)；[WaveSpeed](https://wavespeed.ai/blog/posts/hunyuan-vs-seedream-comparison-2026/)

**Recraft / Ideogram** — Recraft 适合短/中短语；Ideogram 4.0 做新文字与 bbox 版式，4.5 做就地改字/翻译 — [Recraft Docs](https://www.recraft.ai/docs/recraft-models/recraft-V4)；[Morphic Ideogram 4.5](https://morphic.com/resources/models/ideogram-4-5)

**Canva Craft** — 55 种语言文字，靠规划阶段决定排印 — [Canva Craft](https://www.canva.com/newsroom/news/canva-craft/)

### Inferences
- 对营销海报而言，"价格/日期/活动规则"必须走文字层（LLM 产出 → 结构化校验 → 文字层渲染），图像模型只渲染氛围性大字或装饰字；这与 Microsoft Designer 评测建议、Google 提示技巧（先出文案再出图）及国内模板型产品的实践一致。
- 若必须在像素中渲染中文（如艺术字标题），当前最优候选是 Seedream 5.0 Pro / 混元 3.0 / Qwen-Image / GPT Image 2，并用引号+字体+位置的提示规范；仍需 OCR 回读校验。
- 遮罩编辑的"泄漏"（gpt-image-1.5 遮罩外 7–9% 变化）意味着局部重绘后应做像素级 diff 或只替换遮罩内像素再合成。

### Gaps
- 没有找到任何针对"促销数字/价格"错误率的系统基准；各模型文字准确率均为厂商自述或小样本博客测试。
- OpenAI 官方 ChatGPT Images 2.0 页面抓取 403，细节依赖社区帖与转述。
- Qwen 官方 Qwen-Image-Edit 博客抓取为空页。

---

## 关键问题 5：商品图处理（抠图、摆放、外观保真）

### Takeaway
国内电商产品把"上传商品图 → 抠图 → 场景/套图"做成了主流程（美图设计室、稿定、堆友、京点点、鹿班/羚珑），文字层与商品层分离；图像模型侧（GPT Image、Nano Banana Pro、Seedream、FLUX）靠"参考图 + 提示里显式列出必须保持不变的部件"保真，但小字标签与 logo 仍是公认弱点。

### Cited Findings
- 美图设计室：一张商品图生成主图/A+/全套 marketplace 图；Background Remover/Changer/Generator；Virtual Try-On；AI Fashion Model；抠图精度被多篇评测认可，大图清晰度一般 — [designkit.com](https://www.designkit.com/)；[知乎 2026 电商评测（摘要）](https://zhuanlan.zhihu.com/p/2086030235938584484)；[搜狐 2026-05-28](https://www.sohu.com/a/1028558781_122725336)
- 稿定："发丝级"AI 抠图（发丝/人像/文字自动）；电商 Agent 上传产品图生成全套主图；"4K 商拍精修" — [稿定 AI 海报](https://www.gaoding.com/features/ai-poster-design)；[gaoding.art](https://www.gaoding.art/)；[稿定抠图](https://www.gaoding.com/koutu)
- 堆友：上传实拍图 + 卖点 → 亚马逊 6 图套；文章未评估生成图与实物一致性 — [umaax](https://umaax.com/duiyouagentshiceaiyijuhuashengchengshangyejidianshangshejiha/)
- 京点点：SKU 读取商品信息，新品上传实拍图；Oxygen 大模型"支撑商品细节还原"（厂商自述） — [京东云开发者](https://developer.jdcloud.com/article/4484)
- 鹿班：白底图或详情页链接生成宣传图，"产品融入度高、文字清晰"（导航站说法） — [ai-bot 鹿班](https://ai-bot.cn/sites/1986.html)
- 羚珑：一键抠图 + 按商品图/文案配置位置比例颜色智能合图 — [ai-bot 羚珑](https://ai-bot.cn/sites/2048.html)
- 迅排设计开源抠图用 rembg/u2net — [GitHub poster-design](https://github.com/palxiao/poster-design)
- Adobe Express Generate Template 可上传图片使模板包含它 — [Adobe Flyer](https://www.adobe.com/express/create/ai/flyer)
- AdCreative.ai 产品摄影/视频为独立工具，广告生成页未说明产品图如何进入版式 — [AdCreative.ai](https://www.adcreative.ai/ad-creatives)
- gpt-image-1.5：建议透明背景抠出并"精确保留产品几何与标签可读性"；Foundry 称更一致保留 logo — [OpenAI Cookbook](https://cookbook.openai.com/examples/multimodal/image-gen-1.5-prompting_guide)；[Microsoft Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-openai%E2%80%99s-gpt-image-1-5-in-microsoft-foundry/4478139)
- Nano Banana Pro：最多 14 张输入图，"品牌一致性 by design"，可翻译产品上的文字而其余不变 — [Google Blog](https://blog.google/innovation-and-ai/products/nano-banana-pro/)
- Seedream 4.5/5.0：最多 14 张参考图；材质/颜色参考可应用到目标对象；指南建议写明保持不变的部分 — [火山方舟指南](https://docs.volcengine.com/docs/ark/seedream-4-0-5-0-prompt-guide?lang=zh)；[Seed Blog](https://seed.bytedance.com/en/blog/beyond-generation-it-understands-design-introducing-seedream-5-0-pro)
- FLUX Kontext：小字标签保真差；建议列出固定部件 — [Creative Pad Media](https://www.creativepadmedia.com/can-flux-ai-kontext-handle-product-placement-when-it-comes-to-small-text/)；[zeely](https://zeely.ai/blog/flux-kontext-pro-for-product-image/)
- Lovart Edit Elements 测试中咖啡杯阴影消失 — [agiyes](https://www.agiyes.com/ainews/lovart-edit-elements/)

### Inferences
- 最稳妥的商品保真路径是"抠图 → 作为独立图层合成 → 只让图像模型生成背景/场景"，这也是国内模板型产品的做法；"把商品喂给图像模型重绘"只适合氛围图，不适合主图/包装特写。
- 需要"商品出现在生成场景中"时，应用参考图 + 显式"保持不变"清单，并在输出后对商品区域做相似度/OCR 校验。

### Gaps
- 未找到任何第三方对国内 Agent 产品"商品还原度"的定量评测。
- Nano Banana Pro / GPT Image 2 的产品保真只有厂商说法，无独立测量。

---

## 关键问题 6：定价模式与国内电商 / 活动促销 常用工具

### Takeaway
全球工具以订阅 + 积分为主（Canva Pro、Adobe Express Premium 250 积分、Lovart 积分、Recraft 按量、AdCreative.ai 按下载扣积分）；国内模板型产品以会员（去水印、高清下载、分层导出、商用字体）为核心付费点，模型型产品按每日免费额度 + 会员；电商 Agent（京点点、堆友、美图、稿定）以积分/会员为主。国内 活动促销（618/双 11/节日）常用：稿定设计、美图设计室、创客贴、可画、图怪兽（模板）+ 即梦/通义（背景生成）+ 堆友/京点点/鹿班（套图）。

### Cited Findings
- Canva Pro 价格各源冲突（$13/月、$15/月、$120/年） — [buildfastwithai](https://www.buildfastwithai.com/ai-tools/canva-magic-design)；[allable](https://www.allable.ai/blog/canva-ai-review/)
- Adobe Express Premium 250 生成积分/人/月（第三方） — [work-management.org](https://work-management.org/marketing/creators/adobe-express-review/)
- Microsoft Designer 免费 15 积分/月或 M365 60/月（冲突） — [therundown](https://www.therundown.ai/tools/microsoft-designer)
- Lovart $19–$169 多档，积分不滚存，1000 积分 $10 加购 — [App Store](https://apps.apple.com/dz/app/lovart/id6746544072)；[myclaw](https://myclaw.ai/blog/lovart-ai-review)
- Recraft 按量积分在量产时昂贵 — [toolsforhumans](https://www.toolsforhumans.ai/ai-tools/recraft-ai)
- AdCreative.ai $39/$249/$999，仅下载扣积分；Pencil $14/$55/定制 — [AdCreative.ai](https://www.adcreative.ai/ad-creatives)；[tooljunction](https://www.tooljunction.io/ai-tools/pencil-ai)
- Kittl 约 $10–15/月，SVG/PDF 导出需付费 — [whataidoineed](https://whataidoineed.com/tool/kittl%20ai)
- 稿定：免费版水印，高清下载付费，部分模板单售，分层 PSD/特殊字体需会员 — [agent.csdn](https://agent.csdn.net/6a17f9d510ee7a33f276180c.html)；[搜狐](https://www.sohu.com/a/1028558781_122725336)
- 美图设计室：免费水印，去水印约 28 元/月 — [AtomGit](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)
- 即梦：免费额度有限，新用户试用额度；通义万象：每日免费额度 — [AtomGit](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)
- 京点点 618 积分翻倍，每日可生成上百张图或 200 秒视频 — [证券市场周刊](https://static.weeklyonstock.com/26/0520/qdy105917.html)
- 堆友：免费额度 + 高级功能付费（第三方）；素材与生成内容免费商用（第三方） — [ai-bot 堆友](https://ai-bot.cn/sites/61378.html)；[fly63](https://ai.fly63.com/design)
- 国内横评推荐：国内节日/电商海报用稿定；即梦出背景 + Canva 排版 — [CSDN 2026-05-19](https://blog.csdn.net/2401_89926894/article/details/161106579)；[知乎 一文了解主流 AI 海报工具](https://zhuanlan.zhihu.com/p/1934218164335519702)
- 图怪兽 App 名含"电商喜报"，面向电商 — [App Store 图怪兽](https://apps.apple.com/tw/app/%E5%9C%96%E6%80%AA%E7%8D%B8%E8%A8%AD%E8%A8%88-ai%E6%B5%B7%E5%A0%B1%E5%9C%96%E7%89%87%E9%9B%BB%E5%95%86%E5%96%9C%E5%A0%B1-%E6%91%B3%E5%9C%96%E6%8B%BC%E5%9C%96%E4%BD%9C%E5%9C%96%E7%A5%9E%E5%99%A8/id1492397619)

### Inferences
- 国内付费点集中在"商用授权 + 去水印 + 分层导出"，说明用户对"可商用、可改"的付费意愿高于对"AI 生成次数"的付费意愿；新产品定价可对齐此结构。
- 国内电商用户实际工作流是"多工具拼接"（模型出背景、模板工具排版、Agent 出套图），统一这一链路是机会点。

### Gaps
- 稿定、创客贴、可画中国版的官方会员价格未抓到。
- 无国内各工具的市场份额/使用率数据，"常用"判断基于评测文章的推荐语。

---

## 关键问题 7：评测中反复出现的弱点

### Takeaway
弱点高度一致：(1) 中文/非拉丁文字乱码与小字错误（即梦、Seedream 4.0、Ideogram、Firefly、Midjourney）；(2) 模板型"千篇一律/只是选模板"（Canva、稿定、可画）；(3) 小改动困难——整图重绘、遮罩外泄漏、拆层漏字丢字效（gpt-image-1.5、Recraft V4.1、Lovart、Ideogram Layerize）；(4) 商品/细节失真（FLUX 小字、Lovart 阴影消失、美图大图清晰度）；(5) 积分消耗不透明（Lovart、Pencil）；(6) 合规——国内 AI 生成商用图需显著标识，价格/折扣虚假有广告法风险。

### Cited Findings
- 中文乱码/字形错误：即梦"全场5折"错；Firefly 中文理解弱；Midjourney 中文差 — [AtomGit](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)；[CSDN](https://blog.csdn.net/2401_89926894/article/details/161106579)
- Ideogram 非拉丁文字局限；Recraft 段落级落后 — [mindstudio](https://www.mindstudio.ai/blog/what-is-ideogram-v3)；[versely](https://www.versely.studio/blog/recraft-v3-vs-ideogram-v3-brand-graphics-showdown-2026)
- 千篇一律：Canva 无定制则"像所有 Canva 帖子"；Brand Kit 不进入生成 — [Marqeable](https://www.marqeable.com/blog/canva-ai-image-generator-review-marketing/)
- 稿定/可画依赖模板，原创弱 — [AtomGit](https://gitcode.csdn.net/6a05874a54b52172bc742ec4.html)；[知乎 摄图对比](https://zhuanlan.zhihu.com/p/2081340366612186309)
- 小改动困难：gpt-image-1.5 遮罩外 7–9% 变化 — [understandingdata](https://understandingdata.com/ai-engineering-visualised/image-editing-masks/)；Recraft V4.1 无法只改一个对象 — [therundown](https://www.therundown.ai/tools/recraft-v4)；Lovart 字效丢失/图标未分离 — [agiyes](https://www.agiyes.com/ainews/lovart-edit-elements/)；Ideogram Layerize 漏字、背景重绘 — [Ideogram](https://ideogram.ai/features/text-layers/)
- Microsoft Designer 复杂图层/精确排版受限、输出"明显 AI 味" — [toolsforhumans](https://www.toolsforhumans.ai/ai-tools/microsoft-designer)；[aitoolsofthetrade](https://aitoolsofthetrade.com/reviews/microsoft-designer)
- Lovart 积分消耗远超文档；Pencil 按交互计次、Trustpilot 计费投诉 — [framia](https://framia.converge.ai/blog/lovart-ai-review/)；[soku](https://soku.ai/alternatives/pencil-ai/review)
- 合规：商家未显著提示 AI 生成主图存在合规风险，若构成广告且违反广告法可罚 20–100 万元（2026-01-16） — [央广网](https://www.cnr.cn/mspd/zhsh/20260116/t20260116_527494911.shtml)；外卖商家 AI 门头图被律师认定可能构成虚假广告（广告法第 4/28 条，可罚广告费 3–5 倍、最高 200 万） — [扬子晚报 2025-08-01](https://www.yzwb.net/news/zntt/202508/t20250801_244572.html)；江苏市监 618 提示"AI 造假、虚假促销、'全年最低价'绝对化用语、先涨后降、虚标原价" — [新华报业](https://www.xhby.net/content/s6a3395d9e4b0baeb0111ce5f.html)；素材价与成交价不一致是最常踩的价格欺诈情形 — [ByeRisk](https://www.byerisk.com/blog/ad-price-fraud-seven-scenarios)
- 堆友竖排文字手机端偏小；仍需人工把关 — [umaax](https://umaax.com/duiyouagentshiceaiyijuhuashengchengshangyejidianshangshejiha/)

### Inferences
- "AI 幻觉折扣"的直接案例未检索到，但监管对价格表述和 AI 标识的执法在 2025–2026 明显收紧，产品应把"价格/折扣/绝对化用语"作为结构化字段并做合规校验（禁用"最低价""史上"等），且提供 AI 生成标识选项。
- 用户抱怨集中在"改不了小东西"，因此局部编辑应以图层/文字层操作为主，像素级 inpaint 为辅。

### Gaps
- 没有可引用的"AI 海报生成了错误价格导致处罚"的具体案例。
- 国内评测几乎没有可复现的测试协议，弱点结论多为单次体验。

---

## 关键问题 8："AI 海报"产品的最佳实践架构证据

### Takeaway
可查证的架构信号指向同一模式：**规划层（LLM/协议模型）输出文案 + 版式 JSON（含每层内容、位置、字体、颜色、z-order）→ 图像模型只生成背景/主体素材（含 RGBA/透明层）→ 画布引擎（可编辑文字层 + 图片层）合成 → 导出 PNG/PSD/SVG，并支持按尺寸 re-layout**。Canva Craft（规划→渲染）、CreatiPoster（JSON 协议→背景模型）、PosterLLaVA（JSON→SVG）、Ideogram Layerize API（结构化文字 + 去字底图）、迅排设计（Schema JSON + Puppeteer 出图）都是该模式的不同切片。

### Cited Findings
- Canva Craft：规划模型产出构图计划（版式、文字、层级）→ 渲染模型；训练于人工模板；JSON boundary boxing 转可编辑层；Public API 可用 — [Canva Craft](https://www.canva.com/newsroom/news/canva-craft/)
- CreatiPoster：协议模型 JSON（每层文字/素材的内容、字体、x/y/w/h、RGBA、描边、旋转、弯曲、对齐、行距、字距、z-order）+ 背景提示 → 条件背景模型；支持 re-layout 多尺寸、多语言适配；10 万多层设计语料开源 — [arXiv 2506.10890](https://arxiv.org/abs/2506.10890)；[GitHub](https://github.com/graphic-design-ai/creatiposter)
- PosterLLaVA JSON 版面 → PosterGen 可编辑 SVG — [arXiv 2406.02884](https://arxiv.org/html/2406.02884v1)
- Ideogram Layerize API：返回结构化文字数据、去字底图、渲染结果三件套 — [Ideogram](https://ideogram.ai/features/text-layers/)
- Seedream 5.0 Pro：文字指令拆层、透明图层、遮挡补全；草图/坐标指定文字区域 — [Seed Blog](https://seed.bytedance.com/en/blog/beyond-generation-it-understands-design-introducing-seedream-5-0-pro)
- 迅排设计：整页 Schema + 扁平元素数组两份 JSON；Vue3 画布；Puppeteer 服务端渲染出图；PSD 导入为模板 — [GitHub poster-design](https://github.com/palxiao/poster-design)
- Figma Buzz：模板锁定可编辑区 + 表格字段映射图层 + 整组 Resize 到渠道尺寸 — [Figma Blog](https://www.figma.com/blog/new-features-in-figma-buzz/)
- 国内 Agent 产品（堆友/美图/稿定）的"Agent 一键出图 + 编辑元素拆层 + 多尺寸延展" — [umaax](https://umaax.com/duiyouagentshiceaiyijuhuashengchengshangyejidianshangshejiha/)；[App Store 美图设计室](https://apps.apple.com/us/app/%E7%BE%8E%E5%9B%BE%E8%AE%BE%E8%AE%A1%E5%AE%A4-%E8%AE%A9%E5%95%86%E4%B8%9A%E8%AE%BE%E8%AE%A1-%E5%A5%BD%E7%9C%8B%E5%8F%88%E8%A7%81%E6%95%88/id875654777?l=zh-Hans-CN&platform=mac)
- Google 建议"先对话出文案，再出图" — [Google Prompting Tips](https://blog.google/products-and-platforms/products/gemini/prompting-tips-nano-banana-pro/)
- Paper2Poster 多智能体 Planner 二叉树切分 + 视觉反馈迭代 — [知乎](https://zhuanlan.zhihu.com/p/1914378091763046218)
- LandPPT：大纲 → 并行生成 HTML 页 → 对话打磨 — [GitHub LandPPT](https://github.com/sligter/LandPPT)

### Inferences（面向 LangGraph 产品设计）
- 推荐节点划分：① 需求澄清/结构化（品牌、渠道尺寸集、商品、关键信息字段：价格/日期/规则）→ ② 文案生成（LLM，输出结构化字段而非自由文本）→ ③ 版式 JSON 生成（LLM/协议模型，CreatiPoster 字段集可直接借鉴）→ ④ 素材生成（图像模型只做背景/主体，用透明通道；商品图走抠图层）→ ⑤ 画布合成（前端可编辑层；服务端 Puppeteer/Skia 出 PNG）→ ⑥ 校验（OCR 回读像素文字、价格字段比对、合规词检查、AI 标识）→ ⑦ 多尺寸 re-layout → ⑧ 导出 PNG/PSD/SVG/Figma。
- 局部编辑优先级：改文字层 > 换/移图层 > 对背景做 inpaint；像素 inpaint 后对遮罩外做 diff 保护。
- 外部位图（用户带来的旧海报）走"拆层"分支（Magic Layers/Layerize/Seedream 5.0 Pro 思路），自生成的海报不应依赖拆层。

### Gaps
- 没有任何商业产品公开其完整内部管线；上述推断基于公开架构线索与论文。
- CreatiPoster 代码/权重在商用许可与中文字体支持上的细节未核实。
- 未检索到 Fabric.js / HTML-CSS 作为海报合成层的商业案例证据（仅 LandPPT 用 HTML 做 PPT）。
