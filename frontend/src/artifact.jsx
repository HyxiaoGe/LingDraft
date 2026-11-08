// 模型写的文件（Artifact，2026-11-08 Sean 定，方案见 docs/工作区和Artifact方案.md）：
// - 正文下面的文件卡片（FileCards）：一份文件在一轮里一张，写成了的是这一轮最后那一版（第 N 版 · 这版改了什么），正在写的跟着出字数，没写成的照实写；
// - 右栏看文件（FilePanel）：桌面和对话分栏（窄的桌面盖在对话上），手机全屏、左上角返回。顶上是标题、版本切换、复制、下载、关闭；
//   文档和正文一样渲染（来源角标能点），网页在隔离的 iframe 里预览、能切到源码，代码带行号和高亮；选中一段能引用到输入框，接着说怎么改。
// 边写边出现：用户在桌面上看着这一轮生成时自动打开、跟着出字（见 workspace.jsx 的 onEvent，判断记在那里，不跟着组件切换丢掉）；
// 翻历史、刷新以后不自动打开。只能看：不能直接改、不能恢复旧版、不做对比（2026-11-08 Sean 定第一批先不做）
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { threadFileText } from "./api.js";
import { CodeBlock } from "./code-block.jsx";
import { IconBack, IconCheck, IconClose, IconCopy, IconDownload, IconQuote, IconWarn, Spinner } from "./icons.jsx";
import { useLayer } from "./layers.js";
import { Markdown } from "./markdown.jsx";
import { NavContext } from "./nav.jsx";
import { CITE, citeNumbers, copyText, itemKey, num, sourceName, tidyTyping } from "./research.js";
import { AnswerSources, Mark } from "./sources.jsx";
import styles from "./artifact.module.css";

const ext = (name) => (/\.([A-Za-z0-9]{1,8})$/.exec(name || "")?.[1] || "").toLowerCase();
// 角标上的字：文档 MD、网页 HTML，代码写扩展名（最多 4 个字母）
const badgeText = (type, name) => (type === "markdown" ? "MD" : type === "html" ? "HTML" : (ext(name) || "code").slice(0, 4).toUpperCase());
function Badge({ type, name, busy }) {
  if (busy) return <span className={styles.badge} data-kind="busy" aria-hidden="true"><Spinner size={14} /></span>;
  return <span className={styles.badge} data-kind={type || "markdown"} aria-hidden="true">{badgeText(type, name)}</span>;
}

const isWrite = (i) => i.type === "step" && i.kind === "write";

// 这段对话里写成了的各版：第一版的编号 → 各版（按第几版从小到大）。重新回答时旧回答的那几步不算（live.hidden，后端已经删了）。
// 一轮里一份文件只出一版：同一轮里又改了的，后写的顶替先写的、版本号一样（先写的那版后端已经删了，见 app/tools/write_file.py），按版本号留后面那个
export function fileVersions(st, live) {
  const out = new Map();
  const add = (it) => {
    if (!isWrite(it) || !it.file || live?.hidden?.has(itemKey(it))) return;
    if (!out.has(it.file.series)) out.set(it.file.series, new Map());
    out.get(it.file.series).set(it.file.version, it.file);
  };
  for (const t of st?.turns || []) t.items.forEach(add);
  live?.items?.forEach(add);
  return new Map([...out].map(([series, byVersion]) => [series, [...byVersion.values()].sort((a, b) => a.version - b.version)]));
}

// 写文件的那一步（按调用 id）：实时收到的比存下来的新
function findStep(st, live, id) {
  const hit = (items) => items?.find((i) => isWrite(i) && i.id === id);
  const l = hit(live?.items);
  if (l) return l;
  for (const t of st?.turns || []) {
    const s = hit(t.items);
    if (s) return s;
  }
  return null;
}

// 一份文件的标题、类型：写成的那版有；还没写成的，改文件的按改的那份（最新一版），新建的按收到的参数
const latestOf = (versions, series) => (series && versions.get(series)?.at(-1)) || null;

