"""实验 02：检查点。在进程里直接跑灵稿的对话图（假模型、假网关，读页正文放大到接近真实的 6000 字）。

- growth：连说 N 轮，看检查点的条数和字节数怎么长。SQLite / Postgres × 消息通道普通 / DeltaChannel，四种组合。
- growth-exit：durability=exit 时同样连说 N 轮。
- durability：sync / async / exit 三种持久化模式，一轮各写几个检查点。
- retry：一轮中途出错（工具接不住的错误），async 和 exit 两种模式下能不能从断点重试。
- fork：时间回溯。从检查点分叉重新回答、改了原话重发、列出分支、切回旧分支。fork+delta 是消息通道换成 DeltaChannel 再做一遍。

用法（在 backend 下）：
  PYTHONPATH=. uv run --with pgserver --with langgraph-checkpoint-postgres --with "psycopg[binary]" \
    python "../docs/LangGraph研究/实验/02-checkpoint/exp.py" growth growth-exit durability retry fork fork+delta
"""

import dataclasses
import sqlite3
import sys
import tempfile
import time
from functools import reduce
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.constants import START
from langgraph.graph.message import add_messages

from app.graph.research import agent
from app.graph.research.graph import make_serde, open_sqlite_checkpointer
from app.graph.research.state import ResearchState
from app.research_llm import FakeResearchPlanner, fake_gateway_client
from app.tools.images import ImageStore

TMP = Path(tempfile.mkdtemp(prefix="lg-cp-"))
CTX = agent.ChatContext(images=ImageStore(TMP / "images"), check_stop=lambda: None)


def add_messages_batch(state, writes):
    """DeltaChannel 的 reducer 一次收一批写入：按顺序折叠 add_messages，分几批折和一次折结果一样。"""
    return reduce(add_messages, writes, state if state is not None else [])


class DeltaState(ResearchState):
    messages: Annotated[list[AnyMessage], DeltaChannel(add_messages_batch, snapshot_frequency=20)]


def build(saver, delta: bool):
    gw = fake_gateway_client()
    _read = gw.read

    def big_read(url, **kw):
        r = _read(url, **kw)
        return dataclasses.replace(r, content=(r.content * 200)[:6000]) if r.ok else r

    gw.read = big_read
    agent.ResearchState = DeltaState if delta else ResearchState  # build_agent 用的 state_schema
    try:
        return agent.build_agent(FakeResearchPlanner(gw), saver)
    finally:
        agent.ResearchState = ResearchState


def cfg(tid, cid=None):
    c = {"configurable": {"thread_id": tid}}
    if cid:
        c["configurable"]["checkpoint_id"] = cid
    return c


def turn(graph, tid, text, durability="async"):
    """和运行器一样：先把用户的话写进去（as_node=START），再从这里接着跑。"""
    graph.update_state(cfg(tid), {"messages": [HumanMessage(text)]}, as_node=START)
    for _ in graph.stream(None, cfg(tid), context=CTX, stream_mode="updates", durability=durability):
        pass


# ---- 存储量 ----

def sqlite_size(path):
    c = sqlite3.connect(path)
    n, cp = c.execute("select count(*), coalesce(sum(length(checkpoint)), 0) from checkpoints").fetchone()
    w = c.execute("select coalesce(sum(length(value)), 0) from writes").fetchone()[0]
    c.close()
    return n, cp, w, 0


def pg_size(conn):
    with conn.cursor() as cur:
        cur.execute("select count(*), coalesce(sum(octet_length(checkpoint::text)), 0) from checkpoints")
        n, cp = cur.fetchone().values()
        cur.execute("select coalesce(sum(octet_length(blob)), 0) from checkpoint_writes")
        w = list(cur.fetchone().values())[0]
        cur.execute("select coalesce(sum(octet_length(blob)), 0) from checkpoint_blobs")
        b = list(cur.fetchone().values())[0]
    return n, cp, w, b


