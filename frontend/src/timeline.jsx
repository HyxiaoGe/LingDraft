// 对话。一轮是用户的一句话加上模型这一轮做的事：先是过程（思考、搜索、读页按发生的先后），然后是正文和这条回答用到的来源；
// 最后一轮下面是推荐问题。过程正在跑时展开，这一轮结束后收成一行“已完成 25 秒”，点开看每一步。
// 每句话和每条回答下面有时间，都能复制；最后一句在空闲时能改，改完按新的话重新回答。最后一轮的这些一直显示，之前的悬停才出现。
// 存下来的对话为准，正在跑的这一轮叠上实时片段。
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconArrowUp, IconCheck, IconChevron, IconCopy, IconEdit, IconLoop, IconPage, IconRetry, IconSearch, IconStop, IconWarn, Spinner } from "./icons.jsx";
import { Markdown } from "./markdown.jsx";
import { ACTIVE, QUEUED, answerText, citeNumbers, copyText, cutPartialCite, domain, elapsed, mergeItems, splitTurn, stamp, tidyTyping, typingUnit } from "./research.js";
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

  // 换了对话就到底；之后内容变高（字一个个出来、过程展开）或可视区变矮时，贴着底的跟到底。
  // 看的是尺寸变化，不靠这里重新渲染：逐字出现只重新渲染那一段，每帧也跟得上
  useLayoutEffect(() => {
    const el = scrollRef.current;
    stick.current = true;
    el.scrollTop = el.scrollHeight;
  }, [st?.thread_id]);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    const ro = new ResizeObserver(() => { if (stick.current) el.scrollTop = el.scrollHeight; });
    ro.observe(el);
    ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, []);

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
        {EXAMPLES.map((t) => <button key={t} className="ws-suggest-item" onClick={() => onPick(t)}>{t}</button>)}
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
  const { process, answer } = splitTurn(items);
  const nums = citeNumbers(items.filter((i) => i.type === "text").map((i) => i.text), st?.sources);
  const rec = t.record;
  const idle = !running && st?.status === "idle";
  const failed = last && !running && st?.status === "failed";
  const replied = answer.some((i) => i.type === "text");
  return (
    <>
      {t.user && <UserMsg text={live?.edit ?? t.user} at={live?.sentAt ?? t.sent_at} pinned={last} now={now} editable={last && idle} onEdit={act.edit} />}
      <div className="ws-ai">
        {(running || rec) && <Process items={process} st={st} nums={nums} rec={rec} running={running} failed={failed} now={now} />}
        {answer.map((it, k) => <Item key={`${it.type}:${it.id}`} it={it} st={st} nums={nums} running={running} lastItem={k === answer.length - 1} />)}
        {running && <LiveLine live={live} items={items} stopping={stopping} />}
        {!running && <AnswerSources nums={nums} sources={st?.sources} />}
        {!running && rec?.ended_at && rec.outcome === "no_reply" && <p className="ws-muted">这一轮没有给出文字回复。</p>}
        {failed && <Failed st={st} act={act} />}
        {!running && (replied || rec?.ended_at || (last && idle)) && (
          <div className={"ws-ai-foot" + (last ? "" : " quiet")}>
            <Stamp iso={rec?.ended_at} now={now} />
            {replied && <CopyButton text={() => answerText(items, st?.sources)} />}
            {last && idle && <button className="ws-btn ghost sm" onClick={act.regenerate}><IconLoop size={13} />重新回答</button>}
          </div>
        )}
      </div>
      {last && idle && rec?.suggestions?.length > 0 && <Suggestions items={rec.suggestions} onPick={act.say} />}
    </>
  );
}

