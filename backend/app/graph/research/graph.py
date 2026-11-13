"""对话线程的存储：检查点、长期记忆的 Store 和线程列表。图本身就是对话 agent（见 agent.build_agent）。

存在 Postgres 里（LINGDRAFT_DATABASE_URL），检查点、Store 和运行记录共用一个连接池（见 postgres_pool，在 ResearchRunner 的 open_storage 里打开）；
没配库、连不上，服务启动时就报错。测试和评测用内存的（memory_checkpointer、InMemoryStore）。
agent 在对话的事件循环里异步地跑（见 app.aio），检查点和 Store 用异步的那几种，在那个循环里打开（都是协程，用 aio.run 调）。
它们的同步方法在别的线程里也能用（转到那个循环里做、等结果），整理记忆的后台线程就这样用；在那个循环里调同步方法会报错或卡住。
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.graph.research.agent import build_agent
from app.graph.research.memory import MemoryBook
from app.research_llm import ResearchPlanner

ALLOWED_MODELS = [*(("app.graph.research.state", n) for n in ("Turn", "Usage", "Compaction")), ("app.model_catalog", "ModelUse")]
TUNNEL = "本机连 dev 的库要先开隧道：ssh -N -L 15432:127.0.0.1:5432 dev（见 .env.example）"
NO_DATABASE = f"没配 LINGDRAFT_DATABASE_URL：对话存在 Postgres 里，没配库不能启动。{TUNNEL}"
NO_POSTGRES = f"连不上 Postgres（LINGDRAFT_DATABASE_URL）。{TUNNEL}"


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODELS)


async def postgres_pool(url: str) -> AsyncConnectionPool:
    """对话的存储共用的连接池：检查点、Store、运行记录（见 ResearchRunner 的 open_storage）。没配库抛 RuntimeError(NO_DATABASE)。
    AsyncPostgresSaver 的读写持着自己的锁，同一时刻只用一个连接；AsyncPostgresStore 和运行记录每次读写各取一个，用完就还，所以池子开得小。
    用池子还为了连接断了（比如本机的隧道断过）能换新的：取连接时先检查一下，坏的扔掉重连。连接参数照它们的 from_conn_string。
    打开时不等连上：连不上的话第一次取连接等 timeout 秒抛 PoolTimeout。"""
    if not url:
        raise RuntimeError(NO_DATABASE)
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, timeout=10, open=False, check=AsyncConnectionPool.check_connection,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    await pool.open()
    return pool


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=make_serde())


THREADS_SQL = "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''"


async def thread_ids(checkpointer: BaseCheckpointSaver) -> list[str]:
    """检查点里有哪些线程。checkpointer.alist(None) 会把所有检查点都反序列化一遍，这里直接查表。"""
    if isinstance(checkpointer, AsyncPostgresSaver):
        async with checkpointer._cursor() as cur:  # 带锁，从池子里取连接；它没有公开的取游标方法
            await cur.execute(THREADS_SQL)
            rows = await cur.fetchall()
        return [r["thread_id"] for r in rows]
    if isinstance(checkpointer, InMemorySaver):
        return [t for t, by_ns in list(checkpointer.storage.items()) if by_ns.get("")]
    raise TypeError(f"不支持列出线程的检查点存储：{type(checkpointer).__name__}")


# 每个线程一次倒着走主键（thread_id, checkpoint_ns, checkpoint_id）取第一条，不碰检查点的内容；存储自己取最新的也是按编号倒排取第一条
LATEST_SQL = ("SELECT t.thread_id, (SELECT c.checkpoint_id FROM checkpoints c WHERE c.thread_id = t.thread_id AND c.checkpoint_ns = '' "
              "ORDER BY c.checkpoint_id DESC LIMIT 1) AS checkpoint_id FROM unnest(%s::text[]) AS t(thread_id)")


async def latest_checkpoints(checkpointer: BaseCheckpointSaver, threads: list[str]) -> dict[str, str]:
    """这些线程各自最新那个检查点的编号（没有检查点的不在里面）。只查编号，不读检查点的内容：
    线程列表拿它看记着的那几项还对不对得上（见 ResearchRunner._alist_threads），一次查齐。"""
    if not threads:
        return {}
    if isinstance(checkpointer, AsyncPostgresSaver):
        async with checkpointer._cursor() as cur:  # 同 thread_ids
            await cur.execute(LATEST_SQL, (threads,))
            rows = await cur.fetchall()
        return {r["thread_id"]: r["checkpoint_id"] for r in rows if r["checkpoint_id"]}
    if isinstance(checkpointer, InMemorySaver):
        found = {t: (checkpointer.storage.get(t) or {}).get("") for t in threads}  # 用 get：storage 是 defaultdict，下标取会建出空的
        return {t: max(ids) for t, ids in found.items() if ids}
    raise TypeError(f"不支持查最新检查点的检查点存储：{type(checkpointer).__name__}")


async def keep_latest(checkpointer: BaseCheckpointSaver, thread_id: str) -> None:
    """这段对话只留最新的检查点，以前的删掉（存储自带的 aprune，见 langgraph.checkpoint.postgres 的 prune）。
    留下的：每个命名空间最新的那一个、挂在它上面还没并进去的写入、它用到的内容，也就是存储取最新状态时读的全部，接着说、回答问题卡、重试都只用这些。
    删掉的：以前的检查点和只有它们用的内容，做完了的子 agent 那次调用的命名空间。产品里没有地方读以前的检查点（没有回到某一步、分叉）。
    为什么要删：messages 每变一次就整份另存一条，一轮至少两次，不删的话一段对话占的空间随轮数平方涨（dev 上清之前 98% 是以前的）。
    在一个事务里按当时的快照删，这段对话同时在跑也没事：它新存的看不到也不会删。
    只管 Postgres：内存的不删（进程没了就没了；评测录像要每一步的检查点，见 app.evals.decisions）。"""
    if isinstance(checkpointer, AsyncPostgresSaver):
        await checkpointer.aprune([thread_id], strategy="keep_latest")


STALE_SQL = "SELECT thread_id FROM checkpoints GROUP BY thread_id HAVING count(*) > 1"


async def stale_threads(checkpointer: BaseCheckpointSaver) -> list[str]:
    """检查点不止一个的线程（各命名空间都算）：有以前的没清。只数主键，不读内容。"""
    if not isinstance(checkpointer, AsyncPostgresSaver):
        return []
    async with checkpointer._cursor() as cur:  # 同 thread_ids
        await cur.execute(STALE_SQL)
        rows = await cur.fetchall()
    return [r["thread_id"] for r in rows]


def build_graph(planner: ResearchPlanner, checkpointer: BaseCheckpointSaver, memory: MemoryBook | None = None):
    return build_agent(planner, checkpointer, memory)
