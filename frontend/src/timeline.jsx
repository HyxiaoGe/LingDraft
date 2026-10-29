// 中栏：对话与过程。过程从线程状态里存下的记录拼出来（澄清、每轮调研的搜索读页、整理、每一版是按什么改的），
// 正在做的那一步叠上实时进度；问题卡就放在对话里它出现的位置。
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  IconArrowUp, IconAsk, IconBook, IconCheck, IconChevron, IconLayers, IconLoop, IconPage, IconReport, IconRetry, IconSearch, IconShield, IconSparkle, IconWarn, Spinner,
} from "./icons.jsx";
import { ACTIVE, checkLabel, domain, elapsed, label, roundSteps, scopeLine } from "./research.js";

const EXAMPLES = [
  ["简单问题", "最近新加坡的天气情况如何？"],
  ["复杂调研", "AI 海报工具在局部修改和事实准确性上的现状（2026），只看中文信源"],
];

// 每轮调研怎么收尾的（answer_how）；没拿到答案不能写成“调研完了”
const ROUND_END = {
  agent: "调研完了",
  close: "调研没有自己收尾，基于已有材料补了答案",
  none: "调研没有拿到答案",
  kept: "补查没有给出新答案，沿用上一轮的结果",
};

export function Conversation({ st, live, busy, error, now, act, isNew }) {
  const scrollRef = useRef(null);
  const stick = useRef(true); // 用户往上翻看时不自动滚到底
  const it = busy ? null : st?.interrupt;
  const fresh = isNew && !live;

  useEffect(() => { stick.current = true; }, [st?.thread_id]);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const mode = fresh ? "new" : busy ? "busy"
    : it?.type === "research.review" ? "review" : it?.type === "research.ask" ? "ask" : it?.type === "research.scope" ? "scope" : st?.status || "busy";
  const onSend = (text) => {
    if (mode === "new") return act.start(text);
    if (mode === "review") return act.instruct(text);
    if (mode === "ask") return act.answerAsk(text);
    return undefined;
  };
  const title = st?.scope?.question || st?.request || live?.request || "新的调研";
  const bounds = st?.scope_confirmed ? [st.scope?.time_range, st.scope?.region, st.scope?.lang].filter(Boolean).join(" · ") : "";

  return (
    <section className="ws-main" aria-label="对话">
      <header className="ws-head">
        <div className="ws-head-title" title={title}>{fresh ? "新的调研" : title}</div>
        {bounds && <div className="ws-head-sub">{bounds}</div>}
      </header>
      <div className="ws-scroll" ref={scrollRef}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
        <div className="ws-flow">
          {fresh ? <Welcome onPick={act.start} /> : <Flow st={st} live={live} busy={busy} now={now} act={act} it={it} />}
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
      <div className="ws-welcome-mark"><IconSparkle size={22} /></div>
      <h2>想研究什么？</h2>
      <p>说一句要研究什么。需求清楚就直接查，真有歧义才问你；简单问题直接给答案，复杂问题多方查证、读原文核实。</p>
      <p>报告里每句话都能回指来源，事实和推断分开标；你排除一条来源，只靠它的论断会自动移出报告。</p>
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

// ---------------- 对话流 ----------------

function Flow({ st, live, busy, now, act, it }) {
  const versions = st?.versions || [];
  const running = busy || ACTIVE.has(st?.status);
  const request = st?.request || live?.request;
  // 这次执行里用户刚说的话；执行中途出了新的一版，就由那一版的“按什么改的”代替
  const said = live?.said && !(versions.length > (live.versionsAtStart ?? 0)) ? live.said : null;
  const liveLine = running && <LiveLine live={live} since={st?.running_since} now={now} />;

  return (
    <>
      {request && <UserMsg text={request} />}
      <AiMsg>
        <Steps>
          {processSteps({ st, live, running, now, act, it })}
          {versions[0] && versionStep(versions[0])}
          {versions.length === 1 && checksStep(st)}
        </Steps>
        {!st && liveLine}
        {st && !versions.length && !st.current_round && !liveActs(st, live).length && liveLine}
      </AiMsg>
      {versions.slice(1).map((v, i) => (
        <React.Fragment key={v.n}>
          {v.asked && <UserMsg text={v.asked} />}
          <AiMsg>
            {v.understood && <div className="ws-understood"><span>理解为</span>{v.understood}</div>}
            <Steps>
              {versionStep(v)}
              {i === versions.length - 2 && checksStep(st)}
            </Steps>
          </AiMsg>
        </React.Fragment>
      ))}
      {said && <UserMsg text={said} />}
      {versions.length > 0 && running && <AiMsg>{liveLine}</AiMsg>}
      {!running && st && <Ending st={st} it={it} act={act} />}
    </>
  );
}

function UserMsg({ text }) {
  return <div className="ws-user"><div className="ws-bubble">{text}</div></div>;
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

function LiveLine({ live, since, now }) {
  return (
    <div className="ws-live" role="status">
      <Spinner size={14} />
      <span className="stage">{live?.stage || "正在处理"}</span>
      {since && <span className="time">已用 {elapsed(since, now)}</span>}
      <span className="hint">可以关掉页面，回来接着看</span>
    </div>
  );
}

// 实时进度里还没存进轮次记录的工具调用
const liveActs = (st, live) => {
  const saved = new Set((st?.rounds || []).flatMap((r) => r.steps.map((s) => s.id)));
  return (live?.acts || []).filter((a) => !saved.has(a.id));
};

// 一条竖线串起来的步骤；没有步骤就不画
function Steps({ children }) {
  const kids = React.Children.toArray(children).filter(Boolean);
  return kids.length ? <ol className="ws-steps">{kids}</ol> : null;
}

// 从开始到第一版：澄清、确定范围、每轮调研、整理
function processSteps({ st, live, running, now, act, it }) {
  if (!st) return [];
  const items = [];
  const qs = st.clarify || [];
  if (it?.type === "research.scope") {
    items.push(<Step key="scope-ask" icon={IconAsk} tone="ask" title={`开始前有 ${qs.length} 处要先问清`} detail="不然调研方向可能会偏" />);
    items.push(<ScopeCard key="scope-card" it={it} onSubmit={act.answerScope} />);
  } else if (qs.length && st.scope_confirmed) {
    items.push(
      <Step key="clarify" icon={IconAsk} title={`开始前问了你 ${qs.length} 个问题`} fold>
        <div className="ws-qa">
          {qs.map((q) => {
            const a = st.clarify_answers ? st.clarify_answers[q.id] : undefined;
            return (
              <div key={q.id} className="ws-qa-row">
                <div className="q">{q.title}</div>
                <div className={"a" + (a ? "" : " skipped")}>{a === undefined ? "（这次调研早于回答记录）" : a || "跳过了，交给模型判断"}</div>
              </div>
            );
          })}
          {!st.clarify_answers && st.scope?.extra && <div className="ws-qa-row"><div className="q">你的回答</div><div className="a">{st.scope.extra}</div></div>}
        </div>
      </Step>,
    );
  }
  if (st.scope_confirmed) items.push(<Step key="scope" icon={IconCheck} tone="ok" title="确定范围" detail={scopeLine(st.scope)} />);

  const rounds = st.rounds || [];
  rounds.forEach((r, i) => {
    if (r.gap) items.push(<Step key={`gap${i}`} icon={IconLoop} tone="warn" title="整理时发现证据缺口，回去补查" detail={r.gap} />);
    items.push(<RoundStep key={`r${i}`} r={r} title={r.gap ? (r.answer_how === "kept" ? ROUND_END.kept : "补查完了") : ROUND_END[r.answer_how] || "调研完了"} />);
  });
  // 这个字段之前的线程没有过程记录，只说调研的结果
  const pending = liveActs(st, live);
  const current = st.current_round || (pending.length ? { gap: st.gap, steps: [] } : null);
  if (!rounds.length && !current && st.sources?.length) {
    items.push(<Step key="old" icon={IconSearch} title={ROUND_END[st.answer_how] || "调研完了"}
      detail={`${st.sources.length} 条候选来源${st.gap_rounds ? ` · 补查过 ${st.gap_rounds} 轮` : ""} · 这次调研早于过程记录，没有搜索和读页的明细`} />);
  }
  if (current) {
    if (current.gap && !rounds.some((r) => r.gap === current.gap)) items.push(<Step key="gapnow" icon={IconLoop} tone="warn" title="整理时发现证据缺口，回去补查" detail={current.gap} />);
    items.push(<CurrentRound key="now" cur={current} acts={pending} running={running} since={st.running_since} now={now} it={it} failed={st.status === "failed"} />);
    if (it?.type === "research.ask") items.push(<AskCard key={`ask-${it.id}`} it={it} onAnswer={act.answerAsk} />);
  }
  // 整理当时的数：后来失去来源的论断也算（排除来源、改表述都不改变整理出了多少条）
  if (st.outline?.length && !current) {
    items.push(<Step key="consolidate" icon={IconLayers} title={`整理出 ${st.outline.length} 节、${(st.claims || []).length} 条论断`}
      detail={st.sources?.length ? `基于 ${st.sources.length} 条候选来源` : null} />);
  }
  return items;
}

function Step({ icon: Icon, tone = "", title, detail, fold, children, live }) {
  const [open, setOpen] = useState(false);
  const canFold = fold && children;
  return (
    <li className={"ws-step " + tone + (live ? " live" : "")}>
      <span className="ws-step-icon">{live ? <Spinner size={13} /> : <Icon size={13} />}</span>
      <div className="ws-step-body">
        {canFold ? (
          <button className="ws-step-title fold" aria-expanded={open} onClick={() => setOpen(!open)}>
            <span>{title}</span>{detail && <span className="detail"> · {detail}</span>}<IconChevron size={14} className={"chev" + (open ? " up" : "")} />
          </button>
        ) : (
          <div className="ws-step-title"><span>{title}</span>{detail && <span className="detail"> · {detail}</span>}</div>
        )}
        {(!fold || open) && children}
      </div>
    </li>
  );
}

function tally(steps) {
  const n = { search: 0, read: 0, body: 0, ask: 0, skill: 0 };
  for (const s of steps) {
    n[s.kind] = (n[s.kind] || 0) + 1;
    if (s.kind === "read" && s.body) n.body += 1;
  }
  return [n.search && `搜索 ${n.search} 次`, n.read && `读 ${n.read} 页（${n.body} 页读到正文）`, n.ask && `问你 ${n.ask} 次`, n.skill && `用了 ${n.skill} 个调研方法`]
    .filter(Boolean).join(" · ");
}

function RoundStep({ r, title }) {
  const summary = [tally(r.steps), r.new_sources ? `新增 ${r.new_sources} 条来源` : "没有新来源"].filter(Boolean).join(" · ");
  return (
    <Step icon={IconSearch} tone={r.answer_how === "none" ? "warn" : ""} title={title} detail={summary} fold>
      {r.steps.length > 0 && <ToolList steps={r.steps} />}
    </Step>
  );
}

// 正在跑（或停在提问上、半路失败）的这一轮：存下来的步骤 + 实时进度
function CurrentRound({ cur, acts, running, since, now, it, failed }) {
  const steps = roundSteps(cur.steps || [], acts);
  const title = running ? (cur.gap ? "正在补查" : "正在调研")
    : it?.type === "research.ask" ? "调研到一半，有个问题要问你" : failed ? "这一轮调研没跑完" : "调研中";
  const detail = [running && since && `已用 ${elapsed(since, now)}`, tally(steps)].filter(Boolean).join(" · ");
  return (
    <Step icon={IconSearch} title={title} detail={detail || null} live={running} tone={failed ? "warn" : ""}>
      {steps.length > 0 && <ToolList steps={steps} tail={running ? 6 : 4} />}
    </Step>
  );
}

function ToolList({ steps, tail }) {
  const [all, setAll] = useState(false);
  const hidden = tail && !all ? Math.max(0, steps.length - tail) : 0;
  return (
    <ul className="ws-tools">
      {hidden > 0 && <li className="ws-more"><button onClick={() => setAll(true)}>前面还有 {hidden} 步</button></li>}
      {steps.slice(hidden).map((s) => <ToolRow key={s.id} s={s} />)}
    </ul>
  );
}

const TOOL_ICON = { search: IconSearch, read: IconPage, skill: IconBook, ask: IconAsk };

function ToolRow({ s }) {
  if (s.pending) {
    return <li className="ws-tool pending"><Spinner size={12} /><span className="main">{s.text}</span></li>;
  }
  const Icon = TOOL_ICON[s.kind] || IconSearch;
  let main, sub, bad = !s.ok;
  if (s.kind === "search") {
    main = <>搜索「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    sub = s.ok ? `${s.hits} 条结果` : `没搜成：${s.message || "出错了"}`;
  } else if (s.kind === "read") {
    main = <>读 <a href={s.text} target="_blank" rel="noreferrer" title={s.text}>{s.title || domain(s.text)}</a></>;
    sub = s.body ? "读到正文" : `没读到正文${s.message ? `：${s.message}` : ""}`;
    bad = !s.body;
  } else if (s.kind === "skill") {
    main = <>用调研方法「{s.text}」</>;
  } else {
    main = <>问你：{s.text}</>;
    sub = s.ok ? `你答：${s.message}` : "你跳过了，按标注的假设处理";
    bad = false;
  }
  return (
    <li className={"ws-tool" + (bad ? " bad" : "")}>
      <Icon size={12} />
      <span className="main">{main}</span>
      {sub && <span className="sub">{sub}</span>}
      {s.source_ids.length > 0 && <span className="ids">{s.source_ids.map(label).join(" ")}</span>}
    </li>
  );
}

const versionStep = (v) => <Step key={`v${v.n}`} icon={IconReport} tone="ok" title={`生成第 ${v.n} 版报告`} detail={v.note} />;

function checksStep(st) {
  const checks = st.checks || [];
  if (!checks.length) return null;
  const failed = checks.filter((c) => !c.passed);
  const detail = (c) => (c.detail ? `（${c.detail.replace(/；$/, "")}）` : "");
  return failed.length
    ? <Step key="checks" icon={IconWarn} tone="warn" title={`校验有 ${failed.length} 项要留意`} detail={failed.map((c) => `${checkLabel(c)}${detail(c)}`).join("；")} />
    : <Step key="checks" icon={IconShield} tone="ok" title={`${checks.length} 项校验全过`} />;
}

// 停下来以后对话的结尾：终审的收尾栏、结束语、或者重试
function Ending({ st, it, act }) {
  const v = st.versions?.at(-1);
  if (st.status === "failed") {
    return (
      <AiMsg>
        <div className="ws-card warn">
          <div className="ws-card-title"><IconWarn size={15} />这一步没跑完</div>
          <div className="ws-card-sub">{st.error}。已经做过的搜索、整理和报告都还在，重试会从断开的地方接着跑。</div>
          <div className="ws-card-actions"><button className="ws-btn primary" onClick={act.retry}><IconRetry size={14} />重试</button></div>
        </div>
      </AiMsg>
    );
  }
  if (st.status === "done") {
    return <AiMsg><div className="ws-end"><IconCheck size={14} />{v ? `已确认第 ${v.n} 版，这次调研结束` : "这次调研已结束"}</div></AiMsg>;
  }
  if (it?.type !== "research.review") return null;
  const unused = (st.sources || []).filter((s) => s.status === "candidate").length;
  const failed = (name) => (st.checks || []).some((c) => c.name === name && !c.passed);
  const empty = !(st.claims || []).some((c) => !c.orphaned);
  return (
    <AiMsg>
      {it.reply && <div className="ws-reply">{it.reply}</div>}
      {!it.reply && (
        <p className="ws-text">
          {empty ? `第 ${it.version} 版报告里没有带来源的论断，右边写明了情况。`
            : `第 ${it.version} 版报告在右边。每句话都挂着来源，推断单独标出${unused ? `；有 ${unused} 条候选来源没有用上` : ""}。`}
          {failed("question_answered") && " 报告没有完全回答问题，开头写明了还缺什么。"}
          {it.answer_how === "close" && " 这次调研没有自己收尾，答案是基于已查到的材料补的，请多留意。"}
        </p>
      )}
      <div className="ws-closing">
        <div className="ws-closing-text">要改的话，在右边选中文字说怎么改；鼠标放在引用角标上可以看来源、排除来源；也可以直接在下面说。</div>
        <button className="ws-btn primary" onClick={act.approve}><IconCheck size={14} />不用改了</button>
      </div>
    </AiMsg>
  );
}

// ---------------- 问题卡 ----------------

// 澄清：所有问题放在一张卡上；没选的交给模型判断，会写进报告的假设里
function ScopeCard({ it, onSubmit }) {
  const qs = it.questions || [];
  const [ans, setAns] = useState({});
  const set = (id, v) => setAns((a) => ({ ...a, [id]: v }));
  const answered = qs.filter((q) => (ans[q.id] || "").trim()).length;
  const submit = (skipAll) => onSubmit(Object.fromEntries(qs.map((q) => [q.id, skipAll ? null : (ans[q.id] || "").trim() || null])));
  return (
    <li className="ws-card ask">
      {qs.map((q, i) => (
        <div key={q.id} className="ws-q">
          <div className="ws-card-title"><span className="num">{i + 1}</span>{q.title}</div>
          {q.why && <div className="ws-card-sub">为什么问：{q.why}</div>}
          <Choices options={q.options} value={ans[q.id] || ""} onChange={(v) => set(q.id, v)} />
        </div>
      ))}
      <div className="ws-card-foot">
        <span>{answered < qs.length ? "没回答的交给模型判断，会写进报告的假设里" : "都回答了"}</span>
        <button className="ws-btn ghost" onClick={() => submit(true)}>都交给模型判断</button>
        <button className="ws-btn primary" onClick={() => submit(false)}>开始调研</button>
      </div>
    </li>
  );
}

// 选项 + 自己写；自己写的内容和选中的选项二选一
function Choices({ options, value, onChange, onPick }) {
  const custom = value && !options.includes(value) ? value : "";
  return (
    <div className="ws-choices">
      {options.map((o) => (
        <button key={o} className={"ws-chip" + (value === o ? " sel" : "")} aria-pressed={value === o}
          onClick={() => (onPick ? onPick(o) : onChange(value === o ? "" : o))}>{o}</button>
      ))}
      {!onPick && <input className="ws-chip-input" value={custom} placeholder="或者自己写…" aria-label="自己写回答" onChange={(e) => onChange(e.target.value)} />}
    </div>
  );
}

// 调研中途的提问：点选项直接回答；也可以在下面的输入框里说
function AskCard({ it, onAnswer }) {
  const [text, setText] = useState("");
  const send = () => text.trim() && onAnswer(text.trim());
  return (
    <li className="ws-card ask">
      <div className="ws-card-title"><IconAsk size={15} />{it.title}</div>
      {it.why && <div className="ws-card-sub">为什么问：{it.why}</div>}
      {it.options?.length > 0 && <Choices options={it.options} value="" onPick={onAnswer} />}
      <div className="ws-card-input">
        <input value={text} placeholder="或者自己回答…" aria-label="自己回答"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()} />
        <button className="ws-btn primary" disabled={!text.trim()} onClick={send}>回答</button>
      </div>
      <div className="ws-card-foot">
        <span>说不清楚也没关系，模型会按最可能的理解继续，并在报告里写明假设</span>
        <button className="ws-btn ghost" onClick={() => onAnswer(null)}>说不清楚，跳过</button>
      </div>
    </li>
  );
}

// ---------------- 输入框 ----------------

const PLACEHOLDER = {
  new: "想研究什么？简单问题直接问，复杂的说清主题、时间和信源要求",
  review: "对这版报告说点什么，例如：第一节更简短",
  ask: "直接回答上面的问题…",
  scope: "先回答上面的问题",
  busy: "正在处理，稍等…",
  done: "这次调研已经结束；要研究别的，点左上角“新建调研”",
  failed: "这一步没跑完，点上面的“重试”",
};

function Composer({ mode, onSend }) {
  const [text, setText] = useState("");
  const ref = useRef(null);
  const enabled = mode === "new" || mode === "review" || mode === "ask";
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