def kb(x):
    return f"{x / 1024:.0f}K"


def growth(turns=12):
    import pgserver
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    srv = pgserver.get_server(TMP / "pg")
    for backend in ("sqlite", "postgres"):
        for delta in (False, True):
            name = f"{backend}{'+delta' if delta else ''}"
            if backend == "sqlite":
                path = TMP / f"{name}.sqlite"
                saver = open_sqlite_checkpointer(path)
                size = lambda: sqlite_size(path)
            else:
                db = name.replace("+", "_")
                srv.psql(f"create database {db};")
                conn = psycopg.connect(srv.get_uri(db), autocommit=True, prepare_threshold=0, row_factory=dict_row)
                saver = PostgresSaver(conn, serde=make_serde())
                saver.setup()
                size = lambda: pg_size(conn)
            g = build(saver, delta)
            tid = "t1"
            for k in range(1, turns + 1):
                turn(g, tid, f"第{k}个问题：杭州有什么好玩的")
                if k in (1, 4, 8, turns):
                    n, cp, w, b = size()
                    print(f"{name:16} 第{k:2}轮  检查点 {n:3} 个  检查点 {kb(cp):>6}  写入 {kb(w):>6}  blob {kb(b):>6}  合计 {kb(cp + w + b):>6}")
            t0 = time.perf_counter()
            st = g.get_state(cfg(tid))
            print(f"{name:16} 读一次当前状态 {1000 * (time.perf_counter() - t0):.0f}ms，消息 {len(st.values['messages'])} 条，轮次 {len(st.values['turns'])}")


def growth_exit(turns=12):
    """durability=exit：一轮只在开头（用户的话）和结束各留一个检查点。"""
    for delta in (False, True):
        path = TMP / f"exit{'-delta' if delta else ''}.sqlite"
        g = build(open_sqlite_checkpointer(path), delta)
        for k in range(1, turns + 1):
            turn(g, "t", f"第{k}个问题：杭州有什么好玩的", durability="exit")
        n, cp, w, _ = sqlite_size(path)
        print(f"sqlite exit{'+delta' if delta else '':6}  第{turns}轮  检查点 {n} 个  合计 {kb(cp + w)}")


def retry():
    """读页时网关鉴权失败（工具接不住、往外抛），这一轮断在第二个工具。两种持久化模式下看断点留没留住、重试能不能接着跑。"""
    import app.tools.gateway as gwmod

    global fake_gateway_client
    broken = {"on": True}
    orig = fake_gateway_client

    def flaky():
        c = orig()
        real = c.read

        def read(url, **kw):
            if broken["on"]:
                raise gwmod.GatewayAuthError("网关密钥失效")
            return real(url, **kw)

        c.read = read
        return c

    fake_gateway_client = flaky
    try:
        for mode in ("async", "exit"):
            broken["on"] = True
            g = build(open_sqlite_checkpointer(TMP / f"retry-{mode}.sqlite"), False)
            g.update_state(cfg("e"), {"messages": [HumanMessage("杭州有什么好玩的")]}, as_node=START)
            try:
                for _ in g.stream(None, cfg("e"), context=CTX, stream_mode="updates", durability=mode):
                    pass
            except gwmod.GatewayAuthError:
                pass
            st = g.get_state(cfg("e"))
            print(f"{mode:5} 出错后：next={list(st.next)}  消息 {[m.type for m in st.values['messages']]}  检查点 {len(list(g.get_state_history(cfg('e'))))} 个")
            broken["on"] = False
            for _ in g.stream(None, cfg("e"), context=CTX, stream_mode="updates", durability=mode):
                pass
            st = g.get_state(cfg("e"))
            print(f"{mode:5} 重试后：消息 {[m.type for m in st.values['messages']]}")
    finally:
        fake_gateway_client = orig


# ---- 持久化模式 ----