// 这一轮的卡片：每份文件一张，按第一次出现的先后。key 是那份文件（第一版的编号）；新建的还没写成时用调用 id。
// 一份文件这一轮写成了的，后面没写成的那次不盖住它（卡片还是写成的那一版）；还在写的盖住（看得到在改）
export function turnCards(items, drafts, versions) {
  const cards = new Map();
  const put = (key, card) => {
    const prev = cards.get(key);
    if (prev?.file && !card.file && !card.busy) return;
    cards.set(key, { ...card, file: card.file || (card.busy ? prev?.file : null) });
  };
  for (const s of items) {
    if (!isWrite(s)) continue;
    const key = s.file?.series || s.target || s.id;
    const base = latestOf(versions, s.target);
    put(key, { key, call: s.id, step: s, file: s.file || null, busy: !!s.pending, edit: s.edit,
      title: s.file?.title || s.text || base?.title || "", type: s.file?.type || base?.type || "", name: s.file?.name || base?.name || "" });
  }
  for (const d of Object.values(drafts || {})) {
    if (items.some((i) => isWrite(i) && i.id === d.id)) continue; // 已经有那一步了：以它为准
    const key = d.file_id || d.id;
    const base = latestOf(versions, d.file_id);
    put(key, { key, call: d.id, draft: d, busy: true, edit: d.name === "edit_file",
      title: d.title || base?.title || "", type: d.type || base?.type || "", name: base?.name || "" });
  }
  return [...cards.values()];
}

// 正文下面的文件卡片。open(目标)：在右栏打开；current：右栏正开着的（那张标出来）
export function FileCards({ cards, running, open, current }) {
  if (!cards.length) return null;
  return <div className={styles.cards}>{cards.map((c) => <Card key={c.key} c={c} running={running} open={open} current={current} />)}</div>;
}

function Card({ c, running, open, current }) {
  const busy = running && c.busy;
  const failed = !c.file && !c.busy && c.step && !c.step.ok;
  const title = c.title || (c.edit ? "文件" : "新文件");
  let sub;
  if (c.file) sub = `第 ${c.file.version} 版${c.file.note ? ` · ${c.file.note}` : ""}`;
  else if (busy && c.draft) sub = c.edit ? (c.draft.text ? `正在重写 · ${num(c.draft.text.length)} 字` : "正在修改…") : `正在写${c.draft.text ? ` · ${num(c.draft.text.length)} 字` : "…"}`;
  else if (busy) sub = "正在保存…";
  else if (c.busy) sub = "没有完成";
  else sub = c.step?.message || "没写成";
  // 点开：写成的看那一版；还在写的看正在写的；没写成的改文件看那份文件现在的样子，新建的没东西可看
  const target = c.file ? { series: c.file.series, id: c.file.id } : busy ? { call: c.call } : c.edit && c.step?.target ? { series: c.step.target } : null;
  const on = !!current && !!target && (current.call ? current.call === c.call : c.file ? current.id === c.file.id || (!current.id && current.series === c.file.series) : current.series === target.series);
  const body = (
    <>
      <Badge type={c.type} name={c.name} busy={busy} />
      <span className={styles.cardText}>
        <span className={styles.cardTitle}>{title}</span>
        <span className={styles.cardSub + (failed ? " " + styles.bad : "")}>{failed && <IconWarn size={12} />}{sub}</span>
      </span>
    </>
  );
  return (
    <div className={styles.card + (on ? " " + styles.on : "") + (failed ? " " + styles.failed : "")} title={title}>
      {target ? <button className={styles.cardInner} aria-label={`打开 ${title}`} aria-pressed={on} onClick={() => open(target)}>{body}</button>
        : <div className={styles.cardInner}>{body}</div>}
    </div>
  );
}

// 一版的全文：从后端取，各版内容不会变，取过的记着（这次打开的页面里）
const texts = new Map();
function useFileText(tid, id) {
  const [, bump] = useState(0);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setError(null);
    if (!tid || !id || texts.has(id)) return undefined;
    const ctl = new AbortController();
    threadFileText(tid, id, ctl.signal).then((t) => { texts.set(id, t); bump((n) => n + 1); })
      .catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => ctl.abort();
  }, [tid, id, tries]);
  return { text: id ? texts.get(id) ?? null : null, error, retry: () => setTries((n) => n + 1) };
}

