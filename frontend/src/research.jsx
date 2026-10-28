// 调研场景：问题卡的构造 + 助手回复里的结果渲染。卡片结构与海报共用（见 App.jsx 的 QuestionCard）。
import React, { useEffect, useState } from "react";

export const RESEARCH_DEFAULT = "AI 海报工具在局部修改和事实准确性上的现状（2026），只看中文信源";
const CKIND = { fact: "事实", inference: "推断" };
const CSTATUS = { draft: "草稿", confirmed: "用户确认", user_edited: "用户改过" };
const SSTATUS = { candidate: "候选", adopted: "系统选用", excluded: "用户排除" };

export function researchCardFor(it, st, resume, setCard) {
  if (it.type === "research.scope") return scopeCard(it, resume);
  if (it.type === "research.ask") return askCard(it, resume);
  if (it.type === "research.review") return reviewCard(it, st, resume, setCard);
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

export function scopeLine(scope) {
  if (!scope) return "";
  const parts = [["时间", scope.time_range], ["地域", scope.region], ["信源", scope.lang], ["不含", scope.exclude], ["交付", scope.deliverable], ["补充", scope.extra]]
    .filter(([, v]) => v).map(([k, v]) => `${k} ${v}`);
  return parts.length ? `范围按需求定了：${parts.join(" · ")}` : "范围按需求原话定了，没有额外限制";
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

// ---- 终审 ----
function reviewCard(it, st, resume, setCard) {
  const invalid = (it.checks || []).some((c) => c.name === "report_valid" && !c.passed);
  return {
    title: invalid ? `第 ${it.version} 版报告没有带来源的论断，没有导出` : `第 ${it.version} 版已导出，有要改的吗？`,
    page: "1/1",
    options: [
      { label: "不用改了", hint: invalid ? "结束这次调研" : "就用已导出的这版", value: "approve" },
      { label: "排除某条来源", hint: "只靠它的论断会退回草稿并移出报告，改完重新导出", value: "source" },
      { label: "改某条论断", hint: "改过的表述模型不再碰，改完重新导出", value: "claim" },
      { label: "重写某一节", hint: "只按已确认的论断重写这一节，改完重新导出", value: "section" },
    ],
    customPlaceholder: "或直接说要改什么，例如：第一节更简短",
    onAnswer: (v, isCustom) => {
      if (isCustom) return routeResearchText(v, st, resume, setCard), null;
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
    options: srcs.map((s) => ({ label: `${s.id} ${s.title || s.url}`, hint: s.url, value: s.id })),
    onAnswer: (id) => (resume({ action: "exclude_source", target: id }, `排除来源 ${id}`), null),
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
    sub: `来源 ${c.source_ids.join("、")} · ${CSTATUS[c.status]}${c.orphaned ? " · 失去来源支撑" : ""}`,
    page: "1/1",
    options: [
      { label: "改这条论断的表述", hint: "改过的表述模型不再碰", value: "edit" },
      ...srcs.map((s) => ({ label: `排除来源 ${s.id}`, hint: s.title || s.url, value: `src:${s.id}` })),
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
      if (v.startsWith("src:")) return (resume({ action: "exclude_source", target: v.slice(4) }, `排除来源 ${v.slice(4)}`), null);
      if (v === "section") return instructionCard(sec, resume);
      return null;
    },
  };
}

export function sourceCard(s, resume) {
  return {
    title: s.title || s.url,
    sub: `${s.url} · ${SSTATUS[s.status]}${s.published_at ? " · 发布 " + s.published_at.slice(0, 10) : ""}`,
    page: "1/1",
    options: s.status === "excluded" ? [] : [{ label: "排除这条来源", hint: "只靠它的论断会退回草稿并移出报告", value: "x" }],
    onAnswer: () => (resume({ action: "exclude_source", target: s.id }, `排除来源 ${s.id}`), null),
  };
}

function pickSectionCard(it, next, instruction) {
  return {
    title: instruction ? `“${instruction}”是改哪一节？` : "重写哪一节？",
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

// 终审阶段的自由文本：能对上节标题就重写那一节，否则反问改哪一节
export function routeResearchText(text, st, resume, setCard) {
  const t = text.trim();
  if (!t) return;
  const it = st.interrupt;
  const sec = it.sections.find((s) => t.includes(s.title));
  if (sec) return resume({ action: "rewrite_section", target: sec.id, instruction: t }, t);
  const m = t.match(/排除.*?(s\d+)/);
  if (m) return resume({ action: "exclude_source", target: m[1] }, t);
  setCard(pickSectionCard(it, (s) => (resume({ action: "rewrite_section", target: s.id, instruction: t }, t), null), t));
}

// ---------------- 结果渲染 ----------------

export function ResearchBody({ st, it, onCard, resume }) {
  if (it?.type === "research.scope") {
    return <p>需求里有 {(it.questions || []).length} 处要先问清，不然调研方向可能会偏。</p>;
  }
  if (it?.type === "research.ask") {
    return <p>调研到一半有个只有你能回答的问题。</p>;
  }
  if (it?.type === "research.review" || st.exported) return <ReviewBody st={st} it={it} onCard={onCard} resume={resume} />;
  return null;
}

const CHECK_LABEL = {
  answer_present: "调研有答案", claims_sourced: "论断有来源", no_excluded_sources: "未引用已排除来源", inference_marked: "推断已标记",
  sources_complete: "来源信息完整", no_orphaned_claims: "无失去来源的论断", scope_time: "时间在范围内",
  report_valid: "报告有带来源的论断", question_answered: "回答了研究问题",
};

function ReviewBody({ st, it, onCard, resume }) {
  const [tab, setTab] = useState("report");
  const [finalText, setFinalText] = useState(null);
  useEffect(() => {
    if (!it && st.exported?.report_url) fetch(st.exported.report_url).then((r) => r.text()).then(setFinalText).catch(() => {});
  }, [it, st.exported?.report_url]);
  const report = it?.report || finalText;
  const claims = it?.claims || st.claims || [];
  const sources = it?.sources || st.sources || [];
  const sections = it?.sections || [];
  const checks = it?.checks || st.checks || [];
  const failed = checks.filter((c) => !c.passed);
  const versions = st.versions || [];
  const last = versions[versions.length - 1];
  const byId = Object.fromEntries(sources.map((s) => [s.id, s]));
  const notes = [...new Set(it?.notes || st.notes || [])];
  return (
    <>
      {it ? <p>第 {it.version} 版{st.exported?.version === it.version ? "已导出" : "好了，但没有带来源的论断，没有导出"}（{last?.note}）。每句话都挂着论断，推断句有标记；觉得不合适，点论断或来源可以单独处理。{it.answer_how === "close" ? " 这次调研没有自己收尾，答案是基于已查到的材料补的，请多留意。" : ""}</p>
          : <p>已结束，用的是第 {st.exported?.version} 版。</p>}
      <div className="rtabs">
        {[["report", "报告"], ["claims", `论断 ${claims.filter((c) => !c.orphaned).length}`], ["sources", `来源 ${sources.filter((s) => s.status === "adopted").length}`]].map(([k, l]) => (
          <button key={k} className={tab === k ? "sel" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
        {versions.length > 1 && <span className="vnote">v{versions.length} · 共 {versions.length} 版</span>}
      </div>
      {tab === "report" && (report ? <Report text={report} byId={byId} /> : last && <p><a href={last.md_url} target="_blank" rel="noreferrer">打开 report_v{last.n}.md</a></p>)}
      {tab === "claims" && (
        <div className="claims">
          {sections.map((sec) => (
            <div key={sec.id}>
              <div className="ctitle">{sec.title}</div>
              {claims.filter((c) => c.outline_id === sec.id).map((c) => (
                <ClaimRow key={c.id} c={c} onClick={it ? () => onCard(claimCard(c, it, resume)) : null} />
              ))}
            </div>
          ))}
        </div>
      )}
      {tab === "sources" && <SourceList sources={sources} it={it} onCard={onCard} resume={resume} />}
      {failed.length > 0 ? <div className="checks bad">自动校验未通过：{failed.map((c) => `${CHECK_LABEL[c.name] || c.name} ${c.detail}`).join("；")}</div>
        : checks.length > 0 && <div className="checks">{checks.map((c) => "✓ " + (CHECK_LABEL[c.name] || c.name) + (c.detail ? `（${c.detail.replace(/；$/, "")}）` : "")).join(" · ")}</div>}
      {notes.length > 0 && <div className="notes">{notes.map((n, i) => <div key={i}>· {n}</div>)}</div>}
      {st.exported && (
        <div className="export-links">
          <a href={st.exported.report_url} target="_blank" rel="noreferrer">report.md</a>
          <a href={st.exported.items_url} target="_blank" rel="noreferrer">items.json</a>
          <a href={st.exported.sources_url} target="_blank" rel="noreferrer">sources.json</a>
          <a href={st.exported.zip_url}>打包下载</a>
        </div>
      )}
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
      <span className="st">{s.title || s.url}<span className="url">{s.url}</span></span>
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
      <span className="src">{c.source_ids.join(" ")}</span>
      {c.orphaned ? <span className="chip orphan">失去来源</span> : <span className={"chip " + c.status}>{CSTATUS[c.status]}</span>}
    </div>
  );
}

// 极简 markdown：标题 / 列表 / 段落；[Sn] 变成角标并带来源标题
function Report({ text, byId }) {
  const lines = text.split("\n");
  const out = [];
  lines.forEach((line, i) => {
    if (!line.trim()) return;
    if (line.startsWith("# ")) return out.push(<h2 key={i}>{line.slice(2)}</h2>);
    if (line.startsWith("## ")) return out.push(<h3 key={i}>{line.slice(3)}</h3>);
    if (line.startsWith("- ")) return out.push(<li key={i}>{line.slice(2)}</li>);
    if (/^\[S\d+\]/.test(line)) return out.push(<div className="srcline" key={i}>{line}</div>);
    out.push(<p key={i} className={line.startsWith("推断：") ? "inf" : ""}>{withMarks(line)}</p>);
  });
  return <div className="report">{out}</div>;
}

function withMarks(line) {
  const parts = line.split(/(\[S\d+\])/);
  return parts.map((p, i) => (/^\[S\d+\]$/.test(p) ? <sup key={i} className="mark">{p.slice(1, -1)}</sup> : p));
}
