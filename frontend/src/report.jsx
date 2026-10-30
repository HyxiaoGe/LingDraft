// 右栏：交付面板。有报告时是报告 / 论断 / 来源三个页签，报告上可以直接改：悬停角标看来源、排除来源，选中文字说怎么改，
// 章节标题旁重写这一节。还没有报告时，等范围确认就列研究范围，调研中就边查边列已经找到的来源。
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconArrowUp, IconCheck, IconEdit, IconExternal, IconLoop, IconPage, IconReport, IconSearch, IconShield, IconWarn, IconX, Spinner } from "./icons.jsx";
import { SSTATUS, checkLabel, domain, foundSources, reportBlocks, sourceName, sourceNumbers } from "./research.js";

const CKIND = { fact: "事实", inference: "推断" };
const CSTATUS = { draft: "草稿", confirmed: "已确认", user_edited: "你改过" };
const REWRITE_PRESETS = ["更简短", "先给结论再给依据", "多说明局限"];
const uniq = (xs) => [...new Set(xs)];
const byNumber = (num) => (a, b) => (+num[a] || Infinity) - (+num[b] || Infinity); // 有编号的按编号排，没编号的放后面

// 来源网站的图标，后端代取（/api/favicon/域名）；取不到显示域名首字母。取不到的域名记下来，这个页面里不再请求
const noIcon = new Set();
const hostOf = (url) => {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
};
function Favicon({ url }) {
  const host = hostOf(url);
  const [failed, setFailed] = useState(null);
  if (!host || noIcon.has(host) || failed === host) return <span className="ws-fav letter" aria-hidden="true">{(domain(url) || "?")[0].toUpperCase()}</span>;
  return <img className="ws-fav" src={`/api/favicon/${host}`} alt="" loading="lazy" onError={() => { noIcon.add(host); setFailed(host); }} />;
}

