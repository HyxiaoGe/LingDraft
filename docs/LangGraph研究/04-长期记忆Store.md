# 04 长期记忆 Store：存取、语义搜索、过期，和挂到对话上

2026-11-01。读的是装好的源码（langgraph 1.2.18：`langgraph/store/base/__init__.py`、`base/batch.py`、`memory/__init__.py`、`postgres/base.py`、`sqlite/base.py`，`pregel/_algo.py`）。LangMem 0.0.30 没装进项目，读的是 uv 缓存里下载下来的源码（`langmem/knowledge/tools.py`、`extraction.py`、`reflection.py`），没跑。

实验在进程里跑灵稿的运行器：假模型、假网关，图换成"我们的 4 个 middleware + 要试的那个"，加两个记忆工具。向量也是假的：按相邻两个字算哈希，只能看机制，看不出搜得准不准。代码在 `实验/04-store/exp.py`，用法写在文件开头。

## Store 是什么，和检查点有什么不同

- 检查点按对话（thread）存图的状态，每一步一份，第 2 篇讲过。
- Store 是跨对话的键值库：**命名空间（一串字符串，比如 `("memories", "sean")`）+ key → 一个 JSON 对象**。和哪个对话、哪一步都没关系。
- 挂法：`create_agent(store=...)`，它转给 `compile(store=...)`。每个任务开跑前，`_algo.prepare_single_task` 把它放进 `Runtime.store`。
- 取法：
  - 工具里用 `ToolRuntime.store`，或者参数标 `InjectedStore`；
  - middleware 里用 `request.runtime.store`；
  - 节点里用 `get_store()`。
- **Store 是原样传进去的，没有包事务。** 工具里一调 `put` 就写进库了，不跟这一步的检查点一起提交，这一步后面失败了也不会撤回。重新回答、改了重答、删对话、从检查点分叉，都动不了 Store 里的东西（实验 checkpoint）。

## 接口怎么设计的

**只有一个真正要实现的方法**

- `BaseStore` 的抽象方法只有 `batch` 和 `abatch`。
- `get`、`search`、`put`、`delete`、`list_namespaces` 都只是把参数包成一个操作（`GetOp`、`SearchOp`、`PutOp`、`ListNamespacesOp`），再交给 `batch`。
- 删除就是 value 为 None 的 `PutOp`。
- 换一种存储，只要写好 `batch`。

**异步版会攒批**

- `AsyncPostgresStore`、`AsyncSqliteStore` 继承 `AsyncBatchedBaseStore`。
- 异步调用先进一个队列，后台任务把同一轮事件循环里攒下的操作一次交给 `abatch`。
- 一样的读只查一次；同一个 key 的几次 put 只留最后一次（`_dedupe_ops`）。
- 同步版没有这一层，一次调用就是一次 `batch`。

**一条记录**

- `Item` 有 namespace、key、value（dict）、created_at、updated_at；搜出来的 `SearchItem` 多一个 score。

**命名空间的规矩**（`_validate_namespace`）

- 不能空；
- 第一段不能是 `"langgraph"`；
- 每一段不能带点。原因是 Postgres 把命名空间用点拼成一个字符串存（`memories.sean`），按前缀查走 `text_pattern_ops` 索引。

**存**

- `put(namespace, key, value)`：同一个 key 再存是整条替换，不合并字段。
- created_at 的处理不一样：内存版会跟着变（实验 basics）；Postgres 是 `ON CONFLICT DO UPDATE`，只改 value、updated_at 和过期时间，created_at 保留。

**取和搜**

- `get(namespace, key)`：取一条。
- `search(namespace_prefix, query=None, filter=None, limit=10, offset=0)`：
  - 按前缀找，`("memories",)` 会把 `("memories", "sean")`、`("memories", "guest")` 都找出来；
  - filter 对 value 里的字段，只支持 `$eq $ne $gt $gte $lt $lte` 几种，几个条件是"并且"，没有"或者"，也没有 `$in`。
- `list_namespaces(prefix=, suffix=, max_depth=)`：列出有哪些命名空间，可以只列前几层。

## 语义搜索

**怎么开**

- 建 Store 时给 `index={"dims": …, "embed": …, "fields": [...]}`。
- `embed` 可以是 LangChain 的 Embeddings、一个函数，或者 `"openai:text-embedding-3-small"` 这样的字符串。
- 不给 index 就不能按意思搜，put 时的 index 参数也不起作用。

**哪些字段算向量**

- `fields` 默认 `["$"]`，整条一个向量；
- 可以写字段路径；`"tags[*]"` 是数组里每个元素各算一个向量；
- 单条可以覆盖：`put(..., index=False)` 不进索引，`put(..., index=["content", "tags[*]"])` 换字段。
- 实验 semantic：带两个标签的那条存了 3 个向量（content、tags[*].0、tags[*].1）。

**怎么打分**

- 一条有几个向量时取最高的那个（源码注释写的是 max pooling）。
- **没有分数门槛**：总是返回前 limit 条，分数是 0 的也排进来。实验里搜"吃辣的菜"，"对花生过敏"以 0.0 分排在第 4。要门槛得自己按 score 过滤。

