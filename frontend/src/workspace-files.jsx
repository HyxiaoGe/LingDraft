// 工作区（2026-11-08 Sean 定，方案见 docs/工作区和Artifact方案.md 第 5 条）：一段对话里的所有文件。
// - 顶栏的“文件 N”（FilesButton）在右栏打开文件列表（FileList）：生成的文件（每份最新一版，这一轮正在新建的跟着出字数）、上传的文件、
//   图片（发的、画的，按先后）。文件点开在右栏看，看完左上角回列表；图片看大图、能下载。
// - 用户发的文件在右栏看（SentPanel，2026-11-09 Sean 定）：PDF 在桌面上用浏览器自带的 PDF 查看器看原件；别的看抽出来的文字——
//   Markdown、Word 和正文一样排，PPT、表格每段一个小标题（第几张、哪张表哪几行），代码带行号和高亮，纯文字照原样。
//   手机上 PDF 点了直接下载（见 files.jsx 的 previewable）。太长的只显示前面一部分，底下能下载原件。选中一段能引用到输入框。
// - 这段对话里的 PPT（发的、命令做出来的；2026-11-12 Sean 定）：登录了的在桌面上先看排出来的页面（后端在沙箱里排成 PDF，
//   一张一页），顶上能切回文字；下载能选 PPT、PDF。游客、手机、资料库里的照旧看文字。
// - 网页（.html，发的、命令做出来的，比如 plotly 出的能点的图表；2026-11-12）：和模型写的网页一样先看页面（artifact.jsx），顶上能切到源码。
// - 资料库里的文件也在这里看（kb，2026-11-09）：从资料库页点开，或者点回答里引用的资料库来源——那样会翻到引用的那一段并标出来，
//   PDF 翻到那一页。
import { useContext, useEffect, useRef, useState } from "react";
import { downloadKnowledgeFile, downloadThreadFile, getKnowledgeFile, knowledgeFileBlob, knowledgeFileParts, threadFileBlob, threadFileExport, threadFileParts } from "./api.js";
import { CopyAll, FileCards, PanelShell, QuoteButton, SLIDE_FORMATS, SaveMenu, fileVersions, turnCards, useExport, useSelected } from "./artifact.jsx";
import { CodeBlock } from "./code-block.jsx";
import { outputsOf } from "./commands.jsx";
import { Badge as FileBadge, SentFile, fileFacts, partUnit } from "./files.jsx";
import { IconDownload, IconFolder, IconWarn, Spinner } from "./icons.jsx";
import { Viewer, drawn, imageAlt, imageFileName, imageUrl, imagesOf } from "./images.jsx";
import { Markdown } from "./markdown.jsx";
import { NavContext, typeNext } from "./nav.jsx";
import { previewDoc } from "./preview.js";
import { mergeItems } from "./research.js";
import pstyles from "./artifact.module.css";
import styles from "./workspace-files.module.css";

const EXTENSIONS = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };

// 这段对话里的文件：written 是生成的文件（artifact.jsx 的卡片，每份最新一版；这一轮正在新建、还没写成的也在），made 是跑命令产出的文件
// （不是图的，见 commands.jsx），sent 是上传的文件，
// images 是图片（{img, name, alt}，name 是下载用的文件名：画的和对话里图片卡片上下载的一样，发的叫“发的图-N”，跑命令产出的图表用它自己的文件名）。
// count 是一共几个（顶栏上写的）。
// title：对话标题（画的图下载的文件名用）
export function workspaceFiles(st, live, title) {
  const turns = st?.turns || [];
  const pending = !!live?.said && turns.length <= (live.turnsAtStart ?? 0); // 刚说的话还没出现在存下来的对话里（见 timeline.jsx 的 Turns）
  const rows = turns.map((t, i) => ({ t, items: live && !pending && i === turns.length - 1 ? mergeItems(t.items, live) : t.items }));
  if (pending) rows.push({ t: { files: live.docs?.map((d) => d.file).filter(Boolean) || [] }, items: live.items || [] });
  const versions = fileVersions(st, live);
  const written = [...versions].map(([series, list]) => {
    const f = list.at(-1);
    return { key: series, file: f, title: f.title, type: f.type, name: f.name };
  });
  if (live) written.push(...turnCards(rows.at(-1)?.items || [], live.drafts, versions).filter((c) => c.busy && !c.file && !c.edit));
  const sent = [];
  const made = [];
  const images = [];
  const seen = new Set();
  let drew = 0;
  let got = 0;
  for (const { t, items } of rows) {
    for (const f of t.files || []) {
      if (seen.has(f.id)) continue;
      seen.add(f.id);
      sent.push(f);
    }
    for (const img of t.images || []) {
      if (seen.has(img.id)) continue;
      seen.add(img.id);
      got += 1;
      images.push({ img, name: `发的图-${got}.${EXTENSIONS[img.mime_type] || "png"}`, alt: `发的第 ${got} 张图` });
    }
    for (const s of imagesOf(items)) {
      if (!drawn(s) || seen.has(s.image.id)) continue;
      seen.add(s.image.id);
      drew += 1;
      images.push({ img: s.image, name: imageFileName(title, drew, s.image.mime_type), alt: imageAlt(s.image.prompt) });
    }
    const out = outputsOf(items);
    for (const f of out.files) {
      if (seen.has(f.id)) continue;
      seen.add(f.id);
      made.push(f);
    }
    for (const f of out.charts) {
      if (seen.has(f.id)) continue;
      seen.add(f.id);
      images.push({ img: f, name: f.name, alt: `图表：${f.name}` });
    }
  }
  return { written, made, sent, images, count: written.length + made.length + sent.length + images.length };
}

