// 调研工作台：左侧任务列表，中间对话与过程，右侧交付面板。
// 页面以服务器上的线程状态为准：过程记录、来源、报告都从状态里取。执行中的事件只用来实时显示正在做的那一步，
// 并提示该重新取一次状态了；所以刷新、换个页面打开、或者中途断线重连，看到的都一样。
import { useEffect, useRef, useState } from "react";
import { createResearchThread, followResearchThread, getResearchState, listResearchThreads, resumeResearchThread, retryResearchThread } from "./api.js";
import { IconPlus } from "./icons.jsx";
import { Deliverable } from "./report.jsx";
import { ACTIVE, QUEUED, elapsed, useNow, when } from "./research.js";
import { Conversation } from "./timeline.jsx";
import "./workspace.css";

const hashId = () => location.hash.match(/^#r\/([0-9a-f]{6,})$/)?.[1] || null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 同一次工具调用先来“开始”、再来“做完”，按 id 合并
const upsert = (acts, a) => {
  const i = acts.findIndex((x) => x.id === a.id);
  if (i < 0) return [...acts, a];
  const next = acts.slice();
  next[i] = { ...next[i], ...a };
  return next;
};

export default function Workspace() {
  const [threads, setThreads] = useState(null);
  const [tid, setTid] = useState(hashId);
  const [st, setSt] = useState(null);
  const [live, setLive] = useState(null); // 这个页面发起或接上的那次执行：{stage, acts, said, versionsAtStart, ended}
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
    return () => { // 切去海报页：断开连接，旧的回调不再改页面
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
  useEffect(() => { document.title = st?.scope?.question || (tid ? "灵稿 · 调研" : "灵稿 · 新的调研"); });

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

  // 打开一个线程：取状态；服务器上还在跑，就接上这次执行的事件流
  async function restore(id, g) {
    try {
      const s = await getResearchState(id);
      if (g !== gen.current) return;
      setSt(s);
      if (ACTIVE.has(s.status)) {
        setLive({ stage: s.status === "queued" ? QUEUED : "服务器还在处理这一步", acts: [] });
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
        setLive((l) => ({ acts: [], ...l, stage: "连接断了，正在重连…" }));
      }
      await sleep(fails ? Math.min(1000 * 2 ** (fails - 1), 10000) : 500);
    }
  }

  // 一次执行停下来了：以最终状态为准。执行中出过错但线程没算失败（比如回答提交后那一步出错，卡还在），把错误告诉用户
  function finish(g, s, errs) {
    if (g !== gen.current) return;
    clearTimeout(refetchTimer.current);
    refetchAt.current = 0;
    setSt(s);
    setBusy(false);
    setLive((l) => (l ? { ...l, stage: null, acts: [], ended: true } : null));
    if (errs.length && s.status !== "failed") setError(s.status === "waiting" ? `${errs.at(-1)}（这次的回答没用上，请重新回答）` : errs.at(-1));
    refreshList();
  }

  // 执行中重新取一次状态（新存下来的过程、来源、报告）；几个事件挨着来时只取一次，最早要取的那次为准
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
      if (data.queued) setLive((l) => ({ acts: [], ...l, stage: QUEUED }));
      refreshList();
    } else if (ev === "started") {
      setLive((l) => (l?.stage === QUEUED ? { ...l, stage: "正在准备…" } : l));
      refreshList();
    } else if (ev === "progress") {
      setLive((l) => {
        if (!l) return l;
        const acts = data.kind && data.id ? upsert(l.acts, data) : l.acts;
        return { ...l, acts, stage: data.done ? l.stage : data.stage || l.stage };
      });
      if (data.done) refetch(g, 1500);
    } else if (ev === "node") {
      refetch(g, 150);
    } else if (ev === "error") {
      errs.push(data.message);
    }
  };

  // 跑一次（新建、回答、修改或重试）：边收事件边显示，停下来以后以最终状态为准。
  // said：用户这次说的话，在对话里先显示出来；request：新建时的需求（线程状态取到之前先显示它）
  async function run(fn, said, request) {
    const g = gen.current;
    setBusy(true);
    setError(null);
    setLive({ stage: "正在准备…", acts: [], said, request, versionsAtStart: st?.versions?.length || 0 });
    const errs = [];
    try {
      const s = await fn(onEvent(g, errs), ctl.current.signal);
      if (g !== gen.current) return;
      if (s && !ACTIVE.has(s.status)) return finish(g, s, errs);
      if (tidRef.current) return await follow(tidRef.current, g, errs); // 流没收完就断了
      setBusy(false);
    } catch (e) {
      if (e.aborted || g !== gen.current) return;
      // 回答被拒（400）、卡已过期或这一步还在处理（409）、连接断了（没有状态码）——都回服务器接上当前进度
      const recoverable = tidRef.current && (!e.status || e.status === 400 || e.status === 409);
      if (e.status || !recoverable) setError(String(e.message || e));
      if (recoverable) {
        setLive((l) => ({ acts: [], ...l, said: e.status ? null : l?.said, stage: e.status ? "服务器还在处理这一步，等它完成" : "连接断了，正在重连…" }));
        return await follow(tidRef.current, g, errs);
      }
      setBusy(false);
      setLive(null);
    }
  }

  // 回答绑定到当前这张卡：线程和中断 id 都取自出卡时的状态；卡过期了后端会拒（409），不会改到别的卡上
  const resume = (payload, said) => {
    const s = st;
    return run((cb, sig) => resumeResearchThread(s.thread_id, s.interrupt?.id, payload, cb, sig), said || null);
  };
  const act = {
    start: (text) => run((cb, sig) => createResearchThread(text, cb, sig), null, text),
    answerScope: (answers) => resume({ answers }),
    answerAsk: (answer) => resume(answer == null ? { skip: true } : { answer }),
    approve: () => resume({ action: "approve" }, "不用改了，就用这版"),
    exclude: (sid) => {
      const src = st?.sources?.find((x) => x.id === sid); // 和版本说明的写法一致：写标题，不写每版会变的编号
      return resume({ action: "exclude_source", target: sid }, `排除来源「${src?.title || src?.url || sid}」`);
    },
    editClaim: (kid, content) => resume({ action: "edit_claim", target: kid, content }, `把这条论断改成：${content}`),
    rewrite: (sec, instruction) =>
      resume({ action: "rewrite_section", target: sec.id, ...(instruction ? { instruction } : {}) }, `重写「${sec.title}」${instruction ? `：${instruction}` : ""}`),
    instruct: (text, quote) => resume({ action: "instruct", text, ...(quote ? { quote } : {}) }, quote ? `「${quote.text}」${text}` : text),
    retry: () => run((cb, sig) => retryResearchThread(tidRef.current, cb, sig), null),
  };

  return (
    <div className="ws">
      <Sidebar threads={threads} current={tid} st={st} live={live} busy={busy} now={now} onSelect={select} />
      <Conversation st={st} live={live} busy={busy} error={error} now={now} act={act} isNew={!tid} />
      <Deliverable st={st} live={live} busy={busy} act={act} isNew={!tid} />
    </div>
  );
}