// 右栏要显示什么：target 是 {call}（正在写的、刚写完的那次调用）或 {series, id}（某份文件的某一版，没给 id 是最新一版）
function resolve(target, st, live, versions) {
  const step = target.call ? findStep(st, live, target.call) : null;
  const draft = target.call ? live?.drafts?.[target.call] || null : null;
  const series = target.series || step?.file?.series || step?.target || draft?.file_id || null;
  const list = (series && versions.get(series)) || [];
  // 写成了：这一轮后面又改了的，看顶替它的那版（同一版本号）
  if (step?.file) return { mode: "file", file: list.find((f) => f.id === step.file.id) || list.find((f) => f.version === step.file.version) || step.file, list, series };
  // 没写成（收到的草稿一轮结束才清，不能再当在写），或者这一轮已经结束、那一步还挂着（停下了、断了）
  if (step && (!step.pending || !live)) return { mode: "failed", step, base: list.at(-1) || null, list, series };
  if (target.call && (draft || step)) return { mode: "draft", draft, step, base: list.at(-1) || null, list, series };
  if (target.call) return { mode: "gone", list, series };
  const file = target.id ? list.find((f) => f.id === target.id) : list.at(-1);
  return file ? { mode: "file", file, list, series } : { mode: "gone", list, series };
}

// 下载、复制的文字：文档里的来源 id 换成编号，文末附“参考来源”（和导出对话一样写成 Markdown 链接）；网页、代码原样
export function fileExport(file, text, sources) {
  if (file.type !== "markdown") return text;
  const nums = citeNumbers([text], sources);
  const body = text.replace(CITE, (_, sid) => (nums[sid] ? `[${nums[sid]}]` : "")).trimEnd();
  const used = Object.entries(nums).map(([sid, n]) => `${n}. [${sourceName(sources[sid]).replace(/[[\]]/g, "")}](${sources[sid].url})`);
  return used.length ? `${body}\n\n## 参考来源\n\n${used.join("\n")}\n` : `${body}\n`;
}

