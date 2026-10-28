// 调研场景：问题卡的构造 + 助手回复里的结果渲染。卡片结构与海报共用（见 App.jsx 的 QuestionCard）。
import React, { useState } from "react";

export const RESEARCH_DEFAULT = "AI 海报工具在局部修改和事实准确性上的现状（2026），只看中文信源";
const CKIND = { fact: "事实", inference: "推断" };
const CSTATUS = { draft: "草稿", confirmed: "用户确认", user_edited: "用户改过" };
const SSTATUS = { candidate: "候选", adopted: "系统选用", excluded: "用户排除" };

export function researchCardFor(it, resume) {
  if (it.type === "research.scope") return scopeCard(it, resume);
  if (it.type === "research.ask") return askCard(it, resume);
  if (it.type === "research.review") return reviewCard(it, resume);
  return null;
}

// ---- 澄清：只有 intake 认为需求真的模糊时才会有这张卡，一个问题一页 ----
function scopeCard(it, resume, answers = {}, idx = 0, history = []) {
  const qs = it.questions || [];
  if (idx >= qs.length) {
    resume({ answers }, null, history);
    return null;
  }
  const q = qs[idx];
  const next = (v) => scopeCard(it, resume, { ...answers, [q.id]: v }, idx + 1, [...history, { q: q.title, a: v || "（跳过，模型自行判断并记录假设）" }]);
  return {
    title: q.title,
    sub: q.why ? `不问会怎样：${q.why}` : null,
    page: `${idx + 1}/${qs.length}`,
    options: q.options.map((o) => ({ label: o, value: o })),
    customPlaceholder: "或者直接回答…",
    skip: "跳过，让模型自己判断",
    onAnswer: (v) => next(v),
    onSkip: () => next(null),
  };
}

function scopeLine(scope) {
  if (!scope) return "";
  const parts = [["时间", scope.time_range], ["地域", scope.region], ["信源", scope.lang], ["不含", scope.exclude], ["交付", scope.deliverable], ["补充", scope.extra]]
    .filter(([, v]) => v).map(([k, v]) => `${k} ${v}`);
  return parts.length ? `范围按需求定了：${parts.join(" · ")}` : "范围按需求原话定了，没有额外限制";
}

// 每轮调研怎么收尾的（后端进度事件里的 answer_how）；没拿到答案不能写成“调研完了”
const ROUND_END = {
  agent: "调研完了",
  close: "调研没有自己收尾，基于已有材料补了答案",
  none: "调研没有拿到答案",
  kept: "补查没有给出新答案，沿用上一轮的结果",
};

// 调研各步做完时的进度行；不需要说的步骤返回 null
export function researchStepLine(s) {
  if (s.node === "confirm_scope") return s.scope ? scopeLine(s.scope) : null;
  if (s.node === "research") return `${ROUND_END[s.answer_how] || "调研完了"} · 共 ${s.sources} 条候选来源`;
  if (s.node === "consolidate") return s.gap ? `整理时发现证据缺口，回去补查：${s.gap}` : `整理出 ${s.sections} 节、${s.claims} 条论断`;
  if (s.node === "write" && s.version != null) return `已生成第 ${s.version} 版报告 · ${s.note}`;
  return null;
}

// ---- 动态提问：模型在调研中途问的 ----
function askCard(it, resume) {
  return {
    title: it.title,
    sub: it.why ? `不问会怎样：${it.why}` : null,
    page: "1/1",
    options: it.options.map((o) => ({ label: o, value: o })),
    customPlaceholder: "或者直接回答…",
    skip: "说不清楚，按假设处理",
    onAnswer: (v) => (resume({ answer: v }, null, [{ q: it.title, a: v }]), null),
    onSkip: () => (resume({ skip: true }, null, [{ q: it.title, a: "（说不清楚，模型按标注的假设处理）" }]), null),
  };
}

// ---- 失败：某一步出错或执行被掐断，已有的内容都在，从断开的地方接着跑 ----
export function retryCard(retry) {
  return {
    title: "这一步没跑完，要从断开的地方重试吗？",
    page: "1/1",
    options: [{ label: "重试", hint: "已经做过的搜索、整理和报告都保留，从没跑完的那一步接着跑", value: "retry" }],
    onAnswer: () => (retry(), null),
  };
}

