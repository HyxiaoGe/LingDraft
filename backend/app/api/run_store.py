"""运行记录：每次执行（发一句话、重试、重新回答……）一行，存进库里，事后能查历史运行和失败原因。

学的是 LangGraph Agent Server 的 run：执行登记（RunRegistry）在进程内存里，跑完就没了；这里把每次执行的来历、状态、时间、错误落盘。
和检查点存在一处：配了 LINGDRAFT_DATABASE_URL 就是 Postgres 的 runs 表，没配是数据目录下的 runs.sqlite，测试和评测用内存的。
时间由存储自己记（UTC）。删掉对话时它的运行记录一起删。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config

QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST = "queued", "running", "success", "stopped", "error", "lost"
# success：跑到头了（这一轮的结果另看检查点里的轮记录，比如被内容审核拦下）；stopped：用户点了停止；
# error：抛了错；lost：进程重启时还在排队或在跑（服务启动时补记，见 sweep）
STATUSES = (QUEUED, RUNNING, SUCCESS, STOPPED, ERROR, LOST)
FIELDS = ("id", "thread_id", "kind", "turn_id", "status", "created_at", "started_at", "ended_at", "error_type", "error", "traceback", "version")


class RunStore(Protocol):
    """一行的字段见 FIELDS：kind 是这次执行怎么来的（new / say / retry / regenerate / edit），turn_id 是这一轮用户那句话的消息 id，
    version 是跑它的代码版本（部署时的提交号）。列表都是新的在前（按写入的先后，同一毫秒建的也分得清），时间是 ISO 字符串。"""

    def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None: ...
    def started(self, run_id: str) -> None: ...
    def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
               traceback: str | None = None) -> None: ...
    def for_thread(self, thread_id: str) -> list[dict]: ...
    def recent(self, status: str | None, limit: int) -> list[dict]: ...
    def sweep(self) -> int: ...  # 还在排队、在跑的记成 lost，返回条数。只在服务启动时调：单进程部署，这时它们都已经没在跑了
    def delete_thread(self, thread_id: str) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: Any) -> str | None:
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.astimezone(UTC).isoformat(timespec="milliseconds")
    return t


class MemoryRunStore:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._rows: dict[str, dict] = {}  # 按写入的先后

    def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None:
        row = dict.fromkeys(FIELDS) | {"id": run_id, "thread_id": thread_id, "kind": kind, "turn_id": turn_id, "status": QUEUED,
                                        "created_at": _iso(_now()), "version": version}
        with self._mu:
            self._rows[run_id] = row

    def started(self, run_id: str) -> None:
        with self._mu:
            if row := self._rows.get(run_id):
                row.update(status=RUNNING, started_at=_iso(_now()))

    def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
               traceback: str | None = None) -> None:
        with self._mu:
            if row := self._rows.get(run_id):
                row.update(status=status, ended_at=_iso(_now()), error_type=error_type, error=error, traceback=traceback)

    def _newest(self, rows) -> list[dict]:
        return [dict(r) for r in reversed(list(rows))]

    def for_thread(self, thread_id: str) -> list[dict]:
        with self._mu:
            return self._newest(r for r in self._rows.values() if r["thread_id"] == thread_id)

    def recent(self, status: str | None, limit: int) -> list[dict]:
        with self._mu:
            return self._newest(r for r in self._rows.values() if status is None or r["status"] == status)[:limit]

    def sweep(self) -> int:
        with self._mu:
            rows = [r for r in self._rows.values() if r["status"] in (QUEUED, RUNNING)]
            for r in rows:
                r.update(status=LOST, ended_at=_iso(_now()))
        return len(rows)

    def delete_thread(self, thread_id: str) -> None:
        with self._mu:
            self._rows = {k: r for k, r in self._rows.items() if r["thread_id"] != thread_id}


class _SqlRunStore:
    """两种库共用的 SQL；占位符写成 %s，SQLite 换成 ?。子类给 _exec（执行一条，返回字典行）。"""

    _COLS = ", ".join(FIELDS)
    _SEQ = "seq"  # 写入的先后

    def _exec(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def create(self, run_id: str, thread_id: str, kind: str, turn_id: str | None, version: str | None) -> None:
        self._exec("INSERT INTO runs (id, thread_id, kind, turn_id, status, created_at, version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                   (run_id, thread_id, kind, turn_id, QUEUED, self._time(_now()), version))

    def started(self, run_id: str) -> None:
        self._exec("UPDATE runs SET status = %s, started_at = %s WHERE id = %s", (RUNNING, self._time(_now()), run_id))

    def finish(self, run_id: str, status: str, error_type: str | None = None, error: str | None = None,
               traceback: str | None = None) -> None:
        self._exec("UPDATE runs SET status = %s, ended_at = %s, error_type = %s, error = %s, traceback = %s WHERE id = %s",
                   (status, self._time(_now()), error_type, error, traceback, run_id))

    def for_thread(self, thread_id: str) -> list[dict]:
        return self._rows(f"SELECT {self._COLS} FROM runs WHERE thread_id = %s ORDER BY {self._SEQ} DESC", (thread_id,))

    def recent(self, status: str | None, limit: int) -> list[dict]:
        if status is None:
            return self._rows(f"SELECT {self._COLS} FROM runs ORDER BY {self._SEQ} DESC LIMIT %s", (limit,))
        return self._rows(f"SELECT {self._COLS} FROM runs WHERE status = %s ORDER BY {self._SEQ} DESC LIMIT %s", (status, limit))

    def sweep(self) -> int:
        return len(self._exec("UPDATE runs SET status = %s, ended_at = %s WHERE status IN (%s, %s) RETURNING id",
                              (LOST, self._time(_now()), QUEUED, RUNNING)))

    def delete_thread(self, thread_id: str) -> None:
        self._exec("DELETE FROM runs WHERE thread_id = %s", (thread_id,))

    def _rows(self, sql: str, params: tuple) -> list[dict]:
        return [{k: _iso(v) if k.endswith("_at") else v for k, v in r.items()} for r in self._exec(sql, params)]

    @staticmethod
    def _time(t: datetime) -> Any:
        return t


_SQLITE_DDL = (
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL, turn_id TEXT, status TEXT NOT NULL,
        created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, error_type TEXT, error TEXT, traceback TEXT, version TEXT)""",
    "CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id)",
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status)",
)


