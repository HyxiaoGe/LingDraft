// 海报场景：单栏对话 + 底部问题卡（调研场景是 workspace.jsx 的三栏工作台）
import React, { useEffect, useRef, useState } from "react";
import { createThread, resumeThread } from "./api.js";

const KIND = { headline: "标题", subhead: "副标题", offer: "优惠", date: "活动时间", cta: "行动号召", product_image: "产品图" };
const STATUS = { draft: "草稿", confirmed: "已确认", user_edited: "手工编辑" };
// 旧线程里还有 forbidden_words / no_numbers_in_copy 两项（现在合并成文案合规，由模型判断）
const CHECK_NAME = { fact_match: "事实比对", copy_compliance: "文案合规", overflow: "排版溢出", forbidden_words: "违禁词", no_numbers_in_copy: "文案数字" };
const DEFAULT_REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27";

let seq = 0;
const uid = () => ++seq;

export default function PosterApp() {
  const [messages, setMessages] = useState([]);
  const [state, setState] = useState(null);
  const [card, setCard] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [image, setImage] = useState(null);
  const chatRef = useRef(null);
  const fileRef = useRef(null);
  const threadRef = useRef(null); // 问题卡的回调在 state 更新前就建好了，thread_id 走 ref

  useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, card, busy]);

  useEffect(() => { document.title = state ? "黑五预热宣传海报" : "灵稿 · 宣传海报"; });

  const patchAi = (id, fn) => setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  // 一轮结束：记下状态，按 interrupt 类型弹问题卡
  const show = (id, st, extra) => {
    threadRef.current = st.thread_id;
    setState(st);
    patchAi(id, (m) => ({ ...m, stage: null, result: st, ...extra }));
    setCard(cardFor(st));
  };

  const onEvents = (id) => (ev, data) => {
    if (ev === "progress" && data.stage) patchAi(id, (m) => ({ ...m, stage: data.stage }));
    if (ev === "node") patchAi(id, (m) => ({ ...m, steps: [...m.steps, data] }));
    if (ev === "error") setError(data.message);
  };

  // 跑一轮（新建或 resume）：开一条助手消息，边收事件边更新，结束后按状态弹问题卡
  const run = async (fn, userText, qa) => {
    setBusy(true);
    setError(null);
    setCard(null);
    if (userText) setMessages((ms) => [...ms, { id: uid(), role: "user", text: userText }]);
    const id = uid();
    const started = Date.now();
    setMessages((ms) => [...ms, { id, role: "ai", stage: "正在准备…", steps: [], qa: qa || null, started }]);
    try {
      const st = await fn(onEvents(id));
      const elapsed = Math.round((Date.now() - started) / 1000);
      if (st) show(id, st, { elapsed });
      else patchAi(id, (m) => ({ ...m, stage: null, elapsed }));
    } catch (e) {
      setError(String(e.message || e));
      patchAi(id, (m) => ({ ...m, stage: null }));
    } finally {
      setBusy(false);
    }
  };

  const start = (text) => {
    threadRef.current = null;
    return run((cb) => createThread(text, image, cb), text);
  };
  const resume = (payload, userText, qa) => run((cb) => resumeThread(threadRef.current, payload, cb), userText, qa);

  // ---- 把 interrupt 变成问题卡 ----
  const cardFor = (st) => {
    const it = st.interrupt;
    if (!it) return null;
    if (it.type === "confirm_facts") return factsCard(it, resume);
    if (it.type === "choose_style") return styleCard(it, resume);
    if (it.type === "review") return reviewCard(it, st, resume);
    return null;
  };

  // 底部输入框：没开始或已导出 → 新建；review 中 → 原话交给后端由模型理解；其他中断 → 禁用
  const interruptType = state?.interrupt?.type;
  const finished = state && state.exported && !interruptType;
  const inputMode = !state || finished ? "new" : interruptType === "review" ? "review" : "locked";
  const onSend = (text) => {
    if (inputMode === "new") return start(text);
    if (inputMode === "review") return resume({ action: "instruct", text }, text);
  };

  return (
    <div className="app">
      <div className="topbar">
        <h1>{state ? "黑五预热宣传海报" : "灵稿 · 宣传海报"}</h1>
        {!state && !busy && <div className="scen"><a href="#">调研</a><button className="sel">海报</button></div>}
      </div>
      <div className="chat" ref={chatRef}>
        <div className="chat-inner">
          {messages.length === 0 && <Welcome onPick={(t) => start(t)} />}
          {messages.map((m) => (m.role === "user" ? <UserMsg key={m.id} text={m.text} />
            : <AiMsg key={m.id} m={m} onPickElement={(e) => setCard(elementCard(e, state, resume, setCard))} />))}
          {error && <div className="err">出错：{error}</div>}
        </div>
      </div>
      <div className="bottom">
        <div className="bottom-inner">
          {card && !busy && <QuestionCard card={card} onClose={() => setCard(null)} />}
          <InputBox
            mode={inputMode}
            busy={busy}
            hasCard={!!card}
            onSend={onSend}
            onReopen={() => state && setCard(cardFor(state))}
            image={image}
            onImage={() => fileRef.current?.click()}
          />
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => setImage(e.target.files?.[0] || null)} />
          <div className="footnote">内容由 AI 生成，价格、折扣、日期只来自你确认的事实</div>
        </div>
      </div>
    </div>
  );
}