export function Deliverable({ st, live, busy, act, isNew }) {
  const [tab, setTab] = useState("report");
  useEffect(() => setTab("report"), [st?.thread_id]);
  const versions = st?.versions || [];
  const it = !busy ? st?.interrupt : null;
  const canEdit = it?.type === "research.review";

  if (!versions.length) {
    const scoping = it?.type === "research.scope";
    return (
      <section className="ws-panel" aria-label="交付">
        <header className="ws-panel-head"><span className="ws-panel-name">{scoping ? "研究范围" : st || live ? "调研中找到的来源" : "报告"}</span></header>
        <div className="ws-panel-scroll">
          {isNew && !live ? <EmptyPanel /> : scoping ? <ScopePanel st={st} /> : <FoundSources st={st} live={live} busy={busy} />}
        </div>
      </section>
    );
  }

  const v = versions.at(-1);
  const claims = st.claims || [];
  const sources = st.sources || [];
  const kept = claims.filter((c) => !c.orphaned);
  const tabs = [["report", "报告"], ["claims", `论断 ${kept.length}`], ["sources", `来源 ${sources.filter((s) => s.status === "adopted").length}`]];
  // 有报告以后还在跑：多半是在按用户的修改出新一版（也可能是重试、或者接上了别处发起的执行）
  const startN = live?.versionsAtStart;
  const banner = !busy ? null : startN != null && versions.length > startN ? `第 ${v.n} 版已经出来了，正在做最后的校验`
    : startN ? `正在按你的要求出第 ${startN + 1} 版，报告暂时不能改` : "服务器还在处理，报告暂时不能改";
  return (
    <section className="ws-panel" aria-label="交付">
      <header className="ws-panel-head">
        <div className="ws-tabs" role="tablist" aria-label="交付内容" onKeyDown={(e) => {
          const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
          if (!step) return;
          const k = tabs[(tabs.findIndex(([x]) => x === tab) + step + tabs.length) % tabs.length][0];
          setTab(k);
          e.currentTarget.querySelector(`#ws-tab-${k}`)?.focus();
        }}>
          {tabs.map(([k, l]) => (
            <button key={k} id={`ws-tab-${k}`} role="tab" aria-selected={tab === k} aria-controls="ws-tabpanel" tabIndex={tab === k ? 0 : -1}
              className={tab === k ? "sel" : ""} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
        <span className="ws-version" title={v.note}>第 {v.n} 版{versions.length > 1 ? ` / 共 ${versions.length} 版` : " · 初稿"}</span>
      </header>
      {banner && <div className="ws-banner"><Spinner size={13} />{banner}</div>}
      {st.status === "done" && <div className="ws-banner ok"><IconCheck size={13} />已确认，这次调研用的是第 {v.n} 版</div>}
      <div id="ws-tabpanel" role="tabpanel" aria-labelledby={`ws-tab-${tab}`} className={"ws-panel-scroll" + (busy ? " dim" : "")}>
        {tab === "report" && <Report st={st} canEdit={canEdit} act={act} />}
        {tab === "claims" && <ClaimList st={st} canEdit={canEdit} act={act} />}
        {tab === "sources" && <SourceList st={st} canEdit={canEdit} act={act} />}
      </div>
      <PanelFoot st={st} />
    </section>
  );
}

function EmptyPanel() {
  return (
    <div className="ws-empty">
      <div className="ws-empty-icon"><IconReport size={22} /></div>
      <div className="ws-empty-title">报告会出现在这里</div>
      <ul>
        <li><IconPage size={14} />每句话都挂着来源，事实和推断分开标</li>
        <li><IconSearch size={14} />鼠标放在引用角标上看来源，不可信的可以排除</li>
        <li><IconEdit size={14} />选中报告里的文字，直接说怎么改</li>
      </ul>
    </div>
  );
}

function ScopePanel({ st }) {
  const s = st?.scope;
  if (!s) return null;
  const rows = [["时间", s.time_range], ["地域", s.region], ["信源", s.lang], ["不含", s.exclude], ["交付", s.deliverable]].filter(([, x]) => x);
  return (
    <div className="ws-scope">
      <div className="ws-scope-q">{s.question}</div>
      {rows.length > 0 && <dl>{rows.map(([k, x]) => <React.Fragment key={k}><dt>{k}</dt><dd>{x}</dd></React.Fragment>)}</dl>}
      <p className="ws-muted">{st.interrupt?.questions?.length ? "左边的问题回答完就按这个范围开始调研；你的回答会补进范围里。" : "点“开始调研”就按这个范围开始。"}</p>
    </div>
  );
}

// 调研中边查边列：已入库的来源 + 这一轮先算出编号的 + 实时进度里刚找到的，最新的在上面
function FoundSources({ st, live, busy }) {
  const found = foundSources(st, live?.acts).reverse();
  const read = found.filter((s) => s.body).length;
  if (!found.length) {
    return (
      <div className="ws-empty">
        <div className="ws-empty-icon">{busy ? <Spinner size={20} /> : <IconSearch size={22} />}</div>
        <div className="ws-empty-title">{busy ? "正在找资料" : "还没有找到来源"}</div>
        <p className="ws-muted">搜到和读到的来源会边查边列在这里，整理好以后出报告。</p>
      </div>
    );
  }
  return (
    <div className="ws-found">
      <div className="ws-found-head">已找到 {found.length} 条，{read} 条读到了正文{busy && <Spinner size={12} />}</div>
      <ul>
        {found.map((s) => (
          <li key={s.url} className="ws-found-row">
            <span className={"kind" + (s.body ? " body" : "")}>{s.body ? "正文" : "摘要"}</span>
            <a href={s.url} target="_blank" rel="noreferrer" title={s.title ? `${s.title}\n${s.url}` : s.url}>
              <span className="t">{s.title || domain(s.url)}</span>
              <span className="d"><Favicon url={s.url} />{domain(s.url)}{s.published_at ? ` · 发布 ${s.published_at.slice(0, 10)}` : ""}</span>
            </a>
          </li>
        ))}
      </ul>
      <p className="ws-muted">整理时按需求挑出有用的来源，报告里每句话标明出处。</p>
    </div>
  );
}

// ---------------- 报告 ----------------

// 只靠这条来源的论断：排除它之后这些论断失去来源，会移出报告
function soleClaims(st, sid) {
  const excluded = new Set((st.sources || []).filter((s) => s.status === "excluded").map((s) => s.id));
  return (st.claims || []).filter((c) => !c.orphaned && c.source_ids.includes(sid) && c.source_ids.every((x) => x === sid || excluded.has(x)));
}

function Report({ st, canEdit, act }) {
  const blocks = reportBlocks(st);
  const byId = Object.fromEntries((st.sources || []).map((s) => [s.id, s]));
  const num = sourceNumbers(blocks);
  const byNum = Object.fromEntries(Object.entries(num).map(([sid, n]) => [n, byId[sid]]));
  const article = useRef(null);
  const [pop, setPop] = useState(null); // 悬停的引用角标：{sid, rect, el}
  const popRef = useRef(null);
  const [quote, setQuote] = useState(null); // 选中的文字：{text, claim_ids, section_ids, rect, open}
  const [rewrite, setRewrite] = useState(null); // 正在填“怎么重写”的节 id
  const closeTimer = useRef(null);

  const showPop = (sid, el) => {
    clearTimeout(closeTimer.current);
    setPop({ sid, rect: el.getBoundingClientRect(), el });
  };
  const closePop = () => { // Esc：收起来源卡，焦点回到角标上
    clearTimeout(closeTimer.current);
    pop?.el?.focus();
    setPop(null);
  };
  const hidePop = () => {
    clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setPop(null), 220);
  };
  const keepPop = () => clearTimeout(closeTimer.current);
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => {
    if (!pop) return undefined;
    const down = (e) => !e.target.closest?.(".ws-pop, .ws-mark") && setPop(null); // 点别处就收起
    addEventListener("mousedown", down);
    return () => removeEventListener("mousedown", down);
  }, [pop]);

  // 选中的文字落在哪些论断、哪些节上：看选区碰到了哪些带 data-claim / data-section 的元素
  const capture = () => {
    if (!canEdit) return;
    const sel = getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (!article.current?.contains(range.commonAncestorContainer)) return;
    const text = sel.toString().replace(/\s+/g, " ").trim();
    if (!text) return;
    const hit = [...article.current.querySelectorAll("[data-claim],[data-section]")].filter((el) => range.intersectsNode(el));
    setQuote({
      text: text.slice(0, 1000),
      claim_ids: uniq(hit.map((el) => el.dataset.claim).filter(Boolean)),
      section_ids: uniq(hit.map((el) => el.dataset.section).filter(Boolean)),
      rect: range.getBoundingClientRect(),
      range: range.cloneRange(),
      open: false,
    });
  };
  const closeQuote = () => setQuote(null);
  useHighlight(quote?.open ? quote.range : null);

  // 面板滚动、窗口变化时浮层跟不上：来源卡收起；选中的文字没在填就收起，在填就跟着选区挪
  useEffect(() => {
    const box = article.current?.closest(".ws-panel-scroll");
    const onMove = () => {
      setPop(null);
      setQuote((q) => q && (q.open ? { ...q, rect: q.range.getBoundingClientRect() } : null));
    };
    box?.addEventListener("scroll", onMove, { passive: true });
    addEventListener("resize", onMove);
    return () => { box?.removeEventListener("scroll", onMove); removeEventListener("resize", onMove); };
  }, []);

  let section = null;
  const out = [];
  blocks.forEach((b, i) => {
    if (b.type === "title") out.push(<h1 key={i}>{b.text}</h1>);
    else if (b.type === "notice") out.push(<p key={i} className="ws-notice"><IconWarn size={14} />{b.text}</p>);
    else if (b.type === "scope") out.push(<p key={i} className="ws-rscope">{b.text}</p>);
    else if (b.type === "heading") {
      section = b.section_id || null;
      const sec = b.section_id ? { id: b.section_id, title: b.text } : null;
      out.push(
        <div key={i} className="ws-h2" data-section={sec?.id}>
          <h2>{b.text}</h2>
          {sec && canEdit && <button className="ws-h2-act" onClick={() => setRewrite(rewrite === sec.id ? null : sec.id)}><IconLoop size={12} />重写这一节</button>}
        </div>,
      );
      if (sec && canEdit && rewrite === sec.id) out.push(<RewriteBox key={`rw${i}`} sec={sec} onCancel={() => setRewrite(null)} onSubmit={(t) => { setRewrite(null); act.rewrite(sec, t); }} />);
    } else if (b.type === "item") out.push(<p key={i} className="ws-item">{b.text}</p>);
    else if (b.type === "source") {
      const s = byId[b.source_id];
      out.push(
        <div key={i} className={"ws-srcline" + (s?.status === "excluded" ? " excluded" : "")}>
          <span className="lb">{b.label}</span>
          <a href={b.url} target="_blank" rel="noreferrer">{b.title && b.title !== b.url ? b.title : b.url}</a>
          <span className="meta"><Favicon url={b.url} />{domain(b.url)}{b.meta ? ` · ${b.meta}` : ""}</span>
        </div>,
      );
    } else if (b.type === "sentence") {
      const mark = (m) => {
        const s = byNum[m];
        return (
          <button key={m} className={"ws-mark" + (s?.status === "excluded" ? " excluded" : "") + (pop?.sid === s?.id ? " on" : "")}
            aria-label={`来源 ${m}`} onMouseEnter={(e) => s && showPop(s.id, e.currentTarget)} onMouseLeave={hidePop}
            onFocus={(e) => s && showPop(s.id, e.currentTarget)} onBlur={hidePop} onKeyDown={(e) => e.key === "Escape" && pop && closePop()}
            onClick={(e) => {
              if (!s) return;
              if (e.detail === 0) { // 键盘按的回车或空格：来源卡已随焦点打开，把焦点移进卡里，“打开原文”“排除”才按得到
                showPop(s.id, e.currentTarget);
                requestAnimationFrame(() => popRef.current?.querySelector("a, button")?.focus());
              } else if (pop?.sid === s.id) setPop(null);
              else showPop(s.id, e.currentTarget);
            }}>{m}</button>
        );
      };
      // 角标多时可以逐个换行，但第一个和句末那个字粘在一起，不会单独掉到下一行
      const chars = Array.from(b.text);
      const last = b.marks.length ? chars.pop() || "" : "";
      out.push(
        <p key={i} className={"ws-sent" + (b.inference ? " inf" : "")} data-claim={b.claim_id || undefined} data-section={section || undefined}>
          {chars.join("")}
          {b.marks.length > 0 && <span className="ws-nobr">{last}{mark(b.marks[0])}</span>}
          {b.marks.slice(1).map(mark)}
        </p>,
      );
    }
  });

  return (
    <div className="ws-report-wrap">
      {canEdit && <div className="ws-tip"><IconEdit size={13} />选中文字可以说怎么改，悬停角标看来源</div>}
      <article ref={article} className="ws-report" onMouseUp={() => setTimeout(capture, 0)}
        onKeyUp={(e) => e.shiftKey && capture()}>{out}</article>
      {pop && byId[pop.sid] && (
        <SourcePop boxRef={popRef} s={byId[pop.sid]} n={num[pop.sid]} rect={pop.rect} st={st} canEdit={canEdit} onEnter={keepPop} onLeave={hidePop}
          onClose={closePop} onExclude={() => { setPop(null); act.exclude(pop.sid); }} />
      )}
      {quote && canEdit && <QuoteBox q={quote} onOpen={() => setQuote({ ...quote, open: true })} onClose={closeQuote}
        onSubmit={(t) => { closeQuote(); act.instruct(t, { text: quote.text, claim_ids: quote.claim_ids, section_ids: quote.section_ids }); }} />}
    </div>
  );
}

// 填“怎么改”的时候选区会被输入框抢走，用 CSS 高亮把选中的文字留着（浏览器不支持就算了）
function useHighlight(range) {
  useEffect(() => {
    if (!range || !globalThis.CSS?.highlights || typeof Highlight === "undefined") return undefined;
    CSS.highlights.set("ws-quote", new Highlight(range));
    return () => CSS.highlights.delete("ws-quote");
  }, [range]);
}

// 浮层贴着触发它的元素放：下面放不下就放上面，左右不出窗口
function floatStyle(rect, width, height) {
  const left = Math.max(12, Math.min(rect.left - 16, innerWidth - width - 12));
  const below = rect.bottom + 8 + height < innerHeight;
  return below ? { left, top: rect.bottom + 8, width } : { left, bottom: innerHeight - rect.top + 8, width };
}

function SourcePop({ boxRef, s, n, rect, st, canEdit, onEnter, onLeave, onClose, onExclude }) {
  const sole = soleClaims(st, s.id).length;
  const issue = st.time_issues?.[s.id];
  const next = (st.versions?.at(-1)?.n || 0) + 1;
  return (
    <div ref={boxRef} className="ws-pop" style={floatStyle(rect, 340, 240)} role="dialog" aria-label={n ? `来源 ${n}` : "来源"}
      onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="ws-pop-top">
        {n && <span className="lb">{n}</span>}
        <span className={"ws-badge " + s.status}>{SSTATUS[s.status]}</span>
        <span className={"ws-badge " + (s.excerpt_kind === "body" ? "body" : "summary")}>{s.excerpt_kind === "body" ? "读到正文" : "只读到搜索摘要"}</span>
      </div>
      <div className="ws-pop-title">{sourceName(s)}</div>
      <div className="ws-pop-meta"><Favicon url={s.url} />{domain(s.url)}{s.published_at ? ` · 发布 ${s.published_at.slice(0, 10)}` : ""}</div>
      {s.excerpt && <div className="ws-pop-excerpt">{s.excerpt.length > 180 ? s.excerpt.slice(0, 180) + "…" : s.excerpt}</div>}
      {issue && <div className="ws-pop-issue"><IconWarn size={12} />发布时间可能不在范围内：{issue}</div>}
      <div className="ws-pop-actions">
        <a href={s.url} target="_blank" rel="noreferrer" className="ws-btn ghost sm"><IconExternal size={13} />打开原文</a>
        {canEdit && s.status === "adopted" && <button className="ws-btn danger sm" onClick={onExclude}><IconX size={13} />排除这条来源</button>}
      </div>
      {canEdit && s.status === "adopted" && (
        <div className="ws-pop-note">{sole ? `只靠它的 ${sole} 条论断会移出报告，出第 ${next} 版` : `每条用到它的论断都还有别的来源，排除后出第 ${next} 版`}</div>
      )}
    </div>
  );
}

function QuoteBox({ q, onOpen, onClose, onSubmit }) {
  const [text, setText] = useState("");
  const box = useRef(null);
  useEffect(() => {
    const down = (e) => !box.current?.contains(e.target) && onClose();
    const key = (e) => e.key === "Escape" && onClose();
    addEventListener("mousedown", down);
    addEventListener("keydown", key);
    return () => { removeEventListener("mousedown", down); removeEventListener("keydown", key); };
  }, [onClose]);
  const where = q.claim_ids.length ? `${q.claim_ids.length} 条论断` : q.section_ids.length ? "这一节" : null;
  if (!q.open) {
    return (
      <div ref={box} className="ws-quote-pill" style={floatStyle(q.rect, 96, 40)}>
        <button onMouseDown={(e) => e.preventDefault()} onClick={onOpen}><IconEdit size={13} />改这里</button>
      </div>
    );
  }
  const send = () => text.trim() && onSubmit(text.trim());
  return (
    <div ref={box} className="ws-quote" style={floatStyle(q.rect, 360, 130)} role="dialog" aria-label="改选中的文字">
      <div className="ws-quote-head">改选中的这段{where ? `（涉及 ${where}）` : ""}，其他已确认的内容不动</div>
      <div className="ws-quote-text">「{q.text.length > 60 ? q.text.slice(0, 60) + "…" : q.text}」</div>
      <div className="ws-card-input">
        <input autoFocus value={text} placeholder="例如：说明这是哪一年的数据" aria-label="怎么改"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()} />
        <button className="ws-send" aria-label="提交修改" disabled={!text.trim()} onClick={send}><IconArrowUp size={14} /></button>
      </div>
    </div>
  );
}