// ---- 终审：报告已经交付，这里只是可选的修改 ----

const exclude = (id, resume) => (resume({ action: "exclude_source", target: id }, `排除来源 ${id}`), null);
// 来源没有标题时用网址当名字，这时就不再另外列一遍网址
const sourceName = (s) => s.title || s.url;

function reviewCard(it, resume) {
  const failed = (name) => (it.checks || []).some((c) => c.name === name && !c.passed);
  // reply：上一句话模型没能完全对上时的回复，放在标题上让用户接着说
  const title = it.reply ? it.reply
    : failed("report_valid") ? `第 ${it.version} 版报告里没有带来源的论断`
    : failed("question_answered") ? `第 ${it.version} 版报告好了，但没有完全回答问题，有要改的吗？`
    : `第 ${it.version} 版报告好了，有要改的吗？`;
  return {
    title,
    page: "1/1",
    options: [
      { label: "不用改了", hint: "就用这版，结束这次调研", value: "approve" },
      { label: "排除某条来源", hint: "只靠它的论断会退回草稿并移出报告，改完出新一版", value: "source" },
      { label: "改某条论断", hint: "改过的表述模型不再碰，改完出新一版", value: "claim" },
      { label: "重写某一节", hint: "只按已确认的论断重写这一节，改完出新一版", value: "section" },
    ],
    customPlaceholder: "或直接说要改什么，例如：第一节更简短",
    onAnswer: (v, isCustom) => {
      if (isCustom) return (resume({ action: "instruct", text: v }, v), null); // 原话交给后端由模型理解，对不上时回复显示在下一张卡的标题上
      if (v === "approve") return (resume({ action: "approve" }, "不用改了"), null);
      if (v === "source") return pickSourceCard(it, resume);
      if (v === "claim") return pickClaimCard(it, (c) => claimCard(c, it, resume));
      if (v === "section") return pickSectionCard(it, (sec) => instructionCard(sec, resume));
      return null;
    },
  };
}

function pickSourceCard(it, resume) {
  const srcs = it.sources.filter((s) => s.status === "adopted");
  return {
    title: "排除哪条来源？",
    page: "2/2",
    options: srcs.map((s) => ({ label: `${s.id} ${sourceName(s)}`, hint: s.title ? s.url : null, value: s.id })),
    onAnswer: (id) => exclude(id, resume),
  };
}

function pickClaimCard(it, next) {
  const claims = it.claims.filter((c) => !c.orphaned);
  return {
    title: "改哪条论断？",
    page: "2/3",
    options: claims.map((c) => ({ label: `${c.id} ${c.statement}`, hint: CKIND[c.kind], value: c.id })),
    onAnswer: (id) => next(claims.find((c) => c.id === id)),
  };
}

export function claimCard(c, it, resume) {
  const srcs = it.sources.filter((s) => c.source_ids.includes(s.id));
  const sec = it.sections?.find((s) => s.id === c.outline_id);
  return {
    title: `${CKIND[c.kind]}：${c.statement}`,
    sub: [c.source_ids.length > 0 && `来源 ${c.source_ids.join("、")}`, CSTATUS[c.status], c.orphaned && "失去来源支撑"].filter(Boolean).join(" · "),
    page: "1/1",
    options: [
      { label: "改这条论断的表述", hint: "改过的表述模型不再碰", value: "edit" },
      ...srcs.map((s) => ({ label: `排除来源 ${s.id}`, hint: sourceName(s), value: `src:${s.id}` })),
      ...(sec ? [{ label: `重写「${sec.title}」`, hint: "只按已确认的论断重写", value: "section" }] : []),
    ],
    onAnswer: (v) => {
      if (v === "edit")
        return {
          title: "改成什么？",
          page: "1/1",
          options: [],
          customPlaceholder: `当前：${c.statement}`,
          requireAnswer: true,
          onAnswer: (t) => (resume({ action: "edit_claim", target: c.id, content: t }, `论断 ${c.id} 改成：${t}`), null),
        };
      if (v.startsWith("src:")) return exclude(v.slice(4), resume);
      if (v === "section") return instructionCard(sec, resume);
      return null;
    },
  };
}