**没有向量的那条，两种实现不一样**

- 内存版：有分数的不够 limit 条时，拿它们补在后面，score 是 None。
- Postgres：带 query 的搜索要 `JOIN store_vectors`，没有向量的那条根本搜不到。实验 postgres 里，开向量之前存的 a，带 query 搜不出来，不带 query 才列得出来。
- **先存了再开向量，旧的不会补算向量**，要自己重存一遍。

**不带 query 时怎么排**

- 内存版按存进去的顺序；
- Postgres 按 updated_at 从新到旧。

**代价**

- 每次 put 和每次带 query 的 search，都要调一次 embedding 模型。

**底层**

- Postgres：要 pgvector 扩展，默认 HNSW 索引、余弦距离，也可以选 ivfflat、flat、halfvec。
- SQLite：用 sqlite-vec。

## 过期

- 配置 `ttl={"default_ttl": 分钟数, "refresh_on_read": True, "sweep_interval_minutes": …, "omit_expired": False}`，也可以每次 put 单独给 `ttl=`。
- `refresh_on_read` 默认开着：get、search 读到一条，就给它续期。
- 内存版不支持过期，设了就报 `NotImplementedError`；Postgres 和 SQLite 支持。
- Postgres 在每行存 `ttl_minutes` 和 `expires_at`（实验：设 30 天，存成 43200 分钟）。
- **到期不会自己删**：要调 `start_ttl_sweeper()` 起一个后台线程（默认 5 分钟扫一次），或者自己定时调 `sweep_ttl()`。
- **`omit_expired` 默认关着**：过期了但还没扫掉的，照样读得出来。

## 存 Postgres

- `setup()` 建表：
  - 不带向量：`store`（prefix、key、value jsonb、created_at、updated_at、expires_at、ttl_minutes，主键 prefix + key）和 `store_migrations`；
  - 带向量再加 `store_vectors`（每个字段一行向量，删 store 那条时一起删）和 `vector_migrations`。
- 和检查点的四张表（checkpoints、checkpoint_blobs、checkpoint_writes、checkpoint_migrations）不重名，可以放在同一个库里。

## LangMem：langchain-ai 在 Store 上做的记忆

LangGraph 和 LangChain 1.4.7 都没有现成的记忆工具或 middleware：Store 交给工具和 middleware 以后，怎么用全靠自己写。langchain-ai 另有一个库 LangMem，搭在 Store 上，提供两种写记忆的做法。

**做法一：模型在对话里自己存、自己查（两个工具）**

- `create_manage_memory_tool`：
  - 一个工具管增、改、删三件事，参数是 action、content、id；
  - 新建的 key 是随机 uuid；
  - 改和删要带 id。所以模型得先查出这条，才改得了，不然只会再加一条。
- 它的工具说明是这么写的：

  > Proactively call this tool when you: 1. Identify a new USER preference. 2. Receive an explicit USER request to remember something or otherwise alter your behavior. 3. Are working and want to record important context. 4. Identify that an existing MEMORY is incorrect or outdated.

- `create_search_memory_tool`：参数是 query、limit（默认 10）、offset、filter，直接调 `store.search`。
- 两个工具都用 `get_store()` 拿 Store。
- 命名空间可以写成模板 `("memories", "{langgraph_user_id}")`，运行时从 `config["configurable"]` 里取值。

**做法二：对话之后在后台整理（`create_memory_store_manager`）**

一次整理分三步：

1. 先拿对话去 Store 里搜出相关的已有记忆。
   - 默认最多 5 条（`query_limit=5`）。
   - 不另给一个写查询的模型时，用对话末尾的几段当 query。窗口个数是 `query_limit // 4`，默认是 1，也就是**只拿最后一条消息去搜**。
2. 把对话和这几条已有记忆交给模型，模型决定新增、改哪条、删哪条。
3. 写回 Store。

模型只看得到搜出来的这几条。搜得不准，它就看不到已经有的那条，于是再加一条重复的。

**延后执行（`ReflectionExecutor`）**

- `submit(..., after_seconds=N)`：过 N 秒再整理。
- 同一个对话再提交一次，会取消还没跑的那次。所以对话一直在继续，整理就一直往后推，停下来 N 秒才真正跑。
- 本地版是进程里一个线程加一个优先队列，**进程重启，没跑的就丢了**。
- 远程版是在 Agent Server 上建一个 run：`multitask_strategy="rollback"` 加 `after_seconds`，靠服务端来取消和延后。Agent Server 正式部署要 LangSmith 许可，见第 1 篇。

## 挂到灵稿上会怎样（实验）

**agent**（对话 A 里说"记住：我不吃辣，微辣可以"，对话 B 里请它推荐菜）：

- 对话 A：模型调 remember 存进 Store，回"好的，记住了"。
- 对话 B：模型先调 recall 查到这一条，再照常搜、读、答。
- 页面上看不到记忆工具这两步：`history.STEP_TOOLS` 里只有搜索、读页、天气、生图，别的工具不显示。来源也只算搜索和读页。

**checkpoint**（记忆和检查点各管各的）：