function RewriteBox({ sec, onCancel, onSubmit }) {
  const [text, setText] = useState("");
  return (
    <div className="ws-rewrite">
      <div className="ws-rewrite-head">「{sec.title}」怎么重写？只按这一节已确认的论断重写，别的节不动</div>
      <div className="ws-choices">
        {REWRITE_PRESETS.map((p) => <button key={p} className="ws-chip" onClick={() => onSubmit(p)}>{p}</button>)}
      </div>
      <div className="ws-card-input">
        <input value={text} placeholder="其他要求，不填就按原要求重写" aria-label="重写要求" onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => (e.key === "Escape" ? onCancel() : e.key === "Enter" && !e.nativeEvent.isComposing && onSubmit(text.trim()))} />
        <button className="ws-btn primary sm" onClick={() => onSubmit(text.trim())}>重写</button>
        <button className="ws-btn ghost sm" onClick={onCancel}>取消</button>
      </div>
    </div>
  );
}

// ---------------- 论断 ----------------

function ClaimList({ st, canEdit, act }) {
  const claims = st.claims || [];
  const [editing, setEditing] = useState(null);
  const sections = st.outline || [];
  const num = sourceNumbers(reportBlocks(st));
  const byId = Object.fromEntries((st.sources || []).map((s) => [s.id, s]));
  // 报告里引用的来源写编号；排除了的、这版没引用的没有编号，写网站名
  const ref = (id) => num[id] ? <span key={id}>{num[id]}</span>
    : <span key={id} className="off" title={byId[id] ? sourceName(byId[id]) : ""}>{byId[id] ? domain(byId[id].url) : id}</span>;
  return (
    <div className="ws-claims">
      {sections.map((sec) => {
        const cs = claims.filter((c) => c.outline_id === sec.id);
        if (!cs.length) return null;
        return (
          <div key={sec.id} className="ws-claim-group">
            <div className="ws-claim-sec">{sec.title}</div>
            {cs.map((c) => (
              <div key={c.id} className={"ws-claim" + (c.orphaned ? " orphan" : "")}>
                <div className="ws-claim-top">
                  <span className={"ws-badge " + c.kind}>{CKIND[c.kind]}</span>
                  {c.orphaned ? <span className="ws-badge excluded">失去来源</span> : <span className={"ws-badge " + c.status}>{CSTATUS[c.status]}</span>}
                  <span className="ids">{[...c.source_ids].sort(byNumber(num)).map(ref)}</span>
                  {canEdit && !c.orphaned && editing !== c.id && <button className="ws-link" onClick={() => setEditing(c.id)}><IconEdit size={12} />改表述</button>}
                </div>
                {editing === c.id
                  ? <ClaimEdit c={c} onCancel={() => setEditing(null)} onSave={(t) => { setEditing(null); if (t !== c.statement) act.editClaim(c.id, t); }} />
                  : <div className="ws-claim-text">{c.statement}</div>}
              </div>
            ))}
          </div>
        );
      })}
      {canEdit && <p className="ws-muted">改过的表述模型不再碰，改完出新一版。</p>}
    </div>
  );
}