// ---------------- 问题卡的构造 ----------------
// card = { title, options:[{label, hint, swatches, value}], customPlaceholder, skip, page:"1/5", onAnswer(value, isCustom) }

function factsCard(it, resume, answers = {}, idx = 0, history = []) {
  const fields = it.fields;
  if (idx >= fields.length) {
    const values = {};
    fields.forEach((f) => (values[f.key] = answers[f.key] ?? null));
    resume({ values }, null, history);
    return null;
  }
  const f = fields[idx];
  const options = [];
  const hint = f.source === "proposed" ? `按你刚才说的，原来是${f.previous ? `“${f.previous}”` : "空"}` : f.source === "inferred" ? "从需求中提取" : "当前值";
  if (f.value) options.push({ label: f.value, hint, value: f.value });
  const next = (v) => {
    const a = { ...answers, [f.key]: v };
    const h = [...history, { q: `${f.label}是什么？`, a: v || "（不填，海报上不出现）" }];
    return factsCard(it, resume, a, idx + 1, h);
  };
  return {
    title: `${f.label}是什么？${f.required ? "" : "（可选）"}`,
    page: `${idx + 1}/${fields.length}`,
    options,
    customPlaceholder: f.required ? "输入内容…" : "其他补充…",
    skip: f.required ? null : "跳过",
    requireAnswer: f.required,
    onAnswer: (v) => next(v),
    onSkip: () => next(null),
  };
}

function styleCard(it, resume) {
  return {
    title: "想要哪种视觉方向？",
    page: "1/1",
    options: it.options.map((o, i) => ({
      label: `${o.name}${i === 0 ? "（推荐）" : ""}`,
      hint: `${o.mood} · ${o.description}`,
      swatches: o.palette,
      value: o.id,
    })),
    customPlaceholder: "或描述你想要的风格，例如：复古胶片感，暖棕色",
    onAnswer: (v, isCustom) => {
      const o = it.options.find((x) => x.id === v);
      resume(isCustom ? { custom: v } : { style_id: v }, null, [{ q: "想要哪种视觉方向？", a: isCustom ? v : o?.name }]);
      return null;
    },
  };
}