// 用户说的话：下面是发送时间和“复制”，最后一句在空闲时还有“编辑”。pinned（最后一轮）一直显示，其他的悬停才出现
function UserMsg({ text, at, pinned, now, editable, onEdit }) {
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editable) setEditing(false); }, [editable]); // 开着编辑框时这一轮又跑起来了（点了重新回答、推荐问题），编辑框就收起
  if (editing && editable) return <EditBox text={text} onCancel={() => setEditing(false)} onSend={(t) => { setEditing(false); onEdit(t); }} />;
  return (
    <div className={"ws-user" + (pinned ? " pinned" : "")}>
      <div className="ws-bubble">{text}</div>
      <div className={"ws-user-foot" + (pinned ? " pinned" : "")}>
        <Stamp iso={at} now={now} />
        <CopyButton text={() => text} />
        {editable && <button className="ws-btn ghost sm" onClick={() => setEditing(true)}><IconEdit size={13} />编辑</button>}
      </div>
    </div>
  );
}

// 改最后一句：回车发送，Shift+回车换行，Esc 取消；没改动不能发
function EditBox({ text, onCancel, onSend }) {
  const [v, setV] = useState(text);
  const ref = useRef(null);
  useAutoHeight(ref, v);
  useEffect(() => {
    const el = ref.current;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  const changed = v.trim() && v.trim() !== text.trim();
  const send = () => changed && onSend(v.trim());
  return (
    <div className="ws-edit">
      <textarea ref={ref} rows={1} value={v} aria-label="改这句话" onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
          else if (e.key === "Escape") onCancel();
        }} />
      <div className="ws-edit-bar">
        <span className="hint">发送后按新的话重新回答，这一轮原来的回答会被替换</span>
        <button className="ws-btn ghost sm" onClick={onCancel}>取消</button>
        <button className="ws-btn primary sm" disabled={!changed} onClick={send}>发送</button>
      </div>
    </div>
  );
}

// 发送或回答完的时间，悬停看精确到秒
function Stamp({ iso, now }) {
  const s = stamp(iso, now);
  return s && <time className="ws-time" dateTime={iso} title={s.full}>{s.text}</time>;
}

// 复制：点了以后显示“已复制”一会儿。text 是函数，点的时候才算要复制的文字
function CopyButton({ text }) {
  const [done, setDone] = useState(null); // null / "ok" / "fail"
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await copyText(text());
      setDone("ok");
    } catch {
      setDone("fail");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setDone(null), 1500);
  };
  return (
    <button className="ws-btn ghost sm" onClick={copy} aria-live="polite">
      {done === "ok" ? <IconCheck size={13} /> : <IconCopy size={13} />}
      {done === "ok" ? "已复制" : done === "fail" ? "没复制成" : "复制"}
    </button>
  );
}

// 输入框跟着字数长高，最高 160px，再多就在框里滚
function useAutoHeight(ref, value) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [ref, value]);
}