function ClaimEdit({ c, onCancel, onSave }) {
  const [text, setText] = useState(c.statement);
  const ref = useRef(null);
  useLayoutEffect(() => {
    const el = ref.current;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [text]);
  return (
    <div className="ws-claim-edit">
      <textarea ref={ref} autoFocus value={text} aria-label="论断的新表述" onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onCancel();
          else if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) onSave(text.trim());
        }} />
      <div className="row">
        <button className="ws-btn ghost sm" onClick={onCancel}>取消</button>
        <button className="ws-btn primary sm" disabled={!text.trim()} onClick={() => onSave(text.trim())}>保存，出新一版</button>
      </div>
    </div>
  );
}

// ---------------- 来源 ----------------

function SourceList({ st, canEdit, act }) {
  const [showAll, setShowAll] = useState(false);
  const sources = st.sources || [];
  const num = sourceNumbers(reportBlocks(st));
  const order = byNumber(num);
  const groups = [
    ["adopted", "报告引用的", sources.filter((s) => s.status === "adopted").sort((a, b) => order(a.id, b.id))],
    ["excluded", "你排除的", sources.filter((s) => s.status === "excluded")],
  ];
  const rest = sources.filter((s) => s.status === "candidate");
  const row = (s) => {
    const sole = s.status === "adopted" ? soleClaims(st, s.id).length : 0;
    return (
      <li key={s.id} className={"ws-src " + s.status}>
        <span className={"lb" + (num[s.id] ? "" : " none")}>{num[s.id]}</span>
        <div className="body">
          <a href={s.url} target="_blank" rel="noreferrer" className="t" title={sourceName(s)}>{sourceName(s)}</a>
          <div className="meta">
            <Favicon url={s.url} />{domain(s.url)}{s.published_at ? ` · 发布 ${s.published_at.slice(0, 10)}` : ""} · {s.excerpt_kind === "body" ? "读到正文" : "只读到搜索摘要"}
            {st.time_issues?.[s.id] && <span className="warn"> · 发布时间可能不在范围内</span>}
          </div>
        </div>
        {canEdit && s.status === "adopted" && (
          <button className="ws-link danger" title={sole ? `只靠它的 ${sole} 条论断会移出报告` : "每条用到它的论断都还有别的来源"} onClick={() => act.exclude(s.id)}>排除</button>
        )}
      </li>
    );
  };
  return (
    <div className="ws-sources">
      {groups.map(([k, name, xs]) => xs.length > 0 && (
        <div key={k} className="ws-src-group">
          <div className="ws-claim-sec">{name} {xs.length}</div>
          <ul>{xs.map(row)}</ul>
        </div>
      ))}
      {rest.length > 0 && (
        <div className="ws-src-group">
          <button className="ws-link" onClick={() => setShowAll(!showAll)}>{showAll ? "收起" : `还有 ${rest.length} 条搜到但没用上的候选来源`}</button>
          {showAll && <ul>{rest.map(row)}</ul>}
        </div>
      )}
    </div>
  );
}

// ---------------- 底栏：校验和计数 ----------------

function PanelFoot({ st }) {
  const checks = st.checks || [];
  const failed = checks.filter((c) => !c.passed);
  const kept = (st.claims || []).filter((c) => !c.orphaned);
  const facts = kept.filter((c) => c.kind === "fact").length;
  return (
    <footer className="ws-panel-foot">
      {checks.length > 0 && (failed.length
        ? <span className="warn" title={failed.map((c) => `${checkLabel(c)}：${c.detail || "没通过"}`).join("\n")}><IconWarn size={13} />{failed.length} 项校验要留意：{failed.map(checkLabel).join("、")}</span>
        : <span className="ok"><IconShield size={13} />{checks.length} 项校验全过</span>)}
      <span className="counts">事实 {facts} · 推断 {kept.length - facts}</span>
    </footer>
  );
}