function reviewCard(it, st, resume) {
  const editable = it.elements.filter((e) => e.editable_by_model);
  const manual = it.elements.filter((e) => !e.bound_fact && e.kind !== "product_image" && e.kind !== "background");
  return {
    title: it.reply || `第 ${it.version} 版怎么样？`,
    page: "1/1",
    options: [
      { label: "就这版，导出", hint: "PNG + HTML + 事实清单", value: "approve" },
      { label: "让模型改某一处", hint: "其余已确认内容不动", value: "edit" },
      { label: "我直接改文字", hint: "改过的内容模型以后不再碰", value: "manual" },
      { label: "修改优惠、日期等事实", hint: "回到事实确认，改完只重排不重写", value: "fact" },
      { label: "换个风格", hint: "只换配色和背景，文案不动", value: "style" },
    ],
    customPlaceholder: "或直接说要改什么，例如：标题更有紧迫感 / 折扣改成 6 折",
    onAnswer: (v, isCustom) => {
      if (isCustom) return (resume({ action: "instruct", text: v }, v), null); // 原话交给后端，由模型理解成改文案 / 改事实 / 换风格 / 导出
      if (v === "approve") return resume({ action: "approve" }, "就这版，导出"), null;
      if (v === "fact") return resume({ action: "change_fact" }, "修改事实"), null;
      if (v === "style") return styleChangeCard(st, resume);
      if (v === "edit") return pickElementCard(editable, "让模型改哪一处？", (e) => instructionCard(e, resume));
      if (v === "manual") return pickElementCard(manual, "直接改哪一处？", (e) => manualCard(e, resume));
      return null;
    },
  };
}

function pickElementCard(elements, title, next) {
  return {
    title,
    page: "1/2",
    options: elements.map((e) => ({ label: KIND[e.kind] || e.kind, hint: e.text, value: e.id, el: e })),
    onAnswer: (v) => next(elements.find((e) => e.id === v)),
  };
}

function elementCard(e, st, resume, setCard) {
  if (e.bound_fact) {
    return {
      title: `“${e.text}”来自已确认的事实`,
      page: "1/1",
      options: [{ label: "去修改事实", hint: "改完只重新排版，不重写文案", value: "fact" }],
      onAnswer: () => (resume({ action: "change_fact" }, "修改事实"), null),
    };
  }
  return {
    title: `${KIND[e.kind] || e.kind}：${e.text}`,
    page: "1/1",
    options: [
      ...(e.editable_by_model ? [{ label: "让模型改", hint: "描述怎么改", value: "edit" }] : []),
      { label: "我直接改文字", hint: "改过的内容模型以后不再碰", value: "manual" },
    ],
    onAnswer: (v) => (v === "edit" ? instructionCard(e, resume) : manualCard(e, resume)),
  };
}

function instructionCard(e, resume) {
  return {
    title: `${KIND[e.kind] || e.kind}怎么改？`,
    page: "2/2",
    options: [
      { label: "更有紧迫感", value: "更有紧迫感" },
      { label: "更短更有力", value: "更短更有力" },
      { label: "更高级克制", value: "语气更高级克制" },
    ],
    customPlaceholder: "其他要求…",
    onAnswer: (v) => (resume({ action: "edit", target: e.id, instruction: v }, `${KIND[e.kind]}：${v}`), null),
  };
}

function manualCard(e, resume) {
  return {
    title: `${KIND[e.kind] || e.kind}改成什么？`,
    page: "2/2",
    options: [],
    customPlaceholder: `当前：${e.text}`,
    requireAnswer: true,
    onAnswer: (v) => (resume({ action: "manual_edit", target: e.id, content: v }, `${KIND[e.kind]}改成：${v}`), null),
  };
}

function styleChangeCard(st, resume) {
  return {
    title: "换成什么风格？",
    page: "1/1",
    options: [{ label: "重新挑选", hint: "从几个方向里选", value: "__pick" }],
    customPlaceholder: "或描述新风格，例如：极简白底",
    onAnswer: (v, isCustom) => {
      resume(isCustom ? { action: "change_style", instruction: v } : { action: "change_style" }, isCustom ? `换风格：${v}` : "换个风格");
      return null;
    },
  };
}

// ---------------- 组件 ----------------

const INTRO = "我可以帮你做宣传海报。说一句需求就行，我会先把价格、折扣、日期这些关键事实列出来让你确认，再问视觉方向，然后出初稿；之后你改哪一处，其余已确认的内容我都不会动。";

function Welcome({ onPick }) {
  return (
    <div className="msg-ai">
      <AiHead />
      <div className="body">
        <p>{INTRO}</p>
        <p>试试：<a href="#poster" onClick={(e) => { e.preventDefault(); onPick(DEFAULT_REQUEST); }}>{DEFAULT_REQUEST}</a></p>
      </div>
    </div>
  );
}

