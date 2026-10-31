"""对照实验：灵稿的对话图挂在 langgraph dev 上，用 SDK 试普通对话、断线续看、取消、双发策略、从检查点分叉。

用法（先按同目录的 README 起 langgraph dev）：uv run python exp.py e1 e2 e3:interrupt e3:rollback e4:reject e4:enqueue e4:interrupt e4:rollback e5 e6
"""
import asyncio, json, sys, time
from collections import Counter
from langgraph_sdk import get_client

c = get_client(url="http://127.0.0.1:2024")
A = "chat"
say = lambda t: {"messages": [{"role": "user", "content": t}]}


def brief(state):
    v = state["values"]
    msgs = [(m["type"], (m.get("content") or "")[:24] if isinstance(m.get("content"), str) else "list",
             [tc["name"] for tc in m.get("tool_calls", [])]) for m in v.get("messages", [])]
    turns = [(t["outcome"], bool(t["ended_at"])) for t in v.get("turns", [])]
    return {"next": state["next"], "title": v.get("title"), "turns": turns, "msgs": msgs}


async def e1():
    t = await c.threads.create()
    kinds = Counter()
    async for p in c.runs.stream(t["thread_id"], A, input=say("杭州有什么好玩的"), stream_mode=["messages-tuple", "updates", "custom"]):
        kinds[p.event if p.event != "custom" else "custom:" + p.data.get("type", "?")] += 1
    print("E1 events", dict(kinds))
    print("E1 state", json.dumps(brief(await c.threads.get_state(t["thread_id"])), ensure_ascii=False))


async def e2():
    t = await c.threads.create(); tid = t["thread_id"]
    run = await c.runs.create(tid, A, input=say("慢一点查杭州"), stream_mode=["updates", "custom"], stream_resumable=True)
    seen, last = [], None
    async for p in c.runs.join_stream(tid, run["run_id"], stream_mode=["updates", "custom"]):
        seen.append((p.id, p.event)); last = p.id
        if len(seen) == 3:
            break  # 模拟断线
    print("E2 first 3", seen)
    await asyncio.sleep(2)
    rest = [(p.id, p.event) async for p in c.runs.join_stream(tid, run["run_id"], last_event_id=last, stream_mode=["updates", "custom"])]
    print("E2 resumed", len(rest), rest[:2], "...", rest[-1:])
    print("E2 run", (await c.runs.get(tid, run["run_id"]))["status"])


async def e3(action):
    t = await c.threads.create(); tid = t["thread_id"]
    run = await c.runs.create(tid, A, input=say("慢一点查杭州"))
    await asyncio.sleep(2.0)  # 正在慢搜索
    t0 = time.time()
    await c.runs.cancel(tid, run["run_id"], wait=True, action=action)
    print(f"E3[{action}] cancel took {time.time()-t0:.1f}s; run", (await c.runs.get(tid, run["run_id"]))["status"] if action != "rollback" else "(deleted?)")
    print(f"E3[{action}] state", json.dumps(brief(await c.threads.get_state(tid)), ensure_ascii=False))
    print(f"E3[{action}] thread status", (await c.threads.get(tid))["status"])
    return tid


async def e4(strategy):
    t = await c.threads.create(); tid = t["thread_id"]
    r1 = await c.runs.create(tid, A, input=say("慢一点查杭州"))
    await asyncio.sleep(1.0)
    try:
        r2 = await c.runs.create(tid, A, input=say("直接答：那苏州呢"), multitask_strategy=strategy)
    except Exception as e:
        print(f"E4[{strategy}] second run refused:", type(e).__name__, str(e)[:120])
        await c.runs.join(tid, r1["run_id"])
        return
    await c.runs.join(tid, r2["run_id"])
    runs = await c.runs.list(tid)
    print(f"E4[{strategy}] runs", [(r["run_id"][-4:], r["status"]) for r in runs])
    print(f"E4[{strategy}] state", json.dumps(brief(await c.threads.get_state(tid)), ensure_ascii=False))


async def e5():
    t = await c.threads.create(); tid = t["thread_id"]
    await c.runs.wait(tid, A, input=say("直接答：第一问"))
    await c.runs.wait(tid, A, input=say("直接答：第二问"))
    hist = await c.threads.get_history(tid, limit=50)
    print("E5 history len", len(hist), "next of each:", [h["next"] for h in hist][:8])
    # 找第二轮开始前的检查点：消息里只有第一轮（2 条）且已跑完
    before = next(h for h in hist if len(h["values"].get("messages", [])) == 2 and not h["next"])
    cp = before["checkpoint"]
    await c.runs.wait(tid, A, input=say("直接答：第二问换个说法"), checkpoint=cp)
    st = await c.threads.get_state(tid)
    print("E5 after fork", json.dumps(brief(st), ensure_ascii=False))
    hist2 = await c.threads.get_history(tid, limit=100)
    print("E5 history len after fork", len(hist2), "parents of latest:", st["parent_checkpoint"]["checkpoint_id"][-6:] if st.get("parent_checkpoint") else None)


async def e6():
    """取消（interrupt）以后接着说一句：悬空的工具调用、没收尾的那一轮会怎样"""
    tid = await e3("interrupt")
    await c.runs.wait(tid, A, input=say("直接答：接着问"))
    st = await c.threads.get_state(tid)
    print("E6 after next msg", json.dumps(brief(st), ensure_ascii=False))


async def main():
    which = sys.argv[1:]
    for w in which:
        if w == "e1": await e1()
        if w == "e2": await e2()
        if w.startswith("e3"): await e3(w.split(":")[1])
        if w.startswith("e4"): await e4(w.split(":")[1])
        if w == "e5": await e5()
        if w == "e6": await e6()

asyncio.run(main())