// 顶栏上的“文件 N”：这段对话还没有文件时不出现。on：右栏开着文件列表（再点一下收起）
export function FilesButton({ count, on, onClick }) {
  const mobile = !!useContext(NavContext)?.mobile;
  if (!count) return null;
  return (
    <button className={styles.filesBtn} data-files-btn aria-pressed={on} aria-label={`这段对话的文件，${count} 个`} title="这段对话的文件" onClick={onClick}>
      <IconFolder size={mobile ? 20 : 16} />{!mobile && <span>文件</span>}<span className={styles.count}>{count}</span>
    </button>
  );
}

// 右栏的文件列表。onNav(目标)：在右栏换成那个（带 back，看完回列表）；title：对话标题；focus 见 artifact.jsx 的 PanelShell
export function FileList({ tid, st, live, title, onNav, onClose, focus }) {
  const ws = workspaceFiles(st, live, title);
  const [viewing, setViewing] = useState(null); // 看大图：{it, back}（back 是点开它的那张缩略图，收起后光标回去）
  const docs = ws.written.length + ws.made.length + ws.sent.length;
  const facts = [docs && `${docs} 个文件`, ws.images.length && `${ws.images.length} 张图`].filter(Boolean).join(" · ");
  const panel = { open: onNav, current: null };
  return (
    <PanelShell label="这段对话的文件" title="文件" badge={<span className={styles.headIcon} aria-hidden="true"><IconFolder size={17} /></span>}
      facts={facts && <span>{facts}</span>} onClose={onClose} focus={focus}>
      <div className={pstyles.body + " " + pstyles.padded}>
        {!ws.count && <p className={pstyles.note}>这段对话还没有文件。</p>}
        {ws.written.length + ws.made.length > 0 && (
          <Section title="生成的文件">
            <div className={styles.stack}>
              <FileCards wide cards={ws.written} running={!!live} open={(t) => onNav({ ...t, back: true })} current={null} />
              {ws.made.map((f) => <SentFile key={f.id} f={f} tid={tid} panel={panel} back />)}
            </div>
          </Section>
        )}
        {ws.sent.length > 0 && (
          <Section title="上传的文件">
            <div className={styles.stack}>{ws.sent.map((f) => <SentFile key={f.id} f={f} tid={tid} panel={panel} back />)}</div>
          </Section>
        )}
        {ws.images.length > 0 && (
          <Section title="图片">
            <div className={styles.grid}>{ws.images.map((it) => <Tile key={it.img.id} it={it} tid={tid} onOpen={(back) => setViewing({ it, back })} />)}</div>
          </Section>
        )}
      </div>
      {viewing && (
        <Viewer img={{ ...viewing.it.img, alt: viewing.it.alt }} src={imageUrl(tid, viewing.it.img)} download={viewing.it.name}
          onClose={() => { typeNext(viewing.back); setViewing(null); }} />
      )}
    </PanelShell>
  );
}

function Section({ title, children }) {
  return (
    <section className={styles.section}>
      <h3 className={styles.sectionTitle}>{title}</h3>
      {children}
    </section>
  );
}