function AiHead({ children }) {
  return <div className="head"><span className="avatar">灵</span><span className="name">灵稿</span>{children}</div>;
}

// 进度：一步做完一行
function stepLine(s) {
  if (s.node === "render" && s.version != null) return `已渲染第 ${s.version} 版 · ${s.note}`;
  if (s.node === "export" && s.exported) return `已导出第 ${s.exported.version} 版确认物包`;
  return null;
}

function UserMsg({ text }) {
  return <div className="msg-user"><div className="bubble">{text}</div></div>;
}

function AiMsg({ m, onPickElement }) {
  const [open, setOpen] = useState(true);
  const st = m.result;
  const it = st?.interrupt;
  const last = st?.versions?.length ? st.versions[st.versions.length - 1] : null;
  const [sel, setSel] = useState(null);
  const cur = st?.versions?.find((v) => v.n === sel) || last;
  const elements = it?.type === "review" ? it.elements : st?.elements;
  return (
    <div className="msg-ai">
      <AiHead>
        {m.elapsed != null && <span className="meta">已处理 {m.elapsed}s</span>}
      </AiHead>
      <div className="body">
        {m.qa && m.qa.length > 0 && (
          <>
            <div className="qa-toggle" onClick={() => setOpen(!open)}>✎ 向用户提问 {open ? "⌃" : "⌄"}</div>
            {open && <div className="qa-box">{m.qa.map((x, i) => <div className="a" key={i}><div className="q">{x.q}</div><div>{x.a}</div></div>)}</div>}
          </>
        )}
        {m.steps.map((s, i) => { const line = stepLine(s); return line && <div className="done-line" key={i}>{line}</div>; })}
        {m.steps.filter((s) => s.notes?.length).map((s, i) => <p key={i}>{s.notes.join("；")}</p>)}
        {m.stage && <div className="status"><span className="dots">{m.stage}</span><span className="sep">|</span><span>请稍候</span></div>}
        {st && !m.stage && <ResultBody st={st} it={it} cur={cur} sel={sel} setSel={setSel} elements={elements} onPickElement={onPickElement} />}
      </div>
    </div>
  );
}

function ResultBody({ st, it, cur, setSel, elements, onPickElement }) {
  if (it?.type === "confirm_facts") {
    const n = it.fields.filter((f) => f.value).length;
    return <p>{n > 0 ? `我从需求里提取到 ${n} 项关键事实，先逐项确认一下。没提到的不会编造。` : "需求里没有价格、折扣、日期这些关键事实，请逐项补充。"}</p>;
  }
  if (it?.type === "choose_style") return <p>事实已确认。我准备了 {it.options.length} 个视觉方向，选一个或者直接描述。</p>;
  if (it?.type === "review" || st.exported) {
    const failed = (it?.checks || st.checks || []).filter((c) => !c.passed);
    return (
      <>
        {it ? <p>第 {it.version} 版好了。点下面的元素可以单独改；优惠、时间这些事实是锁定的，改它们要走事实确认。</p>
            : <p>已导出第 {st.exported.version} 版。</p>}
        <div className="poster">
          {cur && <img src={cur.png_url + "?v=" + cur.n} alt={`v${cur.n}`} />}
          <div className="side">
            {st.versions.length > 1 && (
              <div className="vers">{st.versions.map((v) => <button key={v.n} className={v.n === cur.n ? "sel" : ""} onClick={() => setSel(v.n)} title={v.note}>v{v.n}</button>)}</div>
            )}
            {cur && <div className="vlabel">v{cur.n} · {cur.note}</div>}
            <div className="els">
              {elements.filter((e) => e.kind !== "background" && e.kind !== "product_image").map((e) => (
                <div className="el" key={e.id} onClick={() => it && onPickElement(e)}>
                  <span className="k">{KIND[e.kind] || e.kind}</span>
                  <span className="t">{e.text || "（空）"}</span>
                  {e.bound_fact ? <span className="chip fact">事实</span> : <span className={"chip " + e.status}>{STATUS[e.status]}</span>}
                </div>
              ))}
            </div>
            {failed.length > 0 ? <div className="checks bad">自动校验未通过：{failed.map((c) => `${CHECK_NAME[c.name] || c.name}：${c.detail}`).join("；")}</div>
              : it && <div className="checks">✓ 事实逐字一致 · ✓ 文案合规 · ✓ 无溢出</div>}
            {st.exported && (
              <div className="export-links">
                <a href={st.exported.png_url} target="_blank" rel="noreferrer">final.png</a>
                <a href={st.exported.html_url} target="_blank" rel="noreferrer">final.html</a>
                <a href={st.exported.brief_url} target="_blank" rel="noreferrer">brief.json</a>
                <a href={st.exported.zip_url}>打包下载</a>
              </div>
            )}
          </div>
        </div>
      </>
    );
  }
  return null;
}