export function sourceCard(s, resume) {
  return {
    title: sourceName(s),
    sub: [s.title && s.url, SSTATUS[s.status], s.published_at && "发布 " + s.published_at.slice(0, 10)].filter(Boolean).join(" · "),
    page: "1/1",
    options: s.status === "excluded" ? [] : [{ label: "排除这条来源", hint: "只靠它的论断会退回草稿并移出报告", value: "x" }],
    onAnswer: () => exclude(s.id, resume),
  };
}

function pickSectionCard(it, next) {
  return {
    title: "重写哪一节？",
    page: "2/3",
    options: it.sections.map((s) => ({ label: s.title, hint: `${s.sentences.length} 句`, value: s.id })),
    onAnswer: (id) => next(it.sections.find((s) => s.id === id)),
  };
}

function instructionCard(sec, resume) {
  return {
    title: `「${sec.title}」怎么改？`,
    page: "3/3",
    options: [
      { label: "更简短", value: "更简短" },
      { label: "先给结论再给依据", value: "先给结论再给依据" },
      { label: "多说明局限", value: "多说明局限" },
    ],
    customPlaceholder: "其他要求…",
    onAnswer: (v) => (resume({ action: "rewrite_section", target: sec.id, instruction: v }, `重写「${sec.title}」：${v}`), null),
  };
}

// ---------------- 结果渲染 ----------------

// live：这是线程当前的状态，报告里的论断、来源可以点着改；历史消息只展示
export function ResearchBody({ st, it, live, onCard, resume }) {
  if (st.status === "failed") {
    return (
      <>
        <p>这一步没跑完：{st.error}。已有的内容都还在，可以从断开的地方重试。</p>
        {st.versions?.length > 0 && <ReviewBody st={st} it={null} live={live} onCard={onCard} resume={resume} />}
      </>
    );
  }
  if (it?.type === "research.scope") {
    return <p>需求里有 {(it.questions || []).length} 处要先问清，不然调研方向可能会偏。</p>;
  }
  if (it?.type === "research.ask") {
    return <p>调研到一半有个只有你能回答的问题。</p>;
  }
  // 有一版报告就摆出来：校验没过的项、没答完的地方只是说明，不扣住结果
  if (it?.type === "research.review" || st.versions?.length > 0) return <ReviewBody st={st} it={it} live={live} onCard={onCard} resume={resume} />;
  return null;
}

const CHECK_LABEL = {
  answer_present: "调研有答案", claims_sourced: "论断有来源", no_excluded_sources: "未引用已排除来源", inference_marked: "推断已标记",
  sources_complete: "来源信息完整", no_orphaned_claims: "无失去来源的论断", scope_time: "时间在范围内",
  report_valid: "报告有带来源的论断", question_answered: "回答了研究问题",
};
const checkLabel = (c) => CHECK_LABEL[c.name] || c.name;
const checkDetail = (c) => (c.detail ? `（${c.detail.replace(/；$/, "")}）` : "");