// 一张图的方块缩略图（铺满裁切），点了看大图；取不到的（删掉的）说一声
function Tile({ it, tid, onOpen }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className={styles.tile}>
      {failed ? <span className={styles.gone}>图片取不到了</span> : (
        <button className={styles.tileBtn} aria-label={`看大图：${it.alt}`} onClick={(e) => onOpen(e.currentTarget)}>
          <img src={imageUrl(tid, it.img)} alt="" loading="lazy" onError={() => setFailed(true)} />
        </button>
      )}
    </div>
  );
}

// ---- 用户发的文件在右栏看 ----

// 抽出来的文字：从后端取，原件不会变，取过的记着（这次打开的页面里）。文件编号对话里的、资料库里的不会重
const parts = new Map();

// 文件从哪取：对话里发的（按对话编号）还是资料库里的。key 变了才重新取
const fromThread = (tid) => ({ key: tid, parts: (id, sig) => threadFileParts(tid, id, sig), blob: (id, sig) => threadFileBlob(tid, id, sig),
  download: (f) => downloadThreadFile(tid, f) });
const fromKnowledge = { key: "kb", parts: knowledgeFileParts, blob: knowledgeFileBlob, download: downloadKnowledgeFile };

function useParts(src, id) {
  const [, bump] = useState(0);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setError(null);
    if (!src.key || !id || parts.has(id)) return undefined;
    const ctl = new AbortController();
    src.parts(id, ctl.signal).then((d) => { parts.set(id, d); bump((n) => n + 1); })
      .catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => ctl.abort();
  }, [src.key, id, tries]); // eslint-disable-line react-hooks/exhaustive-deps -- src 每次是新的，key 一样就是同一处
  return { data: id ? parts.get(id) ?? null : null, error, retry: () => setTries((n) => n + 1) };
}

// PDF 原件：要带登录信息，取成 blob 给 iframe（浏览器自带的 PDF 查看器）。关上右栏就放掉，再开时浏览器缓存里有，不用再下一遍。
// PPT 排出来的页面也用它取（src 换成 pagesOf，后端转过的留着）。error：gone（不在了）、后端说的原因（见 NO_PAGES）、failed（别的）
function usePdf(src, id) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setUrl(null);
    setError(null);
    if (!src.key || !id) return undefined;
    const ctl = new AbortController();
    let made = null;
    src.blob(id, ctl.signal).then((b) => {
      if (ctl.signal.aborted) return;
      made = URL.createObjectURL(new Blob([b], { type: "application/pdf" }));
      setUrl(made);
    }).catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : e.detail?.code || "failed"); });
    return () => {
      ctl.abort();
      if (made) URL.revokeObjectURL(made);
    };
  }, [src.key, id, tries]); // eslint-disable-line react-hooks/exhaustive-deps -- 同上
  return { url, error, retry: () => setTries((n) => n + 1) };
}

// 网页的原件，给预览用：抽出来的文字太长时只有前面一部分，不能拿来当页面。多半是 UTF-8；不是的按 GB18030 认（老的中文网页）。
// 只给预览的 iframe 用，所以取回来就带上内存里的 localStorage（见 preview.js）；下载、看源码走的是别的接口，拿到的还是原件
function decodePage(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("gb18030").decode(bytes);
  }
}

function usePage(src, id) {
  const [got, setGot] = useState(null); // {id, html}
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setError(null);
    if (!src.key || !id) return undefined;
    const ctl = new AbortController();
    src.blob(id, ctl.signal).then((b) => b.arrayBuffer()).then((bytes) => { if (!ctl.signal.aborted) setGot({ id, html: previewDoc(decodePage(bytes)) }); })
      .catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => ctl.abort();
  }, [src.key, id, tries]); // eslint-disable-line react-hooks/exhaustive-deps -- 同上
  return { html: id && got?.id === id ? got.html : null, error, retry: () => setTries((n) => n + 1) };
}