function QuestionCard({ card, onClose }) {
  const [custom, setCustom] = useState("");
  const [sel, setSel] = useState(null);
  const [cur, setCur] = useState(card);
  useEffect(() => { setCur(card); setCustom(""); setSel(null); }, [card]);
  if (!cur) return null;
  const go = (next) => { setCustom(""); setSel(null); if (next) setCur(next); else onClose(); };
  const pick = (o) => go(cur.onAnswer(o.value, false));
  const submitCustom = () => { if (custom.trim()) go(cur.onAnswer(custom.trim(), true)); };
  return (
    <div className="qcard">
      <div className="qhead">
        <div className="qtitle">{cur.title}{cur.sub && <div className="qsub">{cur.sub}</div>}</div>
        <div className="pager">
          {cur.page && <><button disabled>‹</button><span>{cur.page}</span><button disabled>›</button></>}
          <button className="x" onClick={onClose} title="先收起，稍后可从输入框右侧重新打开">×</button>
        </div>
      </div>
      {/* 选项多时（比如几十条来源）在卡里滚动，卡片不会长出屏幕顶端 */}
      <div className="opts">
        {cur.options.map((o, i) => (
          <div className={"opt" + (sel === i ? " sel" : "")} key={i} onMouseEnter={() => setSel(i)} onClick={() => pick(o)}>
            <span className="num">{i + 1}</span>
            <span className="label">
              {o.swatches && <span className="sw">{o.swatches.map((c) => <i key={c} style={{ background: c }} />)}</span>}
              {o.label}{o.hint && <span className="hint">　{o.hint}</span>}
            </span>
            <span className="arrow">→</span>
          </div>
        ))}
      </div>
      {cur.customPlaceholder !== undefined && (
        <div className="custom">
          <span className="pen">✎</span>
          <input value={custom} placeholder={cur.customPlaceholder} autoFocus={cur.options.length === 0}
            onChange={(e) => setCustom(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submitCustom()} />
        </div>
      )}
      <div className="qfoot">
        {cur.skip && <button onClick={() => go(cur.onSkip())}>{cur.skip}</button>}
        {cur.customPlaceholder !== undefined && <button className="primary" disabled={!custom.trim()} onClick={submitCustom}>确定</button>}
      </div>
    </div>
  );
}

function InputBox({ mode, busy, hasCard, onSend, onReopen, image, onImage }) {
  const [text, setText] = useState("");
  const placeholder = mode === "new" ? "今天帮你做什么海报？说清商品、优惠、时间"
    : mode === "review" ? "对这一版说点什么，例如：标题更有紧迫感" : "请先回答上方的问题";
  const send = () => { if (text.trim() && !busy && mode !== "locked") { onSend(text.trim()); setText(""); } };
  return (
    <div className="inputbox">
      <textarea value={text} placeholder={placeholder} disabled={busy || mode === "locked"} rows={2}
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
      <div className="tools">
        <div className="left">
          <button className="tbtn" title="上传产品图" onClick={onImage} disabled={mode !== "new"}>＋ {image ? image.name : "产品图"}</button>
          <span className="tbtn">◎ 豆包 Seed 2.1</span>
        </div>
        <div className="right">
          {mode === "locked" && !hasCard && !busy && <button className="tbtn" onClick={onReopen}>重新打开问题</button>}
          <button className="send" disabled={busy || mode === "locked" || !text.trim()} onClick={send} title="发送">↑</button>
        </div>
      </div>
    </div>
  );
}