class SqliteRunStore(_SqlRunStore):
    """一个连接加一把锁。时间存 ISO 字符串（UTC、毫秒）。先后用 rowid：新行总比现有的都大。"""

    _SEQ = "rowid"

    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        for sql in _SQLITE_DDL:
            self._conn.execute(sql)

    def _exec(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._mu:
            return [dict(r) for r in self._conn.execute(sql.replace("%s", "?"), params).fetchall()]

    @staticmethod
    def _time(t: datetime) -> Any:
        return _iso(t)


_POSTGRES_DDL = (
    """CREATE TABLE IF NOT EXISTS runs (
        id text PRIMARY KEY, seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        thread_id text NOT NULL, kind text NOT NULL, turn_id text, status text NOT NULL,
        created_at timestamptz NOT NULL, started_at timestamptz, ended_at timestamptz,
        error_type text, error text, traceback text, version text)""",
    "CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id, seq)",
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, seq)",
)


class PostgresRunStore(_SqlRunStore):
    """和检查点同一个库、各用各的小连接池（写法同 graph.open_postgres_checkpointer）。表没建的先建。"""

    def __init__(self, url: str) -> None:
        self.pool = ConnectionPool(url, min_size=1, max_size=2, timeout=10, open=True, check=ConnectionPool.check_connection,
                                   kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        try:
            with self.pool.connection() as conn:
                for sql in _POSTGRES_DDL:
                    conn.execute(sql)
        except BaseException:
            self.pool.close()
            raise

    def _exec(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.pool.connection() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []


def open_run_store(data_dir: Path) -> RunStore:
    """和 graph.open_checkpointer 同一个规则：配了 LINGDRAFT_DATABASE_URL 就存 Postgres，没配存数据目录下的 SQLite。"""
    if config.DATABASE_URL:
        return PostgresRunStore(config.DATABASE_URL)
    return SqliteRunStore(Path(data_dir) / "runs.sqlite")