// 这段对话里的 PPT 排成的 PDF（一张一页，看得到每一页排出来的样子；2026-11-12 Sean 定，见 docs/沙箱方案.md）：
// 后端在沙箱里现排一份，不经过模型；只有登录了的能用（和文档导出 PDF 一样）
const pagesOf = (tid) => ({ key: tid, blob: (id, sig) => threadFileExport(tid, id, "pdf", sig) });
// 页面没排出来时“预览”里说的话，按后端给的原因（app/api/export.py）；没列的说 noPages 里那句。第二项：再试一次有没有用
const NO_PAGES = {
  gone: ["这个文件已经不在了。", false],
  too_big: ["这份 PPT 太大，预览不了。", false],
  failed: ["这份 PPT 预览不了。", false],
  daily_limit: ["今天预览的次数用完了，明天再来。", false],
  busy: ["现在预览的人多，过一会儿再试。", true],
};
const noPages = (code) => NO_PAGES[code] || ["预览没取到。", true];
// 存成 PDF 没成时的说法（别的原因和文档的一样，见 artifact.jsx 的 EXPORT_ERRORS）
const SLIDE_EXPORT_ERRORS = { not_found: "这个文件已经不在了", too_big: "这份 PPT 太大，没转成", failed: "这份 PPT 没转成" };

const DOC_LIKE = new Set(["markdown", "docx"]); // 整篇当 Markdown 排（Word 抽出来的标题、表格已经是 Markdown）
const LINES = new Set(["code", "text"]); // 按行切的：接回去就是原文
const WEB = new Set(["html", "htm"]); // 网页：类型登记的是代码，按后缀认
const ext = (name) => (/\.([A-Za-z0-9]{1,8})$/.exec(name || "")?.[1] || "").toLowerCase();
const noCite = () => null;

// 复制、引用用的全文：按行切的接回原文；文档整篇；PPT、表格每段前面写段名
function fullText({ type, parts: ps }) {
  if (LINES.has(type)) return ps.map((p) => p.text).join("\n").replace(/\n+$/, ""); // 文件末尾的换行不多出一个空行
  if (DOC_LIKE.has(type)) return ps.map((p) => p.text).join("\n\n");
  return ps.map((p) => `${p.title}\n\n${p.text}`).join("\n\n");
}

// at：要翻到、标出来的第几段（资料库来源引用的那段）：每段分开排，那一段有底色。没有就照上面整篇排
function SentBody({ data, name, at }) {
  if (at) return <PartsBody data={data} at={at} />;
  if (data.type === "code") return <CodeBlock lang={ext(name)} text={fullText(data)} bare />;
  if (data.type === "text") return <pre className={styles.plain}>{fullText(data)}</pre>;
  if (DOC_LIKE.has(data.type)) return <div className={pstyles.doc}><Markdown text={fullText(data)} cite={noCite} /></div>;
  return (
    <div className={pstyles.doc}>
      {data.parts.map((p, k) => (
        <section key={k} className={styles.part}>
          <h3 className={styles.partTitle}>{p.title}</h3>
          {p.text.trim() ? <Markdown text={p.text} cite={noCite} /> : <p className={styles.empty}>这一{partUnit(data.type)}没有文字</p>}
        </section>
      ))}
    </div>
  );
}

function PartsBody({ data, at }) {
  const lines = LINES.has(data.type);
  const titled = !lines && !DOC_LIKE.has(data.type);
  return (
    <div className={lines ? styles.lines : pstyles.doc}>
      {data.parts.map((p, k) => (
        <section key={k} data-part={k + 1} className={styles.part + (k + 1 === at ? " " + styles.hit : "")}>
          {titled && <h3 className={styles.partTitle}>{p.title}</h3>}
          {lines ? <pre className={styles.plain}>{p.text}</pre>
            : p.text.trim() ? <Markdown text={p.text} cite={noCite} /> : <p className={styles.empty}>这一{partUnit(data.type)}没有文字</p>}
        </section>
      ))}
    </div>
  );
}