def durability():
    for mode in ("sync", "async", "exit"):
        path = TMP / f"dur-{mode}.sqlite"
        g = build(open_sqlite_checkpointer(path), False)
        turn(g, "t", "杭州有什么好玩的", durability=mode)
        hist = list(g.get_state_history(cfg("t")))
        print(f"durability={mode:5}  一轮写了 {len(hist)} 个检查点："
              + " → ".join(f"{h.metadata['source']}{h.metadata['step']}" for h in reversed(hist)))


# ---- 时间回溯 ----

def short(c):
    return c["configurable"]["checkpoint_id"][-6:]


def answers(state):
    return [m.content[:14] for m in state.values["messages"] if m.type in ("human", "ai") and m.content]


def fork(delta=False):
    g = build(open_sqlite_checkpointer(TMP / f"fork{'-delta' if delta else ''}.sqlite"), delta)
    tid = "f"
    for q in ("直接答：第一问", "直接答：第二问", "直接答：第三问"):
        turn(g, tid, q)
    hist = list(g.get_state_history(cfg(tid)))
    print(f"3 轮共 {len(hist)} 个检查点。最近 8 个（新 → 旧）：")
    for h in hist[:8]:
        print(f"  {short(h.config)}  父 {short(h.parent_config) if h.parent_config else '-':6}  {h.metadata['source']:6} step={h.metadata['step']:2}  "
              f"next={list(h.next)}  消息 {len(h.values['messages'])} 条")

    # 重新回答：找到第三问刚写进去、还没开始跑的那个检查点（运行器 update_state 写的，source=update），从那里接着跑
    q3 = next(h for h in hist if h.metadata["source"] == "update" and h.values["messages"][-1].content == "直接答：第三问")
    old_head = g.get_state(cfg(tid))
    for _ in g.stream(None, q3.config, context=CTX, stream_mode="updates"):
        pass
    new_head = g.get_state(cfg(tid))
    print(f"\n重新回答：从 {short(q3.config)} 接着跑。当前状态 {short(new_head.config)}，旧的 {short(old_head.config)} 还在："
          f"旧回答 id {old_head.values['messages'][-1].id[-6:]}，新回答 id {new_head.values['messages'][-1].id[-6:]}")
    print(f"  当前：{answers(new_head)}")
    print(f"  旧的（按 checkpoint_id 取）：{answers(g.get_state(old_head.config))}")

    # 改了原话：在第三问写进去之前的那个检查点上 update_state，再接着跑
    before_q3 = q3.parent_config
    edited = g.update_state(before_q3, {"messages": [HumanMessage("直接答：第三问（改）")]}, as_node=START)
    for _ in g.stream(None, edited, context=CTX, stream_mode="updates"):
        pass
    print(f"\n改原话：从 {short(before_q3)} 分叉。当前：{answers(g.get_state(cfg(tid)))}")

    # 列分支：同一个父检查点下有几个孩子
    hist = list(g.get_state_history(cfg(tid)))
    kids = {}
    for h in hist:
        if h.parent_config:
            kids.setdefault(short(h.parent_config), []).append(h)
    for p, ks in kids.items():
        if len(ks) > 1:
            print(f"  分叉点 {p}：{len(ks)} 个孩子 → " + "，".join(f"{short(k.config)}({k.metadata['source']})" for k in ks))

    # 切回旧分支：get_state 只认最新的检查点，没有“当前分支”指针。在旧分支的末端写一个空更新，它就成了最新的
    back = g.update_state(old_head.config, None)
    print(f"\n切回最早那个回答：在 {short(old_head.config)} 上写一个空更新 → 新检查点 {short(back)}，当前：{answers(g.get_state(cfg(tid)))}")
    print(f"总检查点数 {len(list(g.get_state_history(cfg(tid))))}")


if __name__ == "__main__":
    for w in sys.argv[1:]:
        print(f"\n===== {w} =====")
        {"growth": growth, "growth-exit": growth_exit, "durability": durability, "retry": retry,
         "fork": fork, "fork+delta": lambda: fork(True)}[w]()
