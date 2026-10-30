// 对话。一轮是用户的一句话加上模型这一轮做的事：思考、工具步骤按发生的先后穿插，然后是正文和这条回答用到的来源；
// 最后一轮下面是推荐问题。存下来的对话为准，正在跑的这一轮叠上实时片段。
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconArrowUp, IconChevron, IconLoop, IconPage, IconRetry, IconSearch, IconStop, IconWarn, Spinner } from "./icons.jsx";
import { Markdown } from "./markdown.jsx";
import { ACTIVE, QUEUED, citeNumbers, cutPartialCite, domain, elapsed, mergeItems } from "./research.js";
import { AnswerSources, Mark } from "./sources.jsx";

const EXAMPLES = [
  "深圳这周会不会降温？",
  "最近一周 AI 圈有什么新闻？",
  "珠穆朗玛峰现在的官方高度是多少？",
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

  const mode = fresh ? "new" : st?.status === "queued" ? "queued" : busy || ACTIVE.has(st?.status) ? "busy" : st?.status || "busy";
  const stopping = !!(live?.stopping || st?.stopping);
  const onSend = (text) => {
    if (mode === "new") return act.start(text);
    if (mode === "idle") return act.say(text);
    return undefined;
  };
  const title = st?.title || live?.title || live?.said || "新对话";

  return (
    <section className="ws-main" aria-label="对话">
      <header className="ws-head">
        <div className="ws-head-title" title={title}>{fresh ? "新对话" : title}</div>
      </header>
      <div className="ws-scroll" ref={scrollRef}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
        <div className="ws-flow">
          {fresh ? <Welcome onPick={act.start} /> : <Turns st={st} live={live} busy={busy} stopping={stopping} now={now} act={act} />}
          {error && <div className="ws-error" role="alert"><IconWarn size={15} /><span>{error}</span></div>}
        </div>
      </div>
      <Composer mode={mode} stopping={stopping} onSend={onSend} onStop={act.stop} />
    </section>
  );
}

function Welcome({ onPick }) {
  return (
    <div className="ws-welcome">
      <h2>想问什么？</h2>
      <p>直接问。需要查的会上网搜、读原文，回答里标出处，鼠标放在角标上能看到是哪条来源。</p>
      <div className="ws-examples">
        {EXAMPLES.map((t) => <button key={t} className="ws-example" onClick={() => onPick(t)}><span className="t">{t}</span></button>)}
      </div>
    </div>
  );
}

// ---------------- 一轮一轮 ----------------

function Turns({ st, live, busy, stopping, now, act }) {
  const turns = st?.turns || [];
  const running = busy || ACTIVE.has(st?.status);
  // 刚说的话还没出现在存下来的对话里：先按页面手上的显示
  const pending = live?.said && turns.length <= (live.turnsAtStart ?? 0) ? { id: "pending", user: live.said, items: [], record: null } : null;
  const all = pending ? [...turns, pending] : turns;
  return (
    <>
      {all.map((t, i) => {
        const last = i === all.length - 1;
        return <Turn key={t.id} t={t} st={st} live={last && running ? live : null} running={last && running} stopping={stopping} last={last} now={now} act={act} />;
      })}
    </>
  );
}

