import React, { useEffect, useRef, useState } from "react";
import { createThread, resumeThread, createResearchThread, resumeResearchThread, retryResearchThread, followResearchThread, getResearchState } from "./api.js";
import { RESEARCH_DEFAULT, ResearchBody, researchCardFor, retryCard, routeResearchText, scopeLine } from "./research.jsx";

const KIND = { headline: "标题", subhead: "副标题", offer: "优惠", date: "活动时间", cta: "行动号召", product_image: "产品图" };
const STATUS = { draft: "草稿", confirmed: "已确认", user_edited: "手工编辑" };
const DEFAULT_REQUEST = "某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27";
const QUEUED = "排队中，前面的任务跑完就开始";
const ACTIVE = new Set(["queued", "running"]); // 调研线程的这两种状态说明服务器上还有执行没结束

let seq = 0;
const uid = () => ++seq;

export default function App() {
  const [messages, setMessages] = useState([]);
  const [state, setState] = useState(null);
  const [card, setCard] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [image, setImage] = useState(null);
  const [scenario, setScenario] = useState("poster"); // poster | research；开始后锁定
  const chatRef = useRef(null);
  const fileRef = useRef(null);
  const threadRef = useRef(null); // 问题卡的回调在 state 更新前就建好了，thread_id 走 ref
  const interruptRef = useRef(null); // 同理：当前这张卡的中断 id，回答时带回去，后端据此判断卡有没有过期
  const lastEventRef = useRef(null); // 调研：收到的最后一个事件 id，断线或刷新后凭它接着读，不重不漏
  const scenarioRef = useRef(scenario); // 同理：恢复线程时卡片在 setScenario 生效前就建好了，场景也走 ref
  scenarioRef.current = scenario;

  useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, card, busy]);

  useEffect(() => { document.title = title; });

  // 调研线程的 id 放在地址栏（#r/<id>）：刷新或关掉页面再回来，从服务器取状态接着走
  useEffect(() => {
    const m = location.hash.match(/^#r\/([0-9a-f]{6,})$/);
    if (m) restoreResearch(m[1]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const patchAi = (id, fn) => setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  // 一轮结束或恢复回来：记下状态和这张卡的中断 id，按 interrupt 类型弹问题卡
  const show = (id, st, extra) => {
    threadRef.current = st.thread_id;
    interruptRef.current = st.interrupt?.id ?? null;
    if (st.scenario === "research") history.replaceState(null, "", "#r/" + st.thread_id);
    setState(st);
    patchAi(id, (m) => ({ ...m, stage: null, result: st, ...extra }));
    setCard(cardFor(st));
  };

  // 一次执行的事件流 → 更新这条助手消息。调研的 error 事件先收着：最终状态是失败的话，失败卡里会写原因
  const onEvents = (id, research, errs) => (ev, data, evId) => {
    if (research && evId) lastEventRef.current = evId;
    if (ev === "thread" && research) {
      threadRef.current = data.thread_id;
      history.replaceState(null, "", "#r/" + data.thread_id);
      if (data.queued) patchAi(id, (m) => ({ ...m, stage: QUEUED }));
    }
    if (ev === "started") patchAi(id, (m) => (m.stage === QUEUED ? { ...m, stage: "正在准备…" } : m));
    if (ev === "progress" && data.stage) patchAi(id, (m) => ({ ...m, stage: data.stage, acts: data.kind ? [...(m.acts || []), data] : m.acts }));
    if (ev === "node") patchAi(id, (m) => ({ ...m, steps: [...m.steps, data] }));
    if (ev === "error") research ? errs.push(data.message) : setError(data.message);
  };

  // 一轮停下来：展示状态。执行中出过错但线程没算失败（比如回答提交后那一步出错，卡还在），把错误告诉用户
  const finish = (id, st, errs, extra) => {
    show(id, st, extra);
    if (errs.length && st.status !== "failed") setError(st.status === "waiting" ? `${errs[errs.length - 1]}（这次的回答没用上，请重新回答）` : errs[errs.length - 1]);
  };

  // 接上服务器上这次调研的执行：补发断线期间漏掉的事件，再接着收；连不上就隔一会儿重连。拿到停下来的状态就展示、弹卡
  const follow = async (threadId, id, note, errs = []) => {
    patchAi(id, (m) => ({ ...m, stage: note }));
    for (let fails = 0; ;) {
      try {
        const st = await followResearchThread(threadId, lastEventRef.current, onEvents(id, true, errs));
        if (st && !ACTIVE.has(st.status)) return finish(id, st, errs, { restored: true });
        fails = 0; // 连得上，只是流又断了、或者执行刚开始：接着读
      } catch (e) {
        if (e.status === 404 || ++fails > 30) throw e; // 线程不在了，或者几分钟都连不上；后端重启时代理回的 5xx 也按断线重连
        patchAi(id, (m) => ({ ...m, stage: "连接断了，正在重连…" }));
      }
      await new Promise((r) => setTimeout(r, fails ? Math.min(1000 * 2 ** (fails - 1), 10000) : 500));
    }
  };

  // 跑一轮（新建、resume 或重试）：开一条助手消息，边收事件边更新，结束后按状态弹问题卡
  const run = async (fn, userText, qa) => {
    setBusy(true);
    setError(null);
    setCard(null);
    if (userText) setMessages((ms) => [...ms, { id: uid(), role: "user", text: userText }]);
    const id = uid();
    const started = Date.now();
    const research = scenarioRef.current === "research";
    const errs = [];
    setMessages((ms) => [...ms, { id, role: "ai", stage: "正在准备…", steps: [], qa: qa || null, started }]);
    try {
      const st = await fn(onEvents(id, research, errs));
      const elapsed = Math.round((Date.now() - started) / 1000);
      if (st && !(research && ACTIVE.has(st.status))) finish(id, st, errs, { elapsed });
      else if (research && threadRef.current) await follow(threadRef.current, id, "连接断了，正在重连…", errs); // 流没收完就断了
      else patchAi(id, (m) => ({ ...m, stage: null, elapsed }));
    } catch (e) {
      // 调研：回答被拒（400）、卡已过期或这一步还在处理（409）、连接断了（没有状态码）——都回服务器接上当前进度，重建问题卡
      const recoverable = research && threadRef.current && (!e.status || e.status === 400 || e.status === 409);
      if (e.status || !recoverable) setError(String(e.message || e));
      if (e.status) patchAi(id, (m) => ({ ...m, qa: null })); // 这次回答被拒了，不能显示成已回答
      if (recoverable) {
        try {
          await follow(threadRef.current, id, e.status ? "服务器还在处理这一步，等它完成" : "连接断了，正在重连…", errs);
        } catch (e2) {
          setError(`取回进度失败：${String(e2.message || e2)}`);
          patchAi(id, (m) => ({ ...m, stage: null }));
        }
      } else {
        patchAi(id, (m) => ({ ...m, stage: null }));
      }
    } finally {
      setBusy(false);
    }
  };

  const start = (text) => {
    threadRef.current = null; // 新线程：断线后只按这次拿到的 id 恢复，不能落回上一个线程
    lastEventRef.current = null;
    return run((cb) => (scenarioRef.current === "research" ? createResearchThread(text, cb) : createThread(text, image, cb)), text);
  };

  // 从地址栏的线程 id 恢复：服务器还在跑这一步就等它，停在问题上就把那张卡弹出来，结束了就展示结果
  const restoreResearch = async (threadId) => {
    scenarioRef.current = "research";
    setScenario("research");
    setBusy(true);
    threadRef.current = threadId;
    lastEventRef.current = null; // 刷新后没有事件 id：还在跑的话，服务器从这次执行的开头补发进度
    const id = uid();
    setMessages([{ id: uid(), role: "user", text: "（恢复中…）" }, { id, role: "ai", stage: "正在取回这次调研的进度", steps: [], started: Date.now() }]);
    try {
      const st = await getResearchState(threadId);
      setMessages((ms) => ms.map((m) => (m.role === "user" ? { ...m, text: st.request } : m)));
      if (ACTIVE.has(st.status)) await follow(threadId, id, st.status === "queued" ? QUEUED : "服务器还在处理这一步，等它完成");
      else show(id, st, { restored: true });
    } catch (e) {
      setError(`恢复失败：${String(e.message || e)}`);
      patchAi(id, (m) => ({ ...m, stage: null }));
      if (e.status === 404) history.replaceState(null, "", location.pathname); // 线程确实不在了才清地址栏；网络抖动时留着，刷新还能再试
    } finally {
      setBusy(false);
    }
  };
  const resume = (payload, userText, qa) =>
    run((cb) => (scenarioRef.current === "research"
      ? resumeResearchThread(threadRef.current, interruptRef.current, payload, cb)
      : resumeThread(threadRef.current, payload, cb)), userText, qa);
  const retry = () => run((cb) => retryResearchThread(threadRef.current, cb), "重试");

  // ---- 把 interrupt 变成问题卡 ----
  const cardFor = (st) => {
    if (st.scenario === "research" && st.status === "failed") return retryCard(retry); // 原因写在回复正文里
    const it = st.interrupt;
    if (!it) return null;
    if (it.type.startsWith("research.")) return researchCardFor(it, st, resume, setCard);
    if (it.type === "confirm_facts") return factsCard(it, resume);
    if (it.type === "choose_style") return styleCard(it, resume);
    if (it.type === "review") return reviewCard(it, st, resume, setCard);
    return null;
  };

  // 底部输入框：没开始或已结束 → 新建；review 中 → 自由文本按关键词路由；其他中断、调研失败 → 禁用
  const interruptType = state?.interrupt?.type;
  const finished = state && (state.scenario === "research" ? state.status === "done" : state.exported && !interruptType);
  const inputMode = !state || finished ? "new"
    : interruptType === "review" || interruptType === "research.review" ? "review"
    : interruptType === "research.ask" ? "ask" : "locked";
  const onSend = (text) => {
    if (inputMode === "new") return start(text);
    if (inputMode === "ask") return resume({ answer: text }, null, [{ q: state.interrupt.title, a: text }]);
    if (inputMode === "review") return scenarioRef.current === "research" ? routeResearchText(text, state, resume, setCard) : routeFreeText(text, state, resume, setCard);
  };
  const title = !state ? (scenario === "research" ? "灵稿 · 产品调研" : "灵稿 · 宣传海报")
    : scenario === "research" ? (state.scope?.question || "产品调研") : "黑五预热宣传海报";

  return (
    <div className="app">
      <div className="topbar">
        <h1>{title}</h1>
        {!state && !busy && ( // 跑着的时候不能切场景：卡片按线程的场景回传
          <div className="scen">
            {[["poster", "海报"], ["research", "调研"]].map(([k, l]) => <button key={k} className={scenario === k ? "sel" : ""} onClick={() => { setScenario(k); history.replaceState(null, "", location.pathname); }}>{l}</button>)}
          </div>
        )}
        <div className="icons"><span title="搜索">⌕</span><span title="分享">⇪</span><span title="预览">▭</span></div>
      </div>
      <div className="chat" ref={chatRef}>
        <div className="chat-inner">
          {messages.length === 0 && <Welcome scenario={scenario} onPick={(t) => start(t)} />}
          {messages.map((m) => (m.role === "user" ? <UserMsg key={m.id} text={m.text} />
            : <AiMsg key={m.id} m={m} onPickElement={(e) => setCard(elementCard(e, state, resume, setCard))} onCard={setCard} resume={resume} />))}
          {error && <div className="err">出错：{error}</div>}
        </div>
      </div>
      <div className="bottom">
        <div className="bottom-inner">
          {card && !busy && <QuestionCard card={card} onClose={() => setCard(null)} />}
          <InputBox
            scenario={scenario}
            mode={inputMode}
            busy={busy}
            hasCard={!!card}
            onSend={onSend}
            onReopen={() => state && setCard(cardFor(state))}
            image={image}
            onImage={() => fileRef.current?.click()}
          />
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => setImage(e.target.files?.[0] || null)} />
          <div className="footnote">{scenario === "research" ? "内容由 AI 生成，每条论断都可回指来源，事实与推断分开标记" : "内容由 AI 生成，价格、折扣、日期只来自你确认的事实"}</div>
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
  if (f.value) options.push({ label: f.value, hint: f.source === "inferred" ? "从需求中提取" : "当前值", value: f.value });
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

function reviewCard(it, st, resume, setCard) {
  const editable = it.elements.filter((e) => e.editable_by_model);
  const manual = it.elements.filter((e) => !e.bound_fact && e.kind !== "product_image" && e.kind !== "background");
  return {
    title: `第 ${it.version} 版怎么样？`,
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
      if (isCustom) return routeFreeText(v, st, resume, setCard), null;
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

// review 阶段的自由文本：按关键词路由到元素 / 事实 / 风格，猜不出来就反问
function routeFreeText(text, st, resume, setCard) {
  const t = text.trim();
  if (!t) return;
  const els = st.interrupt.elements;
  const byId = Object.fromEntries(els.map((e) => [e.id, e]));
  if (/折|价|优惠|日期|时间|几号|活动/.test(t)) return resume({ action: "change_fact" }, t);
  if (/风格|配色|颜色|背景|色调|深色|浅色/.test(t)) return resume({ action: "change_style", instruction: t }, t);
  const target = /副标题/.test(t) ? "subhead" : /标题/.test(t) ? "headline" : /按钮|号召|行动/.test(t) ? "cta" : null;
  if (target && byId[target]?.editable_by_model) return resume({ action: "edit", target, instruction: t }, t);
  if (target && byId[target]) return setCard(elementCard(byId[target], st, resume, setCard));
  setCard({
    title: `“${t}”想改哪一部分？`,
    page: "1/2",
    options: els.filter((e) => e.editable_by_model).map((e) => ({ label: KIND[e.kind] || e.kind, hint: e.text, value: e.id })),
    onAnswer: (v) => (resume({ action: "edit", target: v, instruction: t }, t), null),
  });
}

// ---------------- 组件 ----------------

function Welcome({ scenario, onPick }) {
  if (scenario === "research") {
    return (
      <div className="msg-ai">
        <div className="head"><span className="avatar">灵</span><span className="name">灵稿</span></div>
        <div className="body">
          <p>我可以帮你做调研。说一句要研究什么：需求清楚就直接查，真有歧义才问你；简单问题直接给答案，复杂问题多方查证、读原文核实。报告里每句话都能回指来源，事实和推断分开标；你排除一条来源，靠它的论断会自动退回。</p>
          <p>试试：<a href="#" onClick={(e) => { e.preventDefault(); onPick(RESEARCH_DEFAULT); }}>{RESEARCH_DEFAULT}</a></p>
        </div>
      </div>
    );
  }
  return (
    <div className="msg-ai">
      <div className="head"><span className="avatar">灵</span><span className="name">灵稿</span></div>
      <div className="body">
        <p>我可以帮你做宣传海报。说一句需求就行，我会先把价格、折扣、日期这些关键事实列出来让你确认，再问视觉方向，然后出初稿；之后你改哪一处，其余已确认的内容我都不会动。</p>
        <p>试试：<a href="#" onClick={(e) => { e.preventDefault(); onPick(DEFAULT_REQUEST); }}>{DEFAULT_REQUEST}</a></p>
      </div>
    </div>
  );
}

// 调研期间每次搜索 / 读页一行；跑完折叠成一句
function ActLog({ acts, live }) {
  const [open, setOpen] = useState(false);
  const n = { search: 0, read: 0 };
  acts.forEach((a) => { n[a.kind] = (n[a.kind] || 0) + 1; });
  const summary = `搜索 ${n.search} 次 · 读取 ${n.read} 页`;
  if (live) {
    return <div className="actlog">{acts.slice(-4).map((a, i) => <div key={i} className={a.kind}>{a.stage}</div>)}</div>;
  }
  return (
    <>
      <div className="qa-toggle" onClick={() => setOpen(!open)}>⌕ {summary} {open ? "⌃" : "⌄"}</div>
      {open && <div className="actlog">{acts.map((a, i) => <div key={i} className={a.kind}>{a.stage}</div>)}</div>}
    </>
  );
}

function UserMsg({ text }) {
  return <div className="msg-user"><div className="bubble">{text}</div></div>;
}

function AiMsg({ m, onPickElement, onCard, resume }) {
  const [open, setOpen] = useState(true);
  const st = m.result;
  const it = st?.interrupt;
  const last = st?.versions?.length ? st.versions[st.versions.length - 1] : null;
  const [sel, setSel] = useState(null);
  const cur = st?.versions?.find((v) => v.n === sel) || last;
  const elements = it?.type === "review" ? it.elements : st?.elements;
  return (
    <div className="msg-ai">
      <div className="head">
        <span className="avatar">灵</span><span className="name">灵稿</span>
        {m.elapsed != null && <span className="meta">已处理 {m.elapsed}s</span>}
        {m.restored && <span className="meta">从上次的进度恢复</span>}
      </div>
      <div className="body">
        {m.qa && m.qa.length > 0 && (
          <>
            <div className="qa-toggle" onClick={() => setOpen(!open)}>✎ 向用户提问 {open ? "⌃" : "⌄"}</div>
            {open && <div className="qa-box">{m.qa.map((x, i) => <div className="a" key={i}><div className="q">{x.q}</div><div>{x.a}</div></div>)}</div>}
          </>
        )}
        {m.steps.filter((s) => s.node === "render").map((s) => <div className="done-line" key={s.version}>已渲染第 {s.version} 版 · {s.note}</div>)}
        {m.steps.filter((s) => s.node === "confirm_scope" && s.scope).map((s, i) => <div className="done-line" key={"sc" + i}>{scopeLine(s.scope)}</div>)}
        {m.steps.filter((s) => s.node === "research").map((s, i) => <div className="done-line" key={"r" + i}>{s.answer_how === "close" ? "调研没有自己收尾，基于已有材料补了答案" : "调研完了"} · 共 {s.sources} 条候选来源</div>)}
        {m.steps.filter((s) => s.node === "consolidate").map((s, i) => <div className="done-line" key={"c" + i}>{s.gap ? `整理时发现证据缺口，回去补查：${s.gap}` : `整理出 ${s.sections} 节、${s.claims} 条论断`}</div>)}
        {m.steps.filter((s) => s.node === "write").map((s) => <div className="done-line" key={s.version}>已生成第 {s.version} 版报告 · {s.note}</div>)}
        {m.steps.filter((s) => s.node === "export" && s.exported).map((s, i) => <div className="done-line" key={"e" + i}>已导出第 {s.exported.version} 版确认物包</div>)}
        {st?.scenario !== "research" && m.steps.filter((s) => s.notes?.length).map((s, i) => <p key={i}>{s.notes.join("；")}</p>)}
        {m.acts?.length > 0 && <ActLog acts={m.acts} live={!!m.stage} />}
        {m.stage && <div className="status"><span className="dots">{m.stage}</span><span className="sep">|</span><span>{st?.scenario === "research" || m.restored || !st ? "可以关掉页面，回来后从这一步继续" : "请稍候"}</span></div>}
        {st && !m.stage && (st.scenario === "research"
          ? <ResearchBody st={st} it={it} onCard={onCard} resume={resume} />
          : <ResultBody st={st} it={it} cur={cur} sel={sel} setSel={setSel} elements={elements} onPickElement={onPickElement} />)}
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
            {failed.length > 0 ? <div className="checks bad">自动校验未通过：{failed.map((c) => `${c.name} ${c.detail}`).join("；")}</div>
              : it && <div className="checks">✓ 事实逐字一致 · ✓ 无违禁词 · ✓ 文案无数字 · ✓ 无溢出</div>}
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

function InputBox({ scenario, mode, busy, hasCard, onSend, onReopen, image, onImage }) {
  const [text, setText] = useState("");
  const research = scenario === "research";
  const placeholder = mode === "new" ? (research ? "想研究什么？简单问题直接问，复杂的说清主题、时间和信源要求" : "今天帮你做什么海报？说清商品、优惠、时间")
    : mode === "review" ? (research ? "对这版报告说点什么，例如：第一节更简短" : "对这一版说点什么，例如：标题更有紧迫感")
    : mode === "ask" ? "直接回答上面的问题…" : "请先回答上方的问题";
  const send = () => { if (text.trim() && !busy && mode !== "locked") { onSend(text.trim()); setText(""); } };
  return (
    <div className="inputbox">
      <textarea value={text} placeholder={placeholder} disabled={busy || mode === "locked"} rows={2}
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
      <div className="tools">
        <div className="left">
          {!research && <button className="tbtn" title="上传产品图" onClick={onImage} disabled={mode !== "new"}>＋ {image ? image.name : "产品图"}</button>}
          {research && <span className="tbtn">⌕ agent-tools 搜索</span>}
          <span className="tbtn">◎ {research ? "DeepSeek V4 Pro" : "豆包 Seed 2.1"}</span>
        </div>
        <div className="right">
          {mode === "locked" && !hasCard && !busy && <button className="tbtn" onClick={onReopen}>重新打开问题</button>}
          <button className="send" disabled={busy || mode === "locked" || !text.trim()} onClick={send} title="发送">↑</button>
        </div>
      </div>
    </div>
  );
}