function ReviewBody({ st, it, live, onCard, resume }) {
  const [tab, setTab] = useState("report");
  const versions = st.versions || [];
  const last = versions[versions.length - 1];
  const blocks = it?.blocks || st.report_blocks; // 旧的终审卡里没有 blocks，用线程当前状态现算的
  const claims = it?.claims || st.claims || [];
  const sources = it?.sources || st.sources || [];
  const sections = it?.sections || st.outline || [];
  const act = live && it ? it : null; // 能点着改的卡
  const checks = it?.checks || st.checks || [];
  const failed = checks.filter((c) => !c.passed);
  const notes = [...new Set(it?.notes || st.notes || [])];
  return (
    <>
      {it ? <p>第 {it.version} 版报告好了（{last?.note}）。{claims.some((c) => !c.orphaned) && <>每句话都挂着论断，推断句有标记{act ? "；觉得不合适，点论断或来源可以单独处理" : ""}。</>}{it.answer_how === "close" ? " 这次调研没有自己收尾，答案是基于已查到的材料补的，请多留意。" : ""}</p>
          : st.status !== "failed" && last && <p>已结束，用的是第 {last.n} 版。</p>}
      <div className="rtabs">
        {[["report", "报告"], ["claims", `论断 ${claims.filter((c) => !c.orphaned).length}`], ["sources", `来源 ${sources.filter((s) => s.status === "adopted").length}`]].map(([k, l]) => (
          <button key={k} className={tab === k ? "sel" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
        {versions.length > 1 && <span className="vnote">v{versions.length} · 共 {versions.length} 版</span>}
      </div>
      {tab === "report" && blocks?.length > 0 && <Report blocks={blocks} />}
      {tab === "claims" && (
        <div className="claims">
          {sections.map((sec) => (
            <div key={sec.id}>
              <div className="ctitle">{sec.title}</div>
              {claims.filter((c) => c.outline_id === sec.id).map((c) => (
                <ClaimRow key={c.id} c={c} onClick={act ? () => onCard(claimCard(c, act, resume)) : null} />
              ))}
            </div>
          ))}
        </div>
      )}
      {tab === "sources" && <SourceList sources={sources} it={act} onCard={onCard} resume={resume} />}
      {failed.length > 0 ? <div className="checks warn">需要留意：{failed.map((c) => `「${checkLabel(c)}」没通过${checkDetail(c)}`).join("；")}</div>
        : checks.length > 0 && <div className="checks">{checks.map((c) => `✓ ${checkLabel(c)}${checkDetail(c)}`).join(" · ")}</div>}
      {notes.length > 0 && <div className="notes">{notes.map((n, i) => <div key={i}>· {n}</div>)}</div>}
    </>
  );
}

// 来源：报告引用的在前，排除的其次；搜到但没引用的候选默认收起
function SourceList({ sources, it, onCard, resume }) {
  const [showAll, setShowAll] = useState(false);
  const used = sources.filter((s) => s.status !== "candidate");
  const rest = sources.filter((s) => s.status === "candidate");
  const shown = showAll ? [...used, ...rest] : used;
  const row = (s) => (
    <div className={"src " + s.status} key={s.id} onClick={it && s.status !== "excluded" ? () => onCard(sourceCard(s, resume)) : null}>
      <span className="sid">{s.id}</span>
      <span className="st">{sourceName(s)}{s.title && <span className="url">{s.url}</span>}</span>
      <span className={"chip " + s.status}>{SSTATUS[s.status]}</span>
    </div>
  );
  return (
    <>
      <div className="sources">{shown.map(row)}{shown.length === 0 && <div className="src">还没有被引用的来源</div>}</div>
      {rest.length > 0 && <div className="qa-toggle" onClick={() => setShowAll(!showAll)}>{showAll ? "收起候选来源" : `还有 ${rest.length} 条搜到但没引用的候选来源`} {showAll ? "⌃" : "⌄"}</div>}
    </>
  );
}

function ClaimRow({ c, onClick }) {
  return (
    <div className={"claim" + (c.orphaned ? " orphan" : "") + (onClick ? " clickable" : "")} onClick={onClick || undefined}>
      <span className={"chip " + c.kind}>{CKIND[c.kind]}</span>
      <span className="t">{c.statement}</span>
      <span className="sids">{c.source_ids.join(" ")}</span>
      {c.orphaned ? <span className="chip orphan">失去来源</span> : <span className={"chip " + c.status}>{CSTATUS[c.status]}</span>}
    </div>
  );
}

// 报告按后端给的结构化块渲染（和 report.md 出自同一份数据）：哪句是推断、挂哪些来源都由论断决定，不解析文字
function Report({ blocks }) {
  const out = blocks.map((b, i) => {
    if (b.type === "title") return <h2 key={i}>{b.text}</h2>;
    if (b.type === "heading") return <h3 key={i}>{b.text}</h3>;
    if (b.type === "notice") return <p key={i} className="notice">{b.text}</p>;
    if (b.type === "scope") return <p key={i}>{b.text}</p>;
    if (b.type === "item") return <li key={i}>{b.text}</li>;
    // 旧版本的块里没有标题的来源 title 就是网址
    if (b.type === "source") return <div className="srcline" key={i}>[{b.label}] {b.title && b.title !== b.url && `${b.title} — `}{b.url}{b.meta ? `（${b.meta}）` : ""}</div>;
    if (b.type === "sentence")
      return <p key={i} className={b.inference ? "inf" : ""}>{b.text}{b.marks.map((m) => <sup key={m} className="mark">{m}</sup>)}</p>;
    return null;
  });
  return <div className="report">{out}</div>;
}
