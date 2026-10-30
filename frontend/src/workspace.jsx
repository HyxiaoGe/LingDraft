// 对话页：左侧对话列表，右侧对话。
// 页面以服务器上的线程状态为准：对话和来源都从状态里取。执行中的事件（思考和正文的逐字片段、工具步骤）
// 叠在存下来的对话上实时显示，并提示该重新取一次状态了；所以刷新、换个页面打开、或者中途断线重连，看到的都一样。
import { useEffect, useRef, useState } from "react";
import { createResearchThread, followResearchThread, getResearchState, listResearchThreads, regenerateResearchThread, retryResearchThread, sayResearch,
  stopResearchThread } from "./api.js";
import { IconPlus } from "./icons.jsx";
import { ACTIVE, QUEUED, addLive, elapsed, itemKey, useNow, when } from "./research.js";
import { Conversation } from "./timeline.jsx";
import "./workspace.css";

const hashId = () => location.hash.match(/^#r\/([0-9a-f]{6,})$/)?.[1] || null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export default function Workspace() {
  const [threads, setThreads] = useState(null);
  const [tid, setTid] = useState(hashId);
  const [st, setSt] = useState(null);
  // 这个页面发起或接上的那次执行：{said, turnsAtStart, items, stage, title, stopping, hidden}
  const [live, setLive] = useState(null);
  const [busy, setBusyState] = useState(false);
  const [error, setError] = useState(null);
  const gen = useRef(0); // 每切一次线程加一：旧线程的连接和回调发现对不上，就不再改页面
  const tidRef = useRef(tid);
  const ctl = useRef(new AbortController()); // 当前线程的连接；切走时断开（服务器上的执行照常跑）
  const lastEvent = useRef(null); // 收到的最后一个事件 id，断线后凭它接着读
  const busyRef = useRef(false);
  const refetchAt = useRef(0);
  const refetchTimer = useRef(null);

  const setBusy = (b) => {
    busyRef.current = b;
    setBusyState(b);
  };

  const refreshList = () => listResearchThreads().then(setThreads).catch(() => {});
  const anyActive = busy || (threads || []).some((t) => ACTIVE.has(t.status));
  const now = useNow(anyActive);

  useEffect(() => {
    refreshList();
    if (tid) restore(tid, gen.current);
    const onHash = () => location.hash !== "#poster" && hashId() !== tidRef.current && select(hashId()); // 手动改了地址栏里的线程
    addEventListener("hashchange", onHash);
    return () => { // 切去别的页：断开连接，旧的回调不再改页面
      removeEventListener("hashchange", onHash);
      ctl.current.abort();
      ctl.current = new AbortController();
      gen.current += 1;
      clearTimeout(refetchTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const t = setInterval(refreshList, anyActive ? 4000 : 20000);
    return () => clearInterval(t);
  }, [anyActive]);
  const title = st?.title || live?.title;
  useEffect(() => { document.title = title ? `${title} · 灵稿` : tid ? "灵稿 · 对话" : "灵稿 · 新对话"; }, [title, tid]);

  // ---- 切换线程 ----

  const select = (id) => {
    if (id === tidRef.current && id) return;
    ctl.current.abort();
    ctl.current = new AbortController();
    gen.current += 1;
    tidRef.current = id;
    lastEvent.current = null;
    clearTimeout(refetchTimer.current);
    refetchAt.current = 0;
    setTid(id);
    setSt(null);
    setLive(null);
    setError(null);
    setBusy(false);
    history.replaceState(null, "", id ? `#r/${id}` : location.pathname);
    if (id) restore(id, gen.current);
  };

  // 打开一个线程：取状态；服务器上还在跑，就接上这次执行的事件流（从这次执行的头一个事件补发）
  async function restore(id, g) {
    try {
      const s = await getResearchState(id);
      if (g !== gen.current) return;
      setSt(s);
      if (ACTIVE.has(s.status)) {
        setLive({ items: [], turnsAtStart: s.turns.length, stage: s.status === "queued" ? QUEUED : null });
        await follow(id, g, []);
      }
    } catch (e) {
      if (g !== gen.current) return;
      setError(`打开失败：${e.message || e}`);
      if (e.status === 404) history.replaceState(null, "", location.pathname); // 线程确实不在了才清地址栏；网络抖动时留着，刷新还能再试
    }
  }

  // 接上服务器上这次执行：补发断线期间漏掉的事件，再接着收；连不上就隔一会儿重连
  async function follow(id, g, errs) {
    setBusy(true);
    for (let fails = 0; ;) {
      try {
        const s = await followResearchThread(id, lastEvent.current, onEvent(g, errs), ctl.current.signal);
        if (g !== gen.current) return;
        if (s && !ACTIVE.has(s.status)) return finish(g, s, errs);
        fails = 0; // 连得上，只是流又断了、或者执行刚开始：接着读
      } catch (e) {
        if (e.aborted || g !== gen.current) return;
        if (e.status === 404 || ++fails > 30) { // 线程不在了，或者几分钟都连不上；后端重启时代理回的 5xx 也按断线重连
          setError(`取回进度失败：${e.message || e}`);
          setBusy(false);
          setLive(null);
          return;
        }
        setLive((l) => ({ items: [], ...l, stage: "连接断了，正在重连…" }));
      }
      await sleep(fails ? Math.min(1000 * 2 ** (fails - 1), 10000) : 500);
    }
  }

  // 一次执行停下来了：以最终状态为准。执行中出过错但线程没算失败，把错误告诉用户
  function finish(g, s, errs) {
    if (g !== gen.current) return;
    clearTimeout(refetchTimer.current);
    refetchAt.current = 0;
    setSt(s);
    setBusy(false);
    setLive(null);
    if (errs.length && s.status !== "failed") setError(errs.at(-1));
    refreshList();
  }

  // 执行中重新取一次状态（新存下来的对话、来源）；几个事件挨着来时只取一次，最早要取的那次为准
  function refetch(g, ms) {
    const due = Date.now() + ms;
    if (refetchAt.current && refetchAt.current <= due) return;
    clearTimeout(refetchTimer.current);
    refetchAt.current = due;
    refetchTimer.current = setTimeout(async () => {
      refetchAt.current = 0;
      try {
        const s = await getResearchState(tidRef.current);
        if (g === gen.current && busyRef.current) setSt(s); // 执行已经结束的话，以 state 事件给的最终状态为准
      } catch { /* 下一个事件再取 */ }
    }, ms);
  }

  const onEvent = (g, errs) => (ev, data, id) => {
    if (g !== gen.current) return;
    if (id) lastEvent.current = id;
    if (ev === "thread") {
      if (tidRef.current !== data.thread_id) {
        tidRef.current = data.thread_id;
        setTid(data.thread_id);
        history.replaceState(null, "", `#r/${data.thread_id}`);
      }
      setLive((l) => (l ? { ...l, stage: data.queued ? QUEUED : l.stage } : l));
      refreshList();
      refetch(g, 0); // 用户的话已经存下来了
    } else if (ev === "started") {
      setLive((l) => (l ? { ...l, stage: null } : l));
      refreshList();
    } else if (ev === "title") {
      setLive((l) => (l ? { ...l, title: data.title } : l));
      refreshList();
    } else if (ev === "delta" || ev === "step") {
      setLive((l) => addLive(l && { ...l, stage: null }, ev, data));
      if (ev === "step" && !data.step.pending) refetch(g, 1500); // 新的来源
    } else if (ev === "node") {
      refetch(g, 150);
    } else if (ev === "error") {
      errs.push(data.message);
    }
  };

  // 跑一次（新建、接着说、重新回答或重试）：边收事件边显示，停下来以后以最终状态为准。
  // said：用户这次说的话，存下来之前先在对话里显示；extra：这次执行的其他显示信息（重新回答时要藏起来的旧条目）
  async function run(fn, said, extra = {}) {
    const g = gen.current;
    setBusy(true);
    setError(null);
    setLive({ items: [], said, turnsAtStart: st?.turns?.length || 0, stage: "正在准备…", ...extra });
    const errs = [];
    try {
      const s = await fn(onEvent(g, errs), ctl.current.signal);
      if (g !== gen.current) return;
      if (s && !ACTIVE.has(s.status)) return finish(g, s, errs);
      if (tidRef.current) return await follow(tidRef.current, g, errs); // 流没收完就断了
      setBusy(false);
    } catch (e) {
      if (e.aborted || g !== gen.current) return;
      // 话被拒（400）、正在跑或上一轮没跑完（409）、连接断了（没有状态码）——都回服务器接上当前进度
      const recoverable = tidRef.current && (!e.status || e.status === 400 || e.status === 409);
      if (e.status || !recoverable) setError(String(e.message || e));
      if (recoverable) {
        setLive((l) => ({ items: [], ...l, said: e.status ? null : l?.said, stage: e.status ? "服务器还在处理，等它完成" : "连接断了，正在重连…" }));
        return await follow(tidRef.current, g, errs);
      }
      setBusy(false);
      setLive(null);
    }
  }

  const act = {
    start: (text) => run((cb, sig) => createResearchThread(text, cb, sig), text),
    say: (text) => {
      const id = tidRef.current;
      return run((cb, sig) => sayResearch(id, text, cb, sig), text);
    },
    retry: () => run((cb, sig) => retryResearchThread(tidRef.current, cb, sig), null),
    // 最后一轮的旧回答先藏起来，新回答在同一轮里接着出现
    regenerate: () => {
      const hidden = new Set((st?.turns?.at(-1)?.items || []).map(itemKey));
      return run((cb, sig) => regenerateResearchThread(tidRef.current, cb, sig), null, { hidden });
    },
    // 只是提出停下：服务器在下一个字或下一步之前停，最终状态照常从这次执行的事件流里来
    stop: async () => {
      const g = gen.current;
      if (!tidRef.current) return; // 新对话刚发出去，服务器还没给线程 id：这时还没开始跑
      setLive((l) => (l ? { ...l, stopping: true } : l));
      try {
        await stopResearchThread(tidRef.current);
      } catch (e) {
        if (g !== gen.current || e.status === 409) return; // 409：这一轮刚好已经跑完
        setLive((l) => (l ? { ...l, stopping: false } : l));
        setError(`没停下来：${e.message || e}`);
      }
    },
  };

  return (
    <div className="ws">
      <Sidebar threads={threads} current={tid} st={st} live={live} busy={busy} now={now} onSelect={select} />
      <Conversation st={st} live={live} busy={busy} error={error} now={now} act={act} isNew={!tid} />
    </div>
  );
}

// ---------------- 左栏：对话列表 ----------------

const GROUPS = [
  ["active", "进行中", (t) => ACTIVE.has(t.status)],
  ["failed", "没跑完", (t) => t.status === "failed"],
  ["idle", "最近", (t) => t.status === "idle"],
];

function rowSub(t, now) {
  if (t.status === "queued") return "排队中";
  if (t.status === "running") return t.running_since ? `进行中 · ${elapsed(t.running_since, now)}` : "进行中";
  if (t.status === "failed") return "没跑完，可以重试";
  return "";
}

function Sidebar({ threads, current, st, live, busy, now, onSelect }) {
  // 列表每隔几秒取一次；当前这个线程以页面手上的状态为准，刚新建、列表里还没有的先补一行
  let rows = threads || [];
  if (current) {
    const mine = rows.find((t) => t.thread_id === current);
    const title = st?.title || live?.title;
    const patch = { ...(busy ? { status: "running", running_since: st?.running_since || mine?.running_since } : st ? { status: st.status } : {}),
      ...(title ? { title } : {}) };
    rows = mine ? rows.map((t) => (t.thread_id === current ? { ...t, ...patch } : t))
      : [{ thread_id: current, title: live?.said || "新对话", status: "running", ...patch }, ...rows];
  }
  return (
    <aside className="ws-side">
      <div className="ws-brand">
        <span className="logo">灵</span>
        <span className="name">灵稿</span>
        <span className="tag">对话</span>
      </div>
      <button className="ws-new" onClick={() => onSelect(null)} aria-current={!current ? "page" : undefined}>
        <IconPlus size={15} /> 新对话
      </button>
      <nav className="ws-groups" aria-label="对话列表">
        {threads === null && <div className="ws-side-empty">正在取对话列表…</div>}
        {threads !== null && rows.length === 0 && <div className="ws-side-empty">还没有对话</div>}
        {GROUPS.map(([key, name, test]) => {
          const items = rows.filter(test);
          if (!items.length) return null;
          return (
            <div className="ws-group" key={key}>
              <div className="ws-group-name">{name}<span>{items.length}</span></div>
              {items.map((t) => (
                <a key={t.thread_id} href={`#r/${t.thread_id}`} className={"ws-task" + (t.thread_id === current ? " sel" : "")}
                  aria-current={t.thread_id === current ? "page" : undefined}
                  onClick={(e) => { e.preventDefault(); onSelect(t.thread_id); }}>
                  <span className={"dot " + key} />
                  <span className="body">
                    <span className="title" title={t.title}>{t.title}</span>
                    {rowSub(t, now) && <span className="sub">{rowSub(t, now)}</span>}
                  </span>
                  <span className="time">{when(t.updated_at, now)}</span>
                </a>
              ))}
            </div>
          );
        })}
      </nav>
      <a className="ws-alt" href="#poster">宣传海报</a>
    </aside>
  );
}