const MIME = { markdown: "text/markdown", html: "text/html" };
function save(file, text) {
  const url = URL.createObjectURL(new Blob([text], { type: `${MIME[file.type] || "text/plain"};charset=utf-8` }));
  const a = Object.assign(document.createElement("a"), { href: url, download: file.name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 选中的一段引用到输入框：“《标题》里的这段：”下面一段引用，空一行接着写怎么改。太长的只引开头（模型凭开头找得到是哪段）
const QUOTE_MAX = 1500;
export function quoteText(title, text) {
  const cut = text.length > QUOTE_MAX ? `${text.slice(0, QUOTE_MAX)}…` : text;
  const lines = cut.split("\n").map((l) => l.trimEnd());
  while (lines.length && !lines.at(-1)) lines.pop();
  return `《${title}》里的这段：\n${lines.map((l) => (l ? `> ${l}` : ">")).join("\n")}\n\n`;
}

// 右栏里选中的字（只认正文区里的，网页预览在 iframe 里选不到）：{text, top, left}，位置是相对正文区内容的
function useSelected(ref, enabled) {
  const [sel, setSel] = useState(null);
  useEffect(() => {
    setSel(null);
    if (!enabled) return undefined;
    const on = () => {
      const s = getSelection();
      const el = ref.current;
      if (!el || !s || s.isCollapsed || !s.rangeCount) return setSel(null);
      const r = s.getRangeAt(0);
      if (!el.contains(r.commonAncestorContainer)) return setSel(null);
      const text = s.toString().trim();
      if (!text) return setSel(null);
      const box = r.getBoundingClientRect();
      const host = el.getBoundingClientRect();
      setSel({ text, top: box.bottom - host.top + el.scrollTop + 8, left: Math.max(8, Math.min(box.left - host.left, host.width - 96)) });
    };
    document.addEventListener("selectionchange", on);
    return () => document.removeEventListener("selectionchange", on);
  }, [ref, enabled]);
  return sel;
}

const TYPE_LABEL = { markdown: "文档", html: "网页", code: "代码" };

// 右栏。target：开着的是什么（见 resolve）；onTarget：换一版；onClose：关上。tid、st、live 是这段对话的
// focus：用户点开的（不是自动打开的）每次加一，光标挪到右栏的标题上，键盘、读屏接着从这里读
export function FilePanel({ tid, st, live, target, onTarget, onClose, focus }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const versions = fileVersions(st, live);
  const r = resolve(target, st, live, versions);
  const base = r.file || r.base || null;
  const draft = r.draft;
  const shownId = r.mode === "file" ? r.file.id : r.mode === "draft" && draft?.text ? null : base?.id;
  const { text: fetched, error, retry } = useFileText(tid, shownId);
  const streaming = r.mode === "draft" && !!draft?.text; // 新建、整篇重写：内容边收边显示
  // 刚写完、这一版还没取回来：先接着显示刚才写的那些，不闪一下转圈
  const written = target.call && r.mode === "file" ? live?.drafts?.[target.call]?.text : null;
  const text = streaming ? draft.text : fetched ?? (written || null);
  const type = r.mode === "draft" ? draft?.type || base?.type || "markdown" : base?.type || "markdown";
  const title = (r.mode === "draft" ? draft?.title : "") || base?.title || r.step?.text || (r.mode === "draft" && draft?.name === "create_file" ? "新文件" : "文件");
  const sources = st?.sources;
  const [view, setView] = useState("preview"); // 网页：预览 / 源码
  const bodyRef = useRef(null);
  const stick = useRef(true); // 正在写：贴着底的跟着往下走，往上翻着看的不动
  const sel = useSelected(bodyRef, !(type === "html" && view === "preview" && !streaming) && text != null);
  useLayer(mobile, onClose); // 手机全屏时 Esc（外接键盘）收起；桌面上 Esc 留给停止回答

  const titleRef = useRef(null);
  useEffect(() => { if (focus) titleRef.current?.focus({ preventScroll: true }); }, [focus]);
  // 换了文件、换了一版：回到顶上；正在写的从底下跟着
  const shownKey = target.call || `${r.series}:${target.id || ""}`;
  useLayoutEffect(() => {
    stick.current = true;
    const el = bodyRef.current;
    if (el) el.scrollTop = streaming ? el.scrollHeight : 0;
  }, [shownKey]);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (streaming && stick.current && el) el.scrollTop = el.scrollHeight;
  }, [text, streaming]);

  const done = r.mode === "file" && fetched != null;
  const out = done ? fileExport(r.file, fetched, sources) : null;
  const [copied, setCopied] = useState(null);
  useEffect(() => setCopied(null), [shownId]);
  const copy = async () => {
    try {
      await copyText(out);
      setCopied("ok");
    } catch {
      setCopied("fail");
    }
    setTimeout(() => setCopied(null), 2000);
  };
  const quote = () => {
    if (!sel) return;
    dispatchEvent(new CustomEvent("ws-quote", { detail: quoteText(title, sel.text) }));
    getSelection()?.removeAllRanges();
    if (mobile) onClose(); // 手机上右栏盖着输入框：收起来接着写
  };

  // 顶上第二行：第几版、这版改了什么；正在写的说在写
  let status = null;
  if (r.mode === "draft") {
    const editing = draft ? draft.name === "edit_file" : r.step?.edit;
    const doing = !draft ? "正在保存…" : editing ? (draft.text ? "正在重写" : `正在修改${draft.change_note ? `：${draft.change_note}` : "…"}`) : "正在写";
    status = <span className={styles.live}><Spinner size={12} />{doing}{draft?.text ? ` · ${num(draft.text.length)} 字` : ""}</span>;
  } else if (r.mode === "failed") {
    status = <span className={styles.bad}><IconWarn size={12} />{r.step.pending ? "没有完成" : r.step.message || "没写成"}{r.base ? `，下面是第 ${r.base.version} 版` : ""}</span>;
  }
  const versionPick = r.mode === "file" && r.list.length > 1 && (
    <select className={styles.version} aria-label="看哪一版" value={r.file.id} onChange={(e) => onTarget({ series: r.series, id: e.target.value })}>
      {[...r.list].reverse().map((f, k) => (
        <option key={f.id} value={f.id}>{`第 ${f.version} 版${k === 0 ? "（最新）" : ""}${f.note ? ` · ${f.note.slice(0, 24)}${f.note.length > 24 ? "…" : ""}` : ""}`}</option>
      ))}
    </select>
  );
  const facts = r.mode === "file" && (r.list.length > 1 ? null : <span>第 {r.file.version} 版{r.file.note ? ` · ${r.file.note}` : ""}</span>);

  let body;
  if (r.mode === "gone") body = <p className={styles.note}>这份文件已经不在了（重新回答时那一轮写的会一起删掉）。</p>;
  else if (r.mode === "failed" && !r.base) body = <p className={styles.note}>这次没有写成，可以让模型再写一次。</p>;
  else if (error && !streaming) body = (
    <div className={styles.note}>
      {error === "gone" ? "这一版已经不在了。" : "内容没取到。"}
      {error !== "gone" && <button className="ws-btn sm" onClick={retry}>再试一次</button>}
    </div>
  );
  else if (text == null) body = r.mode === "draft" && !draft?.text && draft?.name !== "edit_file"
    ? <p className={styles.note}>正在写…</p> : <div className={styles.loading} role="status" aria-label="正在打开"><Spinner size={16} /></div>;
  else if (type === "markdown") body = <Doc text={streaming ? tidyTyping(text) : text} sources={sources} done={!streaming} />;
  else if (type === "html" && view === "preview" && !streaming) body = <iframe className={styles.frame} title={`${title}（网页预览）`} sandbox="allow-scripts allow-popups" referrerPolicy="no-referrer" srcDoc={text} />;
  else body = <CodeBlock lang={type === "html" ? "html" : base?.lang || ""} text={text} bare still={streaming} />;

  const pad = type === "markdown" || r.mode === "gone" || (r.mode === "failed" && !r.base) || error || text == null;
  return (
    <aside className={styles.panel} aria-label={`文件：${title}`} data-mobile={mobile || undefined}>
      <header className={styles.head}>
        {mobile && <button className="ws-icon-btn" aria-label="返回对话" onClick={onClose}><IconBack size={20} /></button>}
        <Badge type={type} name={base?.name} />
        <div className={styles.headText}>
          <h2 ref={titleRef} tabIndex={-1} className={styles.title} title={title}>{title}</h2>
          <div className={styles.facts}>{status || facts || (!versionPick && <span>{TYPE_LABEL[type]}</span>)}{versionPick}</div>
        </div>
        <div className={styles.acts}>
          {type === "html" && !streaming && text != null && (
            <div className={styles.seg} role="group" aria-label="网页怎么看">
              <button aria-pressed={view === "preview"} onClick={() => setView("preview")}>预览</button>
              <button aria-pressed={view === "source"} onClick={() => setView("source")}>源码</button>
            </div>
          )}
          <button className="ws-icon-btn" aria-label={copied === "ok" ? "已复制" : copied === "fail" ? "没复制成" : "复制全文"} title={copied === "ok" ? "已复制" : copied === "fail" ? "没复制成" : "复制全文"}
            disabled={!out} onClick={copy}>{copied === "ok" ? <IconCheck size={17} /> : <IconCopy size={17} />}</button>
          <button className="ws-icon-btn" aria-label={`下载 ${r.file?.name || ""}`} title={r.file ? `下载 ${r.file.name}` : "写完后可以下载"} disabled={!out}
            onClick={() => save(r.file, out)}><IconDownload size={17} /></button>
          {!mobile && <button className="ws-icon-btn" aria-label="关闭" title="关闭" onClick={onClose}><IconClose size={17} /></button>}
        </div>
      </header>
      <div ref={bodyRef} className={styles.body + (pad ? " " + styles.padded : "") + (type === "html" && view === "preview" && !streaming && text != null ? " " + styles.framed : "")}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {body}
        {sel && (
          <button className={styles.quote + (mobile ? " " + styles.quoteBar : "")} style={mobile ? undefined : { top: sel.top, left: sel.left }}
            onPointerDown={(e) => e.preventDefault()} onClick={quote}>
            <IconQuote size={14} />引用到输入框
          </button>
        )}
      </div>
    </aside>
  );
}

// 文档：和正文一样渲染，来源角标能点（只认这段对话里真有的来源），文末是这份文档用到的来源
function Doc({ text, sources, done }) {
  const nums = citeNumbers([text], sources);
  const cite = (sid) => <Mark n={nums[sid]} s={sources?.[sid]} nums={nums} sources={sources} />;
  return (
    <div className={styles.doc}>
      <Markdown text={text} cite={cite} download={done} ready={done} />
      {done && <AnswerSources nums={nums} sources={sources} />}
    </div>
  );
}