// 这一轮的过程：正在跑时展开，结束后收成一行（已完成 / 已停止 / 已中断 / 没跑完，加上用时），点开看每一步
function Process({ items, st, nums, rec, running, failed, now }) {
  const [open, setOpen] = useState(null); // null：按默认（正在跑就展开）
  const shown = (open ?? running) && items.length > 0;
  const time = running ? elapsed(st?.running_since || rec?.started_at, now)
    : rec?.ended_at ? elapsed(rec.started_at, Date.parse(rec.ended_at)) : "";
  const verb = running ? "处理中" : failed ? "没跑完" : rec?.outcome === "stopped" ? "已停止" : rec?.outcome === "blocked" ? "已中断" : "已完成";
  const label = `${verb}${time ? ` ${time}` : ""}`;
  return (
    <div className="ws-proc">
      {items.length
        ? <button className="ws-proc-head" aria-expanded={shown} onClick={() => setOpen(!shown)}>{label}<IconChevron size={13} className={"chev" + (shown ? " up" : "")} /></button>
        : <div className="ws-proc-head">{label}</div>}
      {shown && (
        <div className="ws-proc-body">
          {items.map((it) => (it.type === "step" ? <ToolRow key={`step:${it.id}`} s={it} />
            : it.type === "thinking" ? <Thinking key={`thinking:${it.id}`} it={it} thinking={running && it.live && !it.ms} />
              : <ProcText key={`text:${it.id}`} it={it} cite={(sid) => <Mark n={nums[sid]} s={st?.sources?.[sid]} />} />))}
        </div>
      )}
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
  if (it.type === "blocked") {
    return (
      <div className="ws-card warn">
        <div className="ws-card-title"><IconWarn size={15} />内容审核没通过</div>
        <div className="ws-card-sub">{BLOCKED_TEXT[it.reason] || BLOCKED_TEXT.input}这一轮不会带进之后的对话。</div>
      </div>
    );
  }
  return <Reply it={it} cite={(sid) => <Mark n={nums[sid]} s={st?.sources?.[sid]} />} caret={running && lastItem} />;
}

// 正文：逐字出现，正在写的时候末尾有光标
function Reply({ it, cite, caret }) {
  const text = useTyped(`text:${it.id}`, it.text, it.live, typingUnit);
  return (
    <div className={"ws-reply" + (caret || text.length < it.text.length ? " typing" : "")}>
      <Markdown text={mdText(it, text)} cite={cite} />
    </div>
  );
}

// 过程里中途说的话（之后又去搜了、读了），一样逐字出现
function ProcText({ it, cite }) {
  const text = useTyped(`text:${it.id}`, it.text, it.live, typingUnit);
  return <div className="ws-proc-text"><Markdown text={mdText(it, text)} cite={cite} /></div>;
}

// 还在出现的，最后一行半截的 Markdown 先收拾一下（见 tidyTyping）；写完的只去掉末尾半个引用
const mdText = (it, text) => (it.live || text.length < it.text.length ? tidyTyping(text) : cutPartialCite(text));

// ---------------- 逐字出现 ----------------
// 后端约每 80ms 发来一批字（几个到十几个），直接显示是一跳一跳的。这里按帧把收到的字一点点放出来：
// 每秒放“还没显示的字数 / LAG_S”这么多，最少 MIN_CPS 个。收得快就放得快，不会越落越远；一批字也不会一下子全冒出来。
// 显示到第几个字按条目记在 typed 里：条目在过程和正文之间挪动、重新挂载时接着往下显示，不从头再来。
// 打开时就已经写完的（存下来的对话）直接整段显示。unit(text, n) 给出 n 所在的那一整块的末尾（见 typingUnit），整块一起出来。
const LAG_S = 0.18;
const MIN_CPS = 20;
const typed = new Map();

function useTyped(key, text, live, unit) {
  const pos = useRef(null);
  pos.current ??= live || typed.has(key) ? Math.min(typed.get(key) ?? 0, text.length) : text.length;
  const [, setShown] = useState(0);
  useEffect(() => {
    if (pos.current >= text.length) {
      if (!live) typed.delete(key);
      return undefined;
    }
    let raf = 0;
    let last = performance.now();
    const frame = (t) => {
      const left = text.length - pos.current;
      pos.current = Math.min(text.length, pos.current + (Math.max(MIN_CPS, left / LAG_S) * Math.max(0, t - last)) / 1000);
      if (unit) pos.current = Math.max(pos.current, unit(text, Math.floor(pos.current)));
      last = t;
      typed.set(key, pos.current);
      setShown(Math.floor(pos.current)); // 整数没变就不重新渲染
      if (pos.current < text.length) raf = requestAnimationFrame(frame);
      else if (!live) typed.delete(key);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [key, text, live]);
  let n = Math.floor(pos.current);
  if (n >= text.length) return text;
  if (/[\uD800-\uDBFF]/.test(text[n - 1] || "")) n++; // 不把占两个码元的字（emoji 之类）切成两半
  return text.slice(0, n);
}

// 思考：正在想的时候展开、逐字出现；想完折叠成一行，点开看原文
function Thinking({ it, thinking }) {
  const text = useTyped(`thinking:${it.id}`, it.text, it.live);
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
      {shown && <div ref={box} className="ws-think-text">{text}</div>}
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

// 正在跑的这一轮最下面一行：现在在做什么（思考、搜索、写正文时各自已经有显示，这里不重复）
function LiveLine({ live, items, stopping }) {
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
  useAutoHeight(ref, text);
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