// 资料库里的文件在右栏看。从来源点开的只知道编号和第几段（f：{id, name?}），先取一次这个文件的类型、大小（PDF 要不要用原件看）。
// at：翻到第几段
const kbFiles = new Map();
export function KnowledgePanel({ f, at, onClose, focus }) {
  const [, bump] = useState(0);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  const known = f.type ? f : kbFiles.get(f.id);
  useEffect(() => {
    setError(null);
    if (known) return undefined;
    const ctl = new AbortController();
    getKnowledgeFile(f.id, ctl.signal).then((v) => {
      if (v.status !== "ready") return setError("gone");
      kbFiles.set(f.id, v.file);
      bump((n) => n + 1);
    }).catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => ctl.abort();
  }, [f.id, tries]); // eslint-disable-line react-hooks/exhaustive-deps
  if (known) return <SentPanel f={known} kb at={at} onClose={onClose} focus={focus} />;
  return (
    <PanelShell label={`资料库：${f.name || "文件"}`} title={f.name || "资料库的文件"} badge={<span className={styles.headBadge}><FileBadge name={f.name || ""} /></span>}
      facts={<span>资料库</span>} onClose={onClose} focus={focus}>
      <div className={pstyles.body + " " + pstyles.padded}>
        {error ? (
          <div className={pstyles.note}>
            {error === "gone" ? "这个文件已经从资料库里删掉了。" : "内容没取到。"}
            {error !== "gone" && <button className="ws-btn sm" onClick={() => setTries((n) => n + 1)}>再试一次</button>}
          </div>
        ) : <div className={pstyles.loading} role="status" aria-label="正在打开"><Spinner size={16} /></div>}
      </div>
    </PanelShell>
  );
}

