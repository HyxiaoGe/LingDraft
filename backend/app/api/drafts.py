"""边写边出现（2026-11-08 方案第 3 节）：模型调 create_file / edit_file 时参数是一段一段流出来的，边收边解出标题、类型、编号和内容，
跟正文一样按批推给页面（见 research_runner.StreamBuffer）。半截的不存：写成了页面换成那一版，没写成的过程里照实写（见 history.broken_write）。

参数是一个 JSON 对象。这里只认顶层的几个字符串字段，边收边解（转义、\\uXXXX、代理对都管）；别的值（edits 数组、数字）跳过。
每个字只过一遍，几万字的文档也不用每批从头解析。不是 JSON 对象（开头不是 {）的就不解了。
"""

from __future__ import annotations

from app.graph.research.history import WRITE_TOOLS

FIELDS = ("title", "type", "file_id", "change_note", "content")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}
_WS = " \t\r\n"


class PartialArgs:
    """边收边解一个 JSON 对象的顶层字符串字段。value(键) 是解到现在的值（还没收完的是半截），done 是收完了的键。
    值按字攒在列表里（几万字的内容一个字一个字接字符串是平方级的）。"""

    def __init__(self, keys: tuple[str, ...] = FIELDS) -> None:
        self.keys = set(keys)
        self._chars: dict[str, list[str]] = {}
        self.done: set[str] = set()
        self.broken = False
        self._state = "start"
        self._key = ""
        self._esc: str | None = None  # 转义到一半：反斜杠后面收到的字（\u 的话最多 5 个）
        self._hi: str | None = None  # 代理对的前一半
        self._depth = 0  # 跳过的数组、对象套了几层
        self._in_str = False  # 跳过的值里正在字符串里
        self._skip_esc = False

    def value(self, key: str, start: int = 0) -> str:
        return "".join(self._chars.get(key, [])[start:])

    def size(self, key: str) -> int:
        return len(self._chars.get(key, []))

    def feed(self, s: str) -> None:
        for ch in s:
            if self.broken:
                return
            self._step(ch)

    def _put(self, ch: str) -> None:
        if self._hi is not None:
            pair, self._hi = self._hi, None
            if "\udc00" <= ch <= "\udfff":
                ch = (pair + ch).encode("utf-16", "surrogatepass").decode("utf-16")
            else:
                self._put_str(pair)
        if "\ud800" <= ch <= "\udbff":
            self._hi = ch
            return
        self._put_str(ch)

    def _put_str(self, ch: str) -> None:
        if self._key in self.keys:
            self._chars[self._key].append(ch)

    def _string(self, ch: str) -> bool:
        """字符串里的一个字；收完了（碰到收尾的引号）返回 True。"""
        if self._esc is not None:
            self._esc += ch
            if self._esc[0] == "u":
                if len(self._esc) < 5:
                    return False
                try:
                    self._put(chr(int(self._esc[1:], 16)))
                except ValueError:
                    self._put_str("\\" + self._esc)
            else:
                self._put(_ESCAPES.get(self._esc, self._esc))
            self._esc = None
            return False
        if ch == "\\":
            self._esc = ""
            return False
        if ch == '"':
            if self._hi is not None:
                self._put_str(self._hi)
                self._hi = None
            return True
        self._put(ch)
        return False

    def _step(self, ch: str) -> None:
        st = self._state
        if st == "start":
            if ch == "{":
                self._state = "key_or_end"
            elif ch not in _WS:
                self.broken = True
        elif st == "key_or_end":
            if ch == '"':
                self._state, self._key = "key", ""
            elif ch == "}":
                self._state = "end"
            elif ch not in _WS + ",":
                self.broken = True
        elif st == "key":
            if self._esc is not None:
                self._key += ch
                self._esc = None
            elif ch == "\\":
                self._esc = ""
            elif ch == '"':
                self._state = "colon"
            else:
                self._key += ch
        elif st == "colon":
            if ch == ":":
                self._state = "value"
            elif ch not in _WS:
                self.broken = True
        elif st == "value":
            if ch in _WS:
                return
            if ch == '"':
                self._state = "string"
                if self._key in self.keys:
                    self._chars[self._key] = []
            elif ch in "[{":
                self._state, self._depth, self._in_str = "skip", 1, False
            else:
                self._state = "scalar"
        elif st == "string":
            if self._string(ch):
                if self._key in self.keys:
                    self.done.add(self._key)
                self._state = "after"
        elif st == "skip":
            if self._in_str:
                if self._skip_esc:
                    self._skip_esc = False
                elif ch == "\\":
                    self._skip_esc = True
                elif ch == '"':
                    self._in_str = False
            elif ch == '"':
                self._in_str = True
            elif ch in "[{":
                self._depth += 1
            elif ch in "]}":
                self._depth -= 1
                if self._depth == 0:
                    self._state = "after"
        elif st == "scalar":
            if ch == ",":
                self._state = "key_or_end"
            elif ch == "}":
                self._state = "end"
        elif st == "after":
            if ch == ",":
                self._state = "key_or_end"
            elif ch == "}":
                self._state = "end"
            elif ch not in _WS:
                self.broken = True


class Draft:
    """一次写文件调用流出来的参数。每批（take）给页面：头一次带工具名、所属消息；标题这些收完了才给一次；内容给这批新收到的。"""

    def __init__(self, call_id: str, name: str, mid: str) -> None:
        self.id, self.name, self.mid = call_id, name, mid
        self.args = PartialArgs()
        self._sent: set[str] = set()
        self._content = 0
        self._started = False

    def take(self) -> dict | None:
        out: dict = {}
        for k in FIELDS[:-1]:
            if k in self.args.done and k not in self._sent:
                out[k] = self.args.value(k)
                self._sent.add(k)
        if (n := self.args.size("content")) > self._content:
            out["text"] = self.args.value("content", self._content)
            self._content = n
        if not out and self._started:
            return None
        if not self._started:
            out |= {"name": self.name, "mid": self.mid}
            self._started = True
        return {"id": self.id, **out}


class DraftTracker:
    """一条条模型消息里写文件的工具调用片段 → Draft。片段按（消息 id，第几个调用）对上：编号、工具名多半只在第一个片段里。"""

    def __init__(self) -> None:
        self._calls: dict[tuple[str, int], dict] = {}
        self._dirty: list[Draft] = []

    def add(self, mid: str, chunks: list) -> None:
        for c in chunks:
            index = c.get("index")
            key = (mid, index if isinstance(index, int) else 0)
            st = self._calls.setdefault(key, {"id": "", "name": "", "draft": None, "pending": ""})
            st["id"] = st["id"] or c.get("id") or ""
            st["name"] = st["name"] or c.get("name") or ""
            args = c.get("args") or ""
            if st["draft"] is None:
                if st["name"] not in WRITE_TOOLS:
                    if st["name"]:  # 别的工具：参数不管
                        st["pending"] = ""
                        continue
                    st["pending"] += args  # 名字还没到（个别接口先给参数）：先攒着
                    continue
                st["draft"] = Draft(st["id"] or f"{mid}:{key[1]}", st["name"], mid)
                args, st["pending"] = st["pending"] + args, ""
            d: Draft = st["draft"]
            if args:
                d.args.feed(args)
            if d not in self._dirty:
                self._dirty.append(d)

    def take(self) -> list[dict]:
        out = [e for d in self._dirty if (e := d.take()) is not None]
        self._dirty = []
        return out

    def drop(self, mid: str) -> None:
        for key in [k for k in self._calls if k[0] == mid]:
            del self._calls[key]
