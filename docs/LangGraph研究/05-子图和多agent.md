# 05 子图、多 agent 和 Send 并行分发

2026-11-01。读的是装好的源码：

- langgraph 1.2.18：`langgraph/types.py`（Send、Command、Checkpointer）、`pregel/_algo.py`（准备任务、合并写入）、`pregel/_runner.py` 和 `_executor.py`（任务怎么并行跑、失败了怎么办）、`pregel/main.py`（子图的检查点）、`pregel/_messages.py`（messages 流怎么处理嵌套的图）、`prebuilt/tool_node.py`（工具返回 Command）；
- langchain 1.4.7：`langchain/agents/factory.py`、`_subagent_transformer.py`。

实验都用假模型、假网关。前两个模式是纯 StateGraph；后面在进程里跑灵稿的运行器，图照 `agent.build_agent` 搭，只多加要试的工具。代码在 `实验/05-subgraph-send/exp.py`，用法写在文件开头。真实数据是换 PG 之前那 17 个对话，只读复制出来查的（同第 3 篇）。

## Send：一步里同时跑好几个

**是什么**

- `Send(node, arg, timeout=None)`：让 `node` 在下一步跑一次，拿到的参数就是 `arg`，不是整个状态。
- 一般由条件边返回一串 Send，同一个节点可以发好几次，参数各不相同。这就是 map-reduce 的写法。

**怎么跑**

- 条件边返回的 Send 先写进一个内部通道（`TASKS`，是个 Topic）。
- 下一步开始时，`prepare_next_tasks` 给每个 Send 建一个 PUSH 任务，再加上普通边触发的节点（PULL 任务），这一步要跑的就是这些。
- `_runner.tick` 的跑法：
  - 只有一个任务时，在当前线程直接跑；
  - 有几个任务时，同步版放进线程池，异步版建几个 asyncio 任务。
- 并发数由 config 里的 `max_concurrency` 控制，默认不限。

**结果怎么合并**

- 一步里所有任务的写入，等这一步结束再一起合并。
- 合并前按任务路径排序（`apply_writes`），所以结果的顺序是发 Send 的顺序，和谁先做完无关。
- 几个任务写同一个字段，这个字段必须有 reducer（比如 `Annotated[list, operator.add]`），否则报 `InvalidUpdateError`。

**一个失败了怎么办**

- 每个任务一做完，它的写入就单独存进检查点（`put_writes`），叫 pending writes，不等这一步结束。
- 有任务失败，`_should_stop_others` 让主循环不再等别的任务，准备抛出错误，这一步不合并。但同步版正在跑的线程停不下来，退出时还是要等它们做完（见实验 send-fail）。异步版是什么样，我没试。
- 接着跑（`invoke(None, config)`）时，存过写入的任务不重跑，只重跑失败的那个。

**`create_agent` 里就有**：一次回复里有几个工具调用时，模型到工具的那条边是每个调用发一个 `Send("tools", …)`（第 3 篇提过）。所以模型同时调几个工具，就是这一步里有几个 PUSH 任务并行跑。

### 实验 send

三个分支分别睡 0.3、0.1、0.2 秒：

- 一共 0.31 秒，三个分支跑在三个线程上。
- 结果顺序是 0、1、2，是发的顺序，不是做完的顺序（1、2、0）。
- 节点拿到的参数只有 `delay` 和 `i`，就是 Send 带过去的那两个。
- 父图一共 3 个检查点：三个分支是同一步，只有一个检查点。
- `max_concurrency=1`：0.61 秒，一个一个跑。
- 结果字段不加 reducer：`InvalidUpdateError: At key 'results': Can receive only one value per step.`
- `Send(timeout=0.15)`：一开始就报 `ValueError`，"Node timeouts are only supported for async nodes"。同步的节点没法从外面打断，所以不支持超时。

### 实验 send-fail

三个分支：0 号 0.05 秒做完；1 号 0.1 秒时出错；2 号要 0.4 秒。

- **0.41 秒才抛出错误**，也就是等 2 号做完了才抛。同步版的线程停不下来，线程池关闭时会等正在跑的任务做完。
- 存下来的 pending writes：0 号、2 号的结果，加上 1 号的错误。
- `get_state` 看到的 results 已经是 [0, 2]，next 还是 work。三个任务里，1 号带着错误，另外两个带着结果。
- 不再出错以后接着跑：结果 [0, 1, 2]。0 号、2 号各跑了 1 次，只有 1 号跑了 2 次。

### 灵稿：真实对话里常常同时调几个工具

17 个对话里：