// 右栏看用户发的一个文件（f：{id, name, type, size, pages}），命令产出的文件也在这里看。back：从文件列表点开的，左上角回列表。
// kb：资料库里的（不归哪段对话），at：翻到第几段（见 KnowledgePanel）。
// canExport：登录了，这段对话里的 PPT 能看排出来的页面、存成 PDF；onLogin：游客开登录弹窗（没有就是登录不了，PDF 不列）
export function SentPanel({ tid, f, back, kb = false, at = null, onNav, onClose, focus, canExport = false, onLogin }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const pdf = f.type === "pdf" && !mobile;
  const deck = f.type === "pptx" && !kb; // 这段对话里的 PPT：能排成 PDF
  const slides = deck && !mobile && canExport; // 右栏里能看排出来的页面（手机上的浏览器不在页面里显示 PDF）
  const web = f.type === "code" && WEB.has(ext(f.name)) && !at; // 网页能当页面看；要翻到引用的那一段时看文字
  const src = kb ? fromKnowledge : fromThread(tid);
  // PPT、网页怎么看：预览（排出来的页面 / 网页）/ 文字（源码）。没点过的先看预览，PPT 排不出来就看文字；点过的照点的来。换了文件重新算
  const [picked, setPicked] = useState(null); // {id, view}
  const pages = usePdf(pagesOf(tid), slides ? f.id : null);
  const view = picked?.id === f.id && (slides || web) ? picked.view : slides ? (pages.error ? "text" : "preview") : web ? "preview" : null;
  const paged = pdf || (slides && view === "preview"); // 用浏览器自带的 PDF 查看器看
  const live = web && view === "preview"; // 网页当页面看
  const text = useParts(src, paged || live ? null : f.id);
  const original = usePdf(src, pdf ? f.id : null);
  const page = usePage(src, live ? f.id : null);
  const sheet = pdf ? original : pages;
  const { data } = text;
  const error = paged ? sheet.error : live ? page.error : text.error;
  const retry = paged ? sheet.retry : live ? page.retry : text.retry;
  const ready = paged ? !!sheet.url : live ? page.html != null : !!data;
  const bodyRef = useRef(null);
  const sel = useSelected(bodyRef, !paged && !live && !!data);
  const [saving, setSaving] = useState(null); // busy / failed
  useEffect(() => setSaving(null), [f.id]);
  const { exporting, failed: exportFail, exportAs, clear: clearExportFail } = useExport(tid, SLIDE_EXPORT_ERRORS);
  // 换了文件回到顶上；要翻到某一段的，文字到了以后滚到那一段（PDF 的翻页在地址里）
  const shown = !!data && !paged && !live;
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const target = at && shown ? el.querySelector(`[data-part="${at}"]`) : null;
    if (target) target.scrollIntoView({ block: "start" });
    else el.scrollTop = 0;
  }, [f.id, at, shown]);
  const missing = at && data && !paged && !live && at > data.parts.length; // 引用的那段在只显示了前面一部分之后
  const download = async () => {
    if (saving === "busy") return;
    setSaving("busy");
    try {
      await src.download(f);
      setSaving(null);
    } catch {
      setSaving("failed");
    }
  };
  const dlLabel = saving === "busy" ? "正在下载…" : saving === "failed" ? "没下载成，再点一次" : `下载 ${f.name}`;
  const notMade = exportFail?.id === f.id ? exportFail.text : null;
  const acts = (
    <>
      {slides && (
        <div className={pstyles.seg} role="group" aria-label="PPT 怎么看">
          <button aria-pressed={view === "preview"} onClick={() => setPicked({ id: f.id, view: "preview" })}>预览</button>
          <button aria-pressed={view === "text"} onClick={() => setPicked({ id: f.id, view: "text" })}>文字</button>
        </div>
      )}
      {web && (
        <div className={pstyles.seg} role="group" aria-label="网页怎么看">
          <button aria-pressed={view === "preview"} onClick={() => setPicked({ id: f.id, view: "preview" })}>预览</button>
          <button aria-pressed={view === "text"} onClick={() => setPicked({ id: f.id, view: "text" })}>源码</button>
        </div>
      )}
      {!paged && !live && <CopyAll text={data ? fullText(data) : null} reset={f.id} />}
      {deck && (canExport || onLogin) ? (
        <SaveMenu file={f} formats={SLIDE_FORMATS} locked={!canExport}
          busy={saving === "busy" ? "正在下载…" : exporting?.id === f.id ? `正在导出 ${exporting.name}…` : null}
          onOpen={() => { clearExportFail(); setSaving(null); }} onSave={download} onExport={(to) => exportAs(f, to)} onLogin={onLogin} />
      ) : (
        <button className="ws-icon-btn" aria-label={dlLabel} title={dlLabel} aria-busy={saving === "busy" || undefined} onClick={download}>
          {saving === "busy" ? <Spinner size={15} /> : <IconDownload size={17} />}
        </button>
      )}
    </>
  );
  const facts = saving === "failed" ? <span className={pstyles.bad}>没下载成，再点一次</span>
    : notMade ? <span className={pstyles.bad} role="status"><IconWarn size={12} />{notMade}</span>
      : <span>{kb ? `资料库 · ${fileFacts(f)}` : fileFacts(f)}</span>;

  let body;
  if (error) {
    const [words, again] = paged && !pdf ? noPages(error) : [error === "gone" ? "这个文件已经不在了。" : "内容没取到。", error !== "gone"];
    body = (
      <div className={pstyles.note}>
        {words}
        {again && <button className="ws-btn sm" onClick={retry}>再试一次</button>}
      </div>
    );
  } else if (!ready) body = <div className={pstyles.loading} role="status" aria-label="正在打开"><Spinner size={16} /></div>;
  else if (paged) body = <iframe className={pstyles.frame} title={`${f.name}（${pdf ? "PDF" : "预览"}）`} src={pdf && at ? `${sheet.url}#page=${at}` : sheet.url} />;
  // 和模型写的网页一样关在 sandbox 里（artifact.jsx）：能跑脚本，碰不到灵稿这边的登录信息
  else if (live) body = <iframe className={pstyles.frame} title={`${f.name}（网页预览）`} sandbox="allow-scripts allow-popups" referrerPolicy="no-referrer" srcDoc={page.html} />;
  else body = (
    <>
      {missing && (
        <div className={styles.more + " " + styles.ahead}>
          <span>引用的第 {at} {partUnit(data.type)}在后面，这里只显示了前面一部分</span>
          <button className="ws-btn sm" onClick={download}><IconDownload size={13} />下载原件</button>
        </div>
      )}
      <SentBody data={data} name={f.name} at={missing ? null : at} />
      {data.more && (
        <div className={styles.more}>
          <span>只显示了前面一部分</span>
          <button className="ws-btn sm" onClick={download}><IconDownload size={13} />下载原件</button>
        </div>
      )}
    </>
  );
  const framed = (paged || live) && ready && !error;
  const padded = !(ready && !error && (paged || live || data?.type === "code"));
  return (
    <PanelShell label={`文件：${f.name}`} title={f.name} badge={!mobile && <span className={styles.headBadge}><FileBadge name={f.name} /></span>} facts={facts} acts={acts}
      onBack={back ? () => onNav({ list: true }) : null} onClose={onClose} focus={focus}>
      <div ref={bodyRef} className={pstyles.body + (padded ? " " + pstyles.padded : "") + (framed ? " " + pstyles.framed : "")}>
        {body}
        <QuoteButton sel={sel} title={f.name} onDone={onClose} />
      </div>
    </PanelShell>
  );
}
