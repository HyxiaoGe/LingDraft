"""知识库（2026-11-09 Sean 定，LangChain 研究第 5 篇 RAG 的落地，方案见 docs/知识库方案.md）。

用户把文件传进自己的知识库，以后任何对话都能查，回答标出用的是哪个文件的哪一段。
- 每人一个库，游客没有。知识库的文件就是文件表里的行（workspace_id = "kb:<账号>"，见 app.files.store），
  解析、原件、按段存的文字、预览、read_file 都沿用对话里的文件。
- 上传 → 解析（app.files.service）→ 切块、算向量、写进 kb_chunks（index）→ 可以用。建好之前文件一直是“解析中”。
- 查：向量（百炼 qwen3.7-text-embedding-flash）和关键词（jieba 分词，词之间是“或”）各取前 20 块，RRF 合并取前 20，qwen3.7-text-rerank 重排取前 8
  （11-09 实验：第 1 条命中 36 题里 26 个，只用向量 15 个，见方案第四节）。重排、向量没调成的少一路照样给结果。
- 主 agent 的 search_knowledge 工具（app.tools.knowledge）：知识库里有文件才有，查不查由模型定。
- 额度：建索引按实际 token 扣（costs.KB_INDEX），查一次按 models.toml 的 [tools] 扣；查的时候调向量、重排的账也记（KB_SEARCH，不算给用户）。
"""