- 有 11 个出现过一次回复同时调多个工具；
- 模型调工具的 34 次回复里，1 个工具的 14 次，2 个的 17 次，3 个的 3 次；
- 同时调的几乎都是几个搜索（16 次两个搜索，3 次三个搜索，1 次一个读页加一个搜索）。

也就是说，**一多半的工具调用本来就是靠 Send 并行跑的**。

**实验 agent-parallel**（假模型第一次回复同时搜两个，每次搜索慢 0.5 秒）：

- 这一轮一共 0.52 秒，两次搜索在两个线程上同时开始、同时结束。
- 进度事件的顺序：甲开始、乙开始、甲做完、乙做完，然后是读页开始、读页做完。ToolProgressMiddleware 的 `wrap_tool_call` 在两个线程里同时跑，进度没有乱。
- 页面这一轮：思考、搜索、搜索、思考、读页、思考、正文；来源 6 条。和一个一个搜的样子一样。

## 子图：一个编译好的图当另一个图的节点

**两种接法**

- 直接 `add_node("sub", 编译好的子图)`：父图把自己的状态交给子图，同名的字段对上。子图跑完，把它的状态写回父图，**按父图那个字段的 reducer 合并**。
- 在函数节点里调 `sub.invoke(...)`：父子的字段可以不一样，进出自己转换。

**子图的检查点（`Checkpointer = None | bool | BaseCheckpointSaver`）**

- `None`（默认）：用父图的检查点存储，但每次调用都是新的。存在 `节点名:任务 id` 这个命名空间下，任务 id 每次不同。中途 interrupt 能恢复，下次再调就从头开始。
- `True`：命名空间去掉任务 id，只剩 `节点名`（`_own_checkpoint_config`），所以同一个对话里每次调用都接着上次的状态。根图不能设 True。
- `False`：不存检查点，父图有也不用。

**看子图的状态**：`get_state(config, subgraphs=True)`，停着的那个任务的 `.state` 就是子图的状态。

**看子图的流**：`stream(..., subgraphs=True)`，每条带命名空间，是 `(ns, mode, chunk)`。**不开的话，嵌套的图发出的 messages、custom 一律丢掉**（`_messages.py`："not self.subgraphs and len(ns) > 0" 就不发）。

### 实验 subgraph

**checkpointer 取 None 和 True**（子图有一个只有它自己有的字段 seen，记下见过的问题；同一个对话问两次）：

- None：两次都是"见过 1 个问题"。检查点：父图 6 个，`sub:…` 8 个（每次调用 4 个，命名空间各不相同）。
- True：第二次是"见过 2 个问题：第一问、第二问"。检查点在 `sub` 这一个命名空间下。

**共用一个 `operator.add` 字段**：子图往 notes 里加一条，说两次以后，父图的 notes 是 `['note1', 'note1', 'note2']`。

- 原因：子图拿到父图的 notes，加一条，再把整个列表写回去，父图的 reducer 又把它整个接在后面。
- `add_messages` 按消息 id 去重，所以 messages 字段没有这个问题。别的带 reducer 的字段都要注意。

**子图里 interrupt 再恢复**（父图：before → sub；子图：prep → ask（interrupt）→ finish）：

| 接法 | 停下时 | 恢复后各节点跑了几次 |
|---|---|---|
| 子图直接当节点 | 父图 next=sub，子图 next=ask | before 1、prep 1、ask 2、finish 1 |
| 在函数节点里调子图 | 一样 | before 1、**函数节点 2**、prep 1、ask 2、finish 1 |

- 子图从它自己的检查点接着跑，prep 不重跑。
- 调 interrupt 的那个节点从头再跑一次。普通的 interrupt 也是这样。
- 函数节点会整个重跑。它里面调子图之前的代码会再执行一遍，但调到子图时，子图仍然从断点接着跑。
- 开 `subgraphs=True` 时，interrupt 在子图那层和父图那层各冒出来一次。

## 多 agent

LangChain 1.4.7 没有现成的 supervisor、swarm 这类函数。多 agent 就是用上面这些零件拼，常见两种拼法。

**一、子 agent 当工具**（主 agent 调一个工具，工具里 `invoke` 另一个 `create_agent`）

- 工具里调的图会继承当前的 config：回调（我们的 StopCheck、messages 流的处理器）和检查点存储都带进去了。
- 子 agent 的检查点存在 `tools:任务 id` 下面。
- `create_agent(name=...)` 会把名字写进运行时的 metadata（`lc_agent_name`）。新的流式接口 `stream_events(version="v3")` 靠它认出"这是一个子 agent"，在 `run.subagents` 上单独给一个句柄（`SubagentTransformer`），还能对上是主 agent 的哪个工具调用发起的。这个接口我们没用，这次也没试。