// ---------------- 左栏：任务列表 ----------------

const GROUPS = [
  ["active", "进行中", (t) => ACTIVE.has(t.status)],
  ["answer", "等你回答", (t) => t.status === "waiting" && t.waiting !== "review"],
  ["review", "等你确认", (t) => t.status === "waiting" && t.waiting === "review"],
  ["failed", "没跑完", (t) => t.status === "failed"],
  ["done", "已完成", (t) => t.status === "done"],
];

function rowSub(t, now) {
  if (t.status === "queued") return "排队中";
  if (t.status === "running") return t.running_since ? `进行中 · ${elapsed(t.running_since, now)}` : "进行中";
  if (t.status === "failed") return "没跑完，可以重试";
  if (t.status === "done") return t.version ? `第 ${t.version} 版 · 已完成` : "已结束";
  if (t.waiting === "scope") return t.questions ? `开始前有 ${t.questions} 个问题` : "确认范围后开始";
  if (t.waiting === "ask") return "调研中途有个问题";
  return `第 ${t.version} 版报告 · 待确认`;
}

function Sidebar({ threads, current, st, live, busy, now, onSelect }) {
  // 列表每隔几秒取一次；当前这个线程以页面手上的状态为准，刚新建、列表里还没有的先补一行
  let rows = threads || [];
  if (current) {
    const mine = rows.find((t) => t.thread_id === current);
    const patch = busy ? { status: "running", running_since: st?.running_since || mine?.running_since } : st ? { status: st.status } : {};
    rows = mine ? rows.map((t) => (t.thread_id === current ? { ...t, ...patch } : t))
      : [{ thread_id: current, title: st?.scope?.question || live?.request || "新的调研", status: "running", ...patch }, ...rows];
  }
  return (
    <aside className="ws-side">
      <div className="ws-brand">
        <span className="logo">灵</span>
        <span className="name">灵稿</span>
        <span className="tag">调研</span>
      </div>
      <button className="ws-new" onClick={() => onSelect(null)} aria-current={!current ? "page" : undefined}>
        <IconPlus size={15} /> 新建调研
      </button>
      <nav className="ws-groups" aria-label="调研任务">
        {threads === null && <div className="ws-side-empty">正在取任务列表…</div>}
        {threads !== null && rows.length === 0 && <div className="ws-side-empty">还没有调研任务</div>}
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
                    <span className="sub">{rowSub(t, now)}</span>
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