- 重新回答一次：模型又存了一遍，Store 里两条"我不吃辣"。
- 改成"记住：我能吃一点辣"再重答：对话里只剩改过的那一轮，Store 里三条，旧的两条还在。
- 删掉对话 A：检查点里 A 没了，三条记忆都还在。

假模型每次都会存。真模型重答时存不存不一定，但只要存，就是再加一条。

**inject**（同一个对话说三轮，比较每轮第一次调用和上一轮最后一次调用的开头对不对得上；第 3 篇讲过，对不上的部分按没命中缓存的价格算，大约贵 30 倍）：

| 做法 | 第 2 轮 | 第 3 轮 |
|---|---|---|
| 每次调模型前查记忆，接在系统提示词后面；只有 1 条记忆，第 3 轮之前别处新记了 1 条 | 全接上（6/6） | **0 条**，系统提示词变了 |
| 同上，但有 5 条记忆，按这一轮的话查前 3 条，中途没有新增 | 全接上（6/6） | **0 条**：查出来的 3 条换了一条 |
| 不放系统提示词，模型自己调 recall，查到的是一条工具结果；第 3 轮之前同样新记 1 条 | 全接上（8/8） | 全接上（16/16） |

系统提示词在最前面，它一变，后面整段都对不上。

- 记忆放进系统提示词：记忆一变，下一次调用就整段没命中。
- 按这一轮的话去查、再放进系统提示词：记忆一多，几乎每轮都会变。
- 用工具查：查到的记忆是接在后面的一条消息，已经发过的内容不变，前缀一直稳。

**postgres**（临时起一个 Postgres，pgserver 自带 pgvector）：

- 建出的表、每行存成什么样，都和上面写的一样，例如一行是 `('memories.sean', 'a', {'content': '不吃辣'}, None)`。
- 没有向量的那条，带 query 搜不到。

**没验证的**：没跑真实模型。模型会不会在该存的时候存、该查的时候查，存进去的写成什么样，都还不知道。

## 和灵稿现在比

- **现在没有跨对话的东西**：每个对话只有自己的检查点。
- **挂上去的改动小**：`build_agent` 的 `create_agent` 多给一个 `store=`，再加工具，工具从 `runtime.store` 拿。我们的 middleware 是同步的，同步的 `PostgresStore`、`SqliteStore` 都能用。
- **命名空间**：灵稿没有登录，只有一个用户，命名空间写死一个就行，比如 `("memories", "default")`。以后有登录，第二段换成用户 id。
- **存哪里**：
  - dev 上的 Postgres 是 15.18，没有 pgvector（`pg_available_extensions` 里查不到），只能存和按条件取，不能按意思搜。要按意思搜，得换成带 pgvector 的镜像，或者在库里装上这个扩展。
  - 本机的 SQLite 版要 sqlite-vec，项目环境里已经有了（0.1.9）。
  - 不管存哪里，都还得有一个 embedding 模型。灵稿现在只接了 DeepSeek 的对话模型，网关上也没有向量模型。
  - 记忆不多的话，不按意思搜也行：不带 query 全部取出来，交给模型自己挑。
- **页面**：记忆工具现在不显示，来源也不算它。
- **和对话操作的关系**：重答会重复存；删对话不删记忆；从检查点分叉也撤不回。
- **缓存**：
  - 第 3 篇算过，我们的钱主要花在没命中缓存的部分；
  - 记忆放进系统提示词，每变一次就整段没命中；
  - 用工具查不影响前缀。

## 建议（等 Sean 定）

1. **要不要现在加记忆**，是产品上的决定。灵稿现在是"先只做 chat"，记忆不是对话能搜、能读、标来源的必需部分。
2. **要加的话，先做最小的一版**：
   - 同步 `PostgresStore`，和检查点放同一个库，先不开向量（dev 的库也开不了）；
   - 照 LangMem 的两个工具，一个存、改、删，一个查。用 LangMem 本身，还是照着写两个自己的工具，要先确认 LangMem 0.0.30 和我们的 langchain 1.4.7 能不能一起装。我这次没装、没试。
   - 工具说明用英文，照 LangMem 的写法。
3. **记忆怎么进上下文**，按缓存来看，排序是：
   - 工具查最稳；
   - 系统提示词里放全部记忆，但每个对话只在第一轮取一次、存进状态，这个对话后面一直用这一份。前缀在一个对话里不变，新记的要到下一个对话才看得到。这条没做实验，是从上面第 2 轮的结果推的：系统提示词不变，开头就全接得上。
   - 每次按这一轮的话去查、再放进系统提示词，最不划算。
4. **写入**：
   - 模型当场调工具存，最简单；
   - 后台整理（LangMem 的第二种做法）多一次模型调用，还要一个能扛住重启的延后任务。LangMem 本地版的延后任务重启就丢；我们的运行器是自己写的，不是 Agent Server，这一块要自己做。
5. **几个产品上的问题**，做之前要定：
   - 页面上要不要显示"记住了"这一步；
   - 要不要有地方查看、删除记忆；
   - 删对话要不要连带删从这个对话记下的记忆；
   - 重答时重复存的要不要去重。

2026-11-01 Sean 定：先不加记忆。
