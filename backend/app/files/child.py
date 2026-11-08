"""解析的子进程（见 service.isolated_parse）：只导入 parse，不碰数据库、配置。

容器只有 512 MB：20 MB 的 xlsx 解压出来可能几百 MB。子进程把自己设成内存不够时最先被杀的（oom_score_adj），
被杀了父进程收不到结果，当“文件太复杂”；对话的进程不受影响。
"""

from __future__ import annotations

from multiprocessing.connection import Connection
from pathlib import Path


def run(path: str, name: str, conn: Connection) -> None:
    try:
        with open("/proc/self/oom_score_adj", "w") as f:  # 只有 Linux 有；调高自己不用权限
            f.write("1000")
    except OSError:
        pass
    from app.files.parse import Unreadable, parse_file

    try:
        conn.send(("ok", parse_file(Path(path), name)))
    except Unreadable as e:
        conn.send(("unreadable", e.code, e.message))
    except Exception as e:  # noqa: BLE001
        conn.send(("failed", f"{type(e).__name__}: {str(e)[:300]}"))
    finally:
        conn.close()
