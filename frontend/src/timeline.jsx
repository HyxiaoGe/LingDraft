// 中栏：对话。一轮是用户的一句话加上模型这一轮做的事：思考、工具步骤、提问按发生的先后穿插，然后是正文；
// 这一轮改了报告就跟一张版本卡，最后一轮下面是推荐问题。存下来的对话为准，正在跑的这一轮叠上实时片段。
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconArrowUp, IconAsk, IconBook, IconChevron, IconPage, IconReport, IconRetry, IconSearch, IconWarn, Spinner } from "./icons.jsx";
import { Markdown } from "./markdown.jsx";
import { Mark } from "./report.jsx";
import { ACTIVE, QUEUED, citeNumbers, domain, elapsed, mergeItems, usageLine } from "./research.js";

const EXAMPLES = [
  ["简单问题", "新加坡现在是什么季节？"],
  ["需要调研", "DeepSeek V3 的训练成本到底是多少？不同说法差在哪？"],
];

export function Conversation({ st, live, busy, error, now, act, isNew }) {
  const scrollRef = useRef(null);
  const stick = useRef(true); // 用户往上翻看时不自动滚到底
  const fresh = isNew && !live;

  useEffect(() => { stick.current = true; }, [st?.thread_id]);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const waiting = !busy && st?.status === "waiting" && st?.interrupt;
  const mode = fresh ? "new" : st?.status === "queued" ? "queued" : busy || ACTIVE.has(st?.status) ? "busy" : waiting ? "ask" : st?.status || "busy";
  const onSend = (text) => {
    if (mode === "new") return act.start(text);
    if (mode === "ask") return act.answerAsk(text);
    if (mode === "idle") return act.say(text);
    return undefined;
  };
  const title = st?.title || live?.title || live?.said?.text || "新的调研";

  return (
    <section className="ws-main" aria-label="对话">
      <header className="ws-head">
        <div className="ws-head-title" title={title}>{fresh ? "新的调研" : title}</div>
      </header>
      <div className="ws-scroll" ref={scrollRef}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
        <div className="ws-flow">
          {fresh ? <Welcome onPick={act.start} /> : <Turns st={st} live={live} busy={busy} now={now} act={act} />}
          {error && <div className="ws-error" role="alert"><IconWarn size={15} /><span>{error}</span></div>}
        </div>
      </div>
      <Composer mode={mode} onSend={onSend} />
    </section>
  );
}