**二、交接（handoff）**（几个 agent 当父图的几个节点，一个 agent 的工具返回 `Command(graph=Command.PARENT, goto="另一个", update=…)`）

- ToolNode 收到 `graph=Command.PARENT` 的 Command，把它往上交给父图，父图转到 `goto` 那个节点。
- `update` 里要带上发起调用的那条模型消息和这次的工具结果，否则接手的 agent 看到的历史里，这个工具调用没有结果。

### 实验 subagent（主 agent 有个 ask_researcher 工具，工具里调研究员；研究员照常搜、读、答）

- **流到页面的正文只有主 agent 的**："主 agent：按研究员查到的，答案是可以用。"研究员的思考和正文一个字都没流出来。
- **研究员的进度事件也没有**，给它挂上 ToolProgressMiddleware 也一样。原因同上：我们的运行器没开 `subgraphs=True`，嵌套的图发出的 messages 和 custom 都被丢掉了。
- **主 agent 的状态里只有一条工具结果**：human、ai、tool:ask_researcher、ai。研究员搜了什么、读了什么，都在它自己的检查点里。
- **页面**：这一轮是思考、思考、正文，**来源 0 条**。页面上的来源和引用编号都是从主 agent 的消息里算的，研究员查到的资料主 agent 这边没有。
- **检查点**：父图 10 个，`tools:…` 下研究员 7 个。第 2 篇说过，每一步都存一个检查点；子 agent 跑一次又多一套。
- **打开 `subgraphs=True` 直接看图的流**：
  - 研究员的进度 search 2 次、read 2 次都出来了；
  - **研究员的正文片段带的 `langgraph_node` 也是 `"model"`**。我们的运行器只按 `langgraph_node == "model"` 筛选，要是为了显示研究员的进度打开 `subgraphs=True`，它的正文会混进主回答。还得再按命名空间筛一次。
- **研究员干活时点停止**：停住了，这一轮只剩第一段思考，最后发出了这一轮结束的事件。StopCheck 是回调，跟着 config 进了研究员，研究员一调模型就抛出 Stopped。工具默认只吞 `ToolInvocationError`，别的异常照常往外抛，所以没被当成工具错误。

### 实验 handoff（前台和专家两个 `create_agent`，前台的工具把对话转给专家；同一个对话说两句话）

- **每句话都从前台开始**（父图的 START 固定接前台）：第二句又经过一次转接。
- **按上次在谁那里**（交接时在父图状态里记一个 active，START 按它分）：第二句直接到专家。
- 父图不会自己记得"上次在谁那里"。每句话都从 START 开始，要接着找上次那个 agent，得自己存。
- 检查点：每个 agent 每被调一次，都在 `triage:…`、`expert:…` 下存一套。

## 和灵稿现在比

- **Send 已经天天在用**：模型同时调几个工具时，这几个调用就是靠 Send 并行跑的，真实对话里一多半的工具调用都是这样。实验里并行跑、进度、页面都正常，不用改。
- **单 agent**：灵稿没有子图、没有多 agent。

如果以后要加子 agent（比如让一个研究员在它自己的上下文里读很多页，只把结论交回来），从实验看要处理这几件事：

1. **页面看不到它**：思考、进度现在都不显示。要显示就得开 `subgraphs=True`，同时筛选条件里加上命名空间，不然它的正文会混进主回答。
2. **来源会丢**：它查到的资料不在主 agent 的消息里，页面上的来源和引用编号都对不上。要把资料随工具结果一起交回来（比如放进 ToolMessage 的 artifact），`collect_sources` 也要跟着改。
3. **检查点会多**：每次调用它都多存一套检查点。第 2 篇已经说过检查点存得多。
4. **停止没问题**：回调会传进去，停得下来。
5. **重答和分叉**：它的检查点在每次调用各自的命名空间下，主 agent 重答，它就重新跑一次。这件事我没专门试。

交接这种做法，对现在"一个对话一个 agent"的 chat 用处不大。

## 建议（等 Sean 定）

1. **Send 和并行工具**：现在就在用，工作正常，不用改。
2. **现在不加子 agent、不做多 agent**。理由：
   - 我们的对话最长的不到上限的 5%（第 3 篇），用不着靠子 agent 分担上下文；
   - 加了就要处理上面的页面显示、来源、检查点三件事。
3. **以后真要加**：先用"子 agent 当工具"这种做法，按上面 1、2 两条改运行器和来源。到时候要不要换成 LangGraph 新的流式接口（`stream_events(version="v3")`，能直接认出子 agent），可以再单独看一次。