function Turn({ t, st, live, running, stopping, last, now, act }) {
  const items = mergeItems(t.items, live);
  const nums = citeNumbers(items.filter((i) => i.type === "text").map((i) => i.text), st?.sources);
  const rec = t.record;
  const idle = !running && st?.status === "idle";
  return (
    <>
      {t.user && <div className="ws-user"><div className="ws-bubble">{t.user}</div></div>}
      <AiMsg>
        {group(items).map((g, k, all) => (g.steps
          ? <div key={`steps:${g.steps[0].id}`} className="ws-tools">{g.steps.map((s) => <ToolRow key={s.id} s={s} />)}</div>
          : <Item key={`${g.type}:${g.id}`} it={g} st={st} nums={nums} running={running} lastItem={k === all.length - 1} />))}
        {running && <LiveLine live={live} items={items} st={st} stopping={stopping} now={now} />}
        {!running && <AnswerSources nums={nums} sources={st?.sources} />}
        {!running && rec?.ended_at && rec.outcome === "no_reply" && <p className="ws-muted">这一轮没有给出文字回复。</p>}
        {!running && rec?.outcome === "stopped" && <p className="ws-muted">已停止</p>}
        {last && !running && st?.status === "failed" && <Failed st={st} act={act} />}
        {last && idle && <div className="ws-ai-foot"><button className="ws-btn ghost sm" onClick={act.regenerate}><IconLoop size={13} />重新回答</button></div>}
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

// 被模型服务的内容审核拦下、这一轮停了：说明是固定的，原因来自后端（这一轮从哪里被拦）
const BLOCKED_TEXT = {
  input: "这句话没通过模型服务的内容审核，这一轮停在这里。换个说法再说一次。",
  material: "这一轮查到的资料或写出的内容没通过模型服务的内容审核，这一轮停在这里。可以换个问法再试。",
  output: "模型的回答没通过内容审核，被中断了，这一轮停在这里。可以换个问法再试。",
};

function Item({ it, st, nums, running, lastItem }) {
  if (it.type === "thinking") return <Thinking it={it} thinking={running && it.live && !it.ms} />;
  if (it.type === "blocked") {
    return (
      <div className="ws-card warn">
        <div className="ws-card-title"><IconWarn size={15} />内容审核没通过</div>
        <div className="ws-card-sub">{BLOCKED_TEXT[it.reason] || BLOCKED_TEXT.input}这一轮不会带进之后的对话。</div>
      </div>
    );
  }
  if (it.type === "step") return <ToolRow s={it} />;
  return (
    <div className={"ws-reply" + (running && lastItem ? " typing" : "")}>
      <Markdown text={cutPartialCite(it.text)} cite={(sid) => <Mark n={nums[sid]} s={st?.sources?.[sid]} />} />
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

function ToolRow({ s }) {
  const Icon = s.kind === "read" ? IconPage : IconSearch;
  let main, sub;
  let bad = !s.ok;
  if (s.kind === "read") {
    main = <>读 <a href={s.text} target="_blank" rel="noreferrer" title={s.text}>{s.title || domain(s.text)}</a></>;
    sub = s.pending ? "" : s.body ? "读到原文" : `没读成${s.message ? `：${s.message}` : ""}`;
    bad = !s.pending && !s.body;
  } else {
    main = <>搜索「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    sub = s.pending ? "" : s.ok ? `${s.hits} 条结果${s.message ? `（${s.message}）` : ""}` : `没搜成：${s.message || "出错了"}`;
  }
  return (
    <div className={"ws-tool" + (s.pending ? " pending" : bad ? " bad" : "")}>
      {s.pending ? <Spinner size={12} /> : <Icon size={12} />}
      <span className="main" title={s.text}>{main}</span>
      {sub && <span className="sub" title={sub}>{sub}</span>}
    </div>
  );
}

function LiveLine({ live, items, st, stopping, now }) {
  const lastItem = items.at(-1);
  let stage = stopping ? "正在停止…" : live?.stage;
  if (!stage) {
    if (lastItem?.type === "thinking" && lastItem.live && !lastItem.ms) stage = null; // 思考块自己有“正在思考”
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
      <div className="ws-card-sub">{st.error}。已经查到的内容都还在，重试会从断开的地方接着跑。</div>
      <div className="ws-card-actions"><button className="ws-btn primary" onClick={act.retry}><IconRetry size={14} />重试</button></div>
    </div>
  );
}

// ---------------- 输入框 ----------------

const PLACEHOLDER = {
  new: "想问什么？",
  idle: "接着问…",
  busy: "正在回答，可以随时停止",
  queued: QUEUED,
  failed: "这一轮没跑完，点上面的“重试”",
};

function Composer({ mode, stopping, onSend, onStop }) {
  const [text, setText] = useState("");
  const ref = useRef(null);
  const enabled = mode === "new" || mode === "idle";
  const canStop = mode === "busy" || mode === "queued";
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
          {canStop
            ? <button className="ws-send stop" aria-label={stopping ? "正在停止" : "停止"} title={stopping ? "正在停止" : "停止"} disabled={stopping} onClick={onStop}>
              {stopping ? <Spinner size={14} /> : <IconStop size={13} />}
            </button>
            : <button className="ws-send" aria-label="发送" disabled={!enabled || !text.trim()} onClick={send}><IconArrowUp size={15} /></button>}
        </div>
      </div>
    </div>
  );
}