function Welcome({ onPick }) {
  return (
    <div className="ws-welcome">
      <h2>想研究什么？</h2>
      <p>直接问。简单问题直接回答；需要查证的，边查边给你看过程，再写成一份带来源的报告。</p>
      <p>报告里每句话都能回指来源，事实和推断分开标。看完可以接着问、要求补查，或者在报告上选中一句说怎么改。</p>
      <div className="ws-examples">
        {EXAMPLES.map(([k, t]) => (
          <button key={t} className="ws-example" onClick={() => onPick(t)}>
            <span className="k">{k}</span><span className="t">{t}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------- 一轮一轮 ----------------

function Turns({ st, live, busy, now, act }) {
  const turns = st?.turns || [];
  const running = busy || ACTIVE.has(st?.status);
  // 刚说的话还没出现在存下来的对话里：先按页面手上的显示
  const pending = live?.said && turns.length <= (live.turnsAtStart ?? 0) ? { id: "pending", user: live.said, items: [], record: null } : null;
  const all = pending ? [...turns, pending] : turns;
  return (
    <>
      {all.map((t, i) => {
        const last = i === all.length - 1;
        return <Turn key={t.id} t={t} st={st} live={last && running ? live : null} running={last && running} last={last} now={now} act={act} />;
      })}
    </>
  );
}

function Turn({ t, st, live, running, last, now, act }) {
  const items = mergeItems(t.items, live);
  const nums = citeNumbers(items.filter((i) => i.type === "text").map((i) => i.text));
  const rec = t.record;
  const idle = !running && st?.status === "idle";
  return (
    <>
      <UserMsg said={t.user} st={st} />
      <AiMsg>
        {group(items).map((g, k, all) => (g.steps
          ? <div key={`steps:${g.steps[0].id}`} className="ws-tools">{g.steps.map((s) => <ToolRow key={s.id} s={s} />)}</div>
          : <Item key={`${g.type}:${g.id}`} it={g} st={st} nums={nums} running={running} lastItem={k === all.length - 1} act={act} />))}
        {running && <LiveLine live={live} items={items} st={st} now={now} />}
        {rec?.version && <VersionCard n={rec.version} st={st} onOpen={act.openReport} />}
        {!running && rec?.ended_at && rec.outcome === "no_reply" && <p className="ws-muted">这一轮没有给出文字回复。</p>}
        {last && !running && st?.status === "failed" && <Failed st={st} act={act} />}
        {rec?.ended_at && usageLine(rec.usage) && <div className="ws-usage">{usageLine(rec.usage)}</div>}
      </AiMsg>
      {last && idle && rec?.suggestions?.length > 0 && <Suggestions items={rec.suggestions} onPick={act.say} />}
    </>
  );
}

// 挨着的工具步骤排成一列
function group(items) {
  const out = [];
  for (const it of items) {
    if (it.type === "step" && out.at(-1)?.steps) out.at(-1).steps.push(it);
    else out.push(it.type === "step" ? { steps: [it] } : it);
  }
  return out;
}

const ACTION_TEXT = {
  exclude_source: (a, st) => `排除来源「${st?.sources?.[a.source_id]?.title || domain(st?.sources?.[a.source_id]?.url || "") || a.source_id}」`,
  edit_claim: (a) => `把这条论断改成：${a.statement}`,
  rewrite_section: (a, st) => `重写「${st?.report?.outline?.find((n) => n.id === a.section_id)?.title || "这一节"}」`,
};

function UserMsg({ said, st }) {
  if (!said) return null;
  const action = said.action ? ACTION_TEXT[said.action.type]?.(said.action, st) : null;
  return (
    <div className="ws-user">
      <div className="ws-bubble">
        {said.quote && <div className="ws-bubble-quote">「{said.quote.text.length > 80 ? said.quote.text.slice(0, 80) + "…" : said.quote.text}」</div>}
        {action && <div className="ws-bubble-act">{action}</div>}
        {said.text}
      </div>
    </div>
  );
}

function AiMsg({ children }) {
  const kids = React.Children.toArray(children).filter(Boolean);
  if (!kids.length) return null;
  return (
    <div className="ws-ai">
      <div className="ws-ai-head"><span className="ws-avatar">灵</span><span className="name">灵稿</span></div>
      <div className="ws-ai-body">{kids}</div>
    </div>
  );
}

function Item({ it, st, nums, running, lastItem, act }) {
  if (it.type === "thinking") return <Thinking it={it} thinking={running && it.live && !it.ms} />;
  if (it.type === "step") return <ToolRow s={it} />;
  if (it.type === "ask") return <AskItem it={it} st={st} running={running} act={act} />;
  return (
    <div className={"ws-reply" + (running && lastItem ? " typing" : "")}>
      <Markdown text={it.text} cite={(sid) => <Cite sid={sid} n={nums[sid]} st={st} canEdit={!running && st?.status === "idle"} act={act} />} />
    </div>
  );
}

// 思考：正在想的时候展开、逐字出现；想完折叠成一行，点开看原文
function Thinking({ it, thinking }) {
  const [open, setOpen] = useState(null); // null：按默认（正在想就展开）
  const shown = open ?? thinking;
  const box = useRef(null);
  useLayoutEffect(() => { // 正在想的时候框里跟着最新的字
    if (thinking && box.current) box.current.scrollTop = box.current.scrollHeight;
  });
  const secs = it.ms ? Math.max(1, Math.round(it.ms / 1000)) : null;
  const label = thinking ? "正在思考" : secs ? `思考了 ${secs} 秒` : "思考过程";
  return (
    <div className={"ws-think" + (thinking ? " live" : "")}>
      <button className="ws-think-head" aria-expanded={shown} onClick={() => setOpen(!shown)}>
        {thinking ? <Spinner size={12} /> : <IconChevron size={13} className={"chev" + (shown ? " up" : "")} />}
        <span>{label}</span>
      </button>
      {shown && <div ref={box} className="ws-think-text">{it.text}</div>}
    </div>
  );
}

const TOOL_ICON = { search: IconSearch, read: IconPage, skill: IconBook, report: IconReport };

function ToolRow({ s }) {
  const Icon = TOOL_ICON[s.kind] || IconSearch;
  let main, sub;
  let bad = !s.ok;
  if (s.kind === "search") {
    main = <>搜索「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    sub = s.pending ? "" : s.ok ? `${s.hits} 条结果` : `没搜成：${s.message || "出错了"}`;
  } else if (s.kind === "read") {
    main = <>读 <a href={s.text} target="_blank" rel="noreferrer" title={s.text}>{s.title || domain(s.text)}</a></>;
    sub = s.pending ? "" : s.body ? "读到原文" : `没读成${s.message ? `：${s.message}` : ""}`;
    bad = !s.pending && !s.body;
  } else if (s.kind === "skill") {
    main = <>用调研方法「{s.text}」</>;
  } else {
    main = <>{s.pending ? "正在改报告" : "改报告"}{s.text ? `：${s.text}` : ""}</>;
    sub = s.pending ? "" : s.ok ? s.message : `没改成：${s.message}`;
  }
  return (
    <div className={"ws-tool" + (s.pending ? " pending" : bad ? " bad" : "")}>
      {s.pending ? <Spinner size={12} /> : <Icon size={12} />}
      <span className="main" title={s.text}>{main}</span>
      {sub && <span className="sub" title={sub}>{sub}</span>}
    </div>
  );
}

// 提问：在等回答时是一张卡，答过的留成一问一答
function AskItem({ it, st, running, act }) {
  const live = it.pending && !running && st?.status === "waiting" && st.interrupt;
  if (live) return <AskCard it={st.interrupt} onAnswer={act.answerAsk} />;
  return (
    <div className="ws-asked">
      <div className="q"><IconAsk size={13} />问你：{it.question}</div>
      <div className={"a" + (it.answer ? "" : " skipped")}>{it.pending ? "等你回答" : it.answer ? `你答：${it.answer}` : "你跳过了，按最可能的理解继续"}</div>
    </div>
  );
}

// 正文里的引用角标：这一轮按出现先后编号，悬停看来源。不在这条对话来源里的 id 标成问号
function Cite({ sid, n, st, canEdit, act }) {
  const s = st?.sources?.[sid];
  if (!s) return <span className="ws-mark off" title="这个来源不在搜索、读页的结果里">?</span>;
  return <Mark n={n} s={s} st={st} canEdit={canEdit} act={act} />;
}

function LiveLine({ live, items, st, now }) {
  const lastItem = items.at(-1);
  let stage = live?.stage;
  if (!stage) {
    if (live?.writing) stage = `正在写报告 · 已写 ${live.writing.toLocaleString()} 字`;
    else if (lastItem?.type === "thinking" && lastItem.live && !lastItem.ms) stage = null; // 思考块自己有“正在思考”
    else if (lastItem?.type === "step" && lastItem.pending) stage = null;
    else if (lastItem?.type === "text" && lastItem.live) stage = null;
    else stage = "正在处理";
  }
  return (
    <div className="ws-live" role="status">
      {stage && <><Spinner size={14} /><span className="stage">{stage}</span></>}
      {st?.running_since && <span className="time">已用 {elapsed(st.running_since, now)}</span>}
      <span className="hint">可以关掉页面，回来接着看</span>
    </div>
  );
}

function VersionCard({ n, st, onOpen }) {
  const v = st?.versions?.find((x) => x.n === n);
  const latest = st?.versions?.length || 0;
  return (
    <button className="ws-vcard" onClick={onOpen}>
      <span className="icon"><IconReport size={16} /></span>
      <span className="body">
        <span className="t">调研报告 · 第 {n} 版{latest > n ? <span className="old">（现在是第 {latest} 版）</span> : null}</span>
        {v?.note && <span className="d">{v.note}</span>}
      </span>
      <span className="go">在右边看</span>
    </button>
  );
}

function Suggestions({ items, onPick }) {
  return (
    <div className="ws-suggest" aria-label="可以接着问">
      {items.map((q) => <button key={q} className="ws-suggest-item" onClick={() => onPick(q)}>{q}</button>)}
    </div>
  );
}

function Failed({ st, act }) {
  return (
    <div className="ws-card warn">
      <div className="ws-card-title"><IconWarn size={15} />这一轮没跑完</div>
      <div className="ws-card-sub">{st.error}。已经查到的内容和之前的报告都还在，重试会从断开的地方接着跑。</div>
      <div className="ws-card-actions"><button className="ws-btn primary" onClick={act.retry}><IconRetry size={14} />重试</button></div>
    </div>
  );
}

// ---------------- 提问卡 ----------------

function AskCard({ it, onAnswer }) {
  const [text, setText] = useState("");
  const send = () => text.trim() && onAnswer(text.trim());
  return (
    <div className="ws-card ask">
      <div className="ws-card-title"><IconAsk size={15} />{it.title}</div>
      {it.why && <div className="ws-card-sub">为什么问：{it.why}</div>}
      {it.options?.length > 0 && (
        <div className="ws-choices">
          {it.options.map((o) => <button key={o} className="ws-chip" onClick={() => onAnswer(o)}>{o}</button>)}
        </div>
      )}
      <div className="ws-card-input">
        <input value={text} placeholder="或者自己回答…" aria-label="自己回答"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()} />
        <button className="ws-btn primary" disabled={!text.trim()} onClick={send}>回答</button>
      </div>
      <div className="ws-card-foot">
        <span>说不清楚也没关系，会按最可能的理解继续，并写明假设</span>
        <button className="ws-btn ghost" onClick={() => onAnswer(null)}>跳过</button>
      </div>
    </div>
  );
}

// ---------------- 输入框 ----------------

const PLACEHOLDER = {
  new: "想研究什么？",
  idle: "接着问，或者说说报告哪里要改",
  ask: "直接回答上面的问题…",
  busy: "正在处理，稍等…",
  queued: QUEUED,
  failed: "这一轮没跑完，点上面的“重试”",
};

function Composer({ mode, onSend }) {
  const [text, setText] = useState("");
  const ref = useRef(null);
  const enabled = mode === "new" || mode === "idle" || mode === "ask";
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [text]);
  const send = () => {
    const t = text.trim();
    if (!t || !enabled) return;
    onSend(t);
    setText("");
  };
  return (
    <div className="ws-composer">
      <div className={"ws-input" + (enabled ? "" : " off")}>
        <textarea ref={ref} rows={1} value={text} disabled={!enabled} placeholder={PLACEHOLDER[mode] || PLACEHOLDER.busy} aria-label="输入"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
        <div className="ws-input-bar">
          <span className="tag"><IconSearch size={12} />联网搜索 · 读原文</span>
          <button className="ws-send" aria-label="发送" disabled={!enabled || !text.trim()} onClick={send}><IconArrowUp size={15} /></button>
        </div>
      </div>
    </div>
  );
}
