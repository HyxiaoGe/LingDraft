# 01 Agent Server 对照我们的运行器

2026-10-31。把灵稿的对话图原样挂到 `langgraph dev` 上（langgraph-api 0.17.0，内存版运行时 0.37.0），用假模型、假网关和 langgraph-sdk 试了一遍。实验代码和起法见 `实验/01-agent-server/`。

## 先说授权

- `langgraph dev` 是内存版，用来开发和测试，免费。启动日志原话："This in-memory server is designed for development and testing. For production use, please use LangSmith Deployment."
- 生产环境自己部署 Agent Server 要 LangSmith 授权。官方的[独立部署文档](https://docs.langchain.com/langsmith/deploy-standalone-server)写了：
  - 环境变量里有 `LANGGRAPH_CLOUD_LICENSE_KEY`（LangSmith 许可证密钥，服务启动时验证一次）；
  - 网络要能连 `beacon.langchain.com`，做许可证验证和用量上报，离线部署除外。
  - 文档没写价格，也没写哪种套餐能拿到密钥。（11-02 补。之前只找到[社区论坛](https://forum.langchain.com/t/best-practices-for-self-hosting-langgraph-server-oss-without-langgraph-keys/3779)和第三方整理的说法）
- LangGraph 框架本身是 MIT 协议。
- 有开源的替代品 Aegra（FastAPI + Postgres，Apache 协议），没试过。

所以这一步的定位是学它的设计、拿来对照，不打算把它部署到 dev 上。

## 它的模型：thread 和 run

- **thread**：一段对话，状态存在检查点里。和我们一样，一个对话一个 thread_id。
- **run**：一次执行。有自己的 id 和状态（pending、running、success、error、interrupted），存在库里，事后能列出来、能查失败原因。
  - 我们的“一次执行”只在内存登记表里（`RunRegistry`），跑完就没了，留下的只有每轮记录（`Turn`）。
- **assistant**：一个图加一套配置，算一个版本。我们只有一个图，用不上。

## 逐项对照

### 1. 一个对话同时只能跑一次：双发策略

它有四种策略（`multitask_strategy`），用同一个场景各试了一次：第一句话正在慢慢搜，1 秒后又发第二句。

- **reject**：第二句被拒，409 `ConflictError`。
- **enqueue**（默认）：第二句排队，第一轮跑完后自动接着跑。两轮都完整。
- **interrupt**：第一轮停在它最后一个检查点上，第二句马上开始跑。
  - 结果是两句用户的话在消息里挨着，第一轮没有回答。
  - 我们的 `after_agent` 没跑，第一轮的记录没收尾（没有 `ended_at`）。
- **rollback**：第一轮连同它的输入整个撤掉，就像那句话没说过。

我们只有 reject 一种：`RunRegistry.admit` 占不到位就 409。页面允许回答时先打下一句，但要等答完才能发。

（11-02 更新）后来做了两种：

- **排队**：回答时又说的话排在我们自己的库里（`pending_messages`），这一轮答完或停下就发成新的一轮。像 enqueue，但只排一句，又说的接在后面合成一句；这一轮出错就不发，放回输入框。
- **插进这一轮**：排着的话可以改成插进正在跑的这一轮，主 agent 下一次调模型之前放进对话，还是同一轮。Agent Server 的四种里没有这种。
  - 要等手上这一步做完才看得到；派了子 agent 的，要等子 agent 都做完。
  - 模型已经在写最后的回答、不再调模型了，这句话就照常排着，答完发成新的一轮。

### 2. 停止

它的做法：

- 调用 `runs.cancel(action="interrupt" | "rollback")`。
- 取消消息经流管理器送到 worker。worker 把“等下一步”的那个 asyncio 任务取消掉（`langgraph_api/asyncio.py` 的 `wait_if_not_done`），抛出 `UserInterrupt` 或 `UserRollback`（`langgraph_api/worker.py`）。
- 实测从发出取消到状态变成 interrupted，几乎是 0 秒。
- interrupt 停在最后一个检查点；rollback 删掉这次 run 写过的所有检查点。

interrupt 停下以后实际是什么样（实测）：

- 已经流出来、还没进检查点的正文丢了。
- 模型发起了工具调用、工具还没返回的，消息里留下一个悬空的工具调用，没有对应的工具结果。
- 我们的 `after_agent` 没跑，这一轮没收尾。线程状态是 interrupted，`next` 是 `["tools"]`。
- 接着再说一句话，新的一轮照常跑，但那个悬空的工具调用一直留在历史里。
  - DeepSeek 这类接口要求工具调用后面必须跟着结果，所以下一次请求会报错。
  - 假模型不检查这一点，所以实验里没报错。
  - 它的文档也提醒过：interrupt 可能留下悬空的工具调用，要图自己处理。

我们的做法：

- `StopCheck` 在下一个片段、或下一步开始时抛 `Stopped`。不是立刻停：要等当前的工具做完，画图例外，它边等边查。
- 然后 `_write_stop` 用 `stop_turn` 收尾：已经流出来的正文写回去，悬空的工具调用补上结果，这一轮标成 stopped。
- **这段收尾 Agent Server 不替你做。** 真要换过去，这段逻辑还得自己写。

两边有一个共同的限制：同步的工具在线程里跑，谁都杀不掉。它取消的只是“等待”，线程里的搜索还会跑完，结果被丢掉。我们画图停止以后也是这样。

### 3. 断线续看

- 它：创建 run 时开 `stream_resumable=True`，之后用 `join_stream(last_event_id=…)` 接上。
  - 事件 id 是“毫秒-序号”，和 Redis Stream 一个格式。内存版存在进程里，正式版存在 Redis。
  - 实测读 3 个事件就断开，2 秒后带着最后一个 id 接上，后面 6 个一个不少。
- 我们：`LocalEventLog` 在进程里留最近 64 次执行的事件，页面用 `follow(after=序号)` 接上。
  - 思路一样，接口也已经按“以后换 Redis”写成了可替换的（`EventLog` 协议）。

### 4. 重新回答、改了原话重发

- 它没有专门的接口，靠时间回溯：
  - 先用 `get_history` 找到这一轮之前的那个检查点，再 `runs.create(checkpoint=…)` 从那里分叉。
  - 实测：新分支成了当前状态，旧回答还在历史里，检查点从 14 个变成 21 个。
- 我们：`update_state` 写 `RemoveMessage`，删掉这一轮之后的消息，再跑。
  - 旧回答从当前状态里删掉了。
  - 但 SQLite 里的旧检查点其实都还在，因为我们从来不清理检查点，只有删除对话时才删（`delete_thread`）。等于数据留着，没用上。2026-10-31 的 8000 上，17 个对话一共存了 306 个检查点，库文件 16MB。
- 改成分叉的好处：能做 ChatGPT 那种“1/2 切换看旧回答”。代价是要管分支，页面也得知道当前在哪条分支上。放到题目 2（检查点）里细看。

### 5. 排队和并发

- 它：run 在库里排队，worker 从队列里取，同一个线程的 run 一个接一个跑。内存版只有 1 个后台 worker。
- 我们：`Executor` 是一个先进先出的线程池，大小是 `RESEARCH_WORKERS`，默认 4。满了的排队，页面会收到 `queued`。

### 6. context 只能是 JSON

- 它：每次 run 的 `context` 是 JSON，按 `context_schema` 构造出来。
- 我们的 `ChatContext` 里放的是对象：图片存储，加一个停止检查函数。第一次挂上去，构造就失败了：`ChatContext.__init__() missing 2 required positional arguments`。
- 要挂到它上面，这些东西得改成模块级的单例，或者在图的工厂函数里建。停止检查本来就用不上，它有自己的取消。

### 7. 同步还是异步

- 它用 `astream` 跑图。middleware 必须有异步版（`awrap_model_call`、`awrap_tool_call`），否则报 `NotImplementedError: Asynchronous implementation of awrap_model_call is not available`。
- 我们整个是同步写法：线程池加同步 middleware。
- 实验里加了一层通用的桥：同步的 middleware 放到线程里跑，它调的 handler 再转回事件循环。挂上以后标题、每轮记录、搜索 → 读页 → 回答都正常。
- 真要迁过去，middleware 要写异步版，模型和网关最好也换成异步客户端。

## 结论（我的看法，等 Sean 定）

- 我们的运行器基本上是一个小号的 Agent Server：
  - 登记表相当于 reject 策略；
  - 事件日志相当于可续看的流；
  - 停止相当于 interrupt，再加上我们自己的收尾。

  方向没跑偏。接口也是按同一条线拆的：我们的 `RunRegistry` 和 `EventLog` 可替换，它的正式版是 Postgres 存 run、Redis 存流。
- 它多出来、值得学的有三样：
  - run 作为一等对象落库，能查历史运行和失败原因；
  - enqueue 和 rollback 两种双发策略；
  - 从检查点分叉，代替删消息。
- 我们已经做了、而它不替你做的：停止后的收尾，包括保住已经写出来的正文、补上悬空的工具调用、把这一轮标成停止。
- 不建议整个换成 Agent Server，原因有三：生产环境要授权；我们是同步写法，得先全面改成异步；页面的事件格式也要跟着换。值得借的是它的设计。
