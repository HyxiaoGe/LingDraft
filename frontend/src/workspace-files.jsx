// 工作区（2026-11-08 Sean 定，方案见 docs/工作区和Artifact方案.md 第 5 条）：一段对话里的所有文件。
// - 顶栏的“文件 N”（FilesButton）在右栏打开文件列表（FileList）：生成的文件（每份最新一版，这一轮正在新建的跟着出字数）、上传的文件、
//   图片（发的、画的，按先后）。文件点开在右栏看，看完左上角回列表；图片看大图、能下载。
// - 用户发的文件在右栏看（SentPanel，2026-11-09 Sean 定）：PDF 在桌面上用浏览器自带的 PDF 查看器看原件；别的看抽出来的文字——
//   Markdown、Word 和正文一样排，PPT、表格每段一个小标题（第几张、哪张表哪几行），代码带行号和高亮，纯文字照原样。
//   手机上 PDF 点了直接下载（见 files.jsx 的 previewable）。太长的只显示前面一部分，底下能下载原件。选中一段能引用到输入框。
import { useContext, useEffect, useRef, useState } from "react";
import { downloadThreadFile, threadFileBlob, threadFileParts } from "./api.js";
import { CopyAll, FileCards, PanelShell, QuoteButton, fileVersions, turnCards, useSelected } from "./artifact.jsx";
import { CodeBlock } from "./code-block.jsx";
import { Badge as FileBadge, SentFile, fileFacts, partUnit } from "./files.jsx";
import { IconDownload, IconFolder, Spinner } from "./icons.jsx";
import { Viewer, drawn, imageAlt, imageFileName, imageUrl, imagesOf } from "./images.jsx";
import { Markdown } from "./markdown.jsx";
import { NavContext, typeNext } from "./nav.jsx";
import { mergeItems } from "./research.js";
import pstyles from "./artifact.module.css";
import styles from "./workspace-files.module.css";

const EXTENSIONS = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };

// 这段对话里的文件：written 是生成的文件（artifact.jsx 的卡片，每份最新一版；这一轮正在新建、还没写成的也在），sent 是上传的文件，
// images 是图片（{img, name, alt}，name 是下载用的文件名：画的和对话里图片卡片上下载的一样，发的叫“发的图-N”）。count 是一共几个（顶栏上写的）。
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
  const images = [];
  const seen = new Set();
  let made = 0;
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
      made += 1;
      images.push({ img: s.image, name: imageFileName(title, made, s.image.mime_type), alt: imageAlt(s.image.prompt) });
    }
  }
  return { written, sent, images, count: written.length + sent.length + images.length };
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
  const docs = ws.written.length + ws.sent.length;
  const facts = [docs && `${docs} 个文件`, ws.images.length && `${ws.images.length} 张图`].filter(Boolean).join(" · ");
  const panel = { open: onNav, current: null };
  return (
    <PanelShell label="这段对话的文件" title="文件" badge={<span className={styles.headIcon} aria-hidden="true"><IconFolder size={17} /></span>}
      facts={facts && <span>{facts}</span>} onClose={onClose} focus={focus}>
      <div className={pstyles.body + " " + pstyles.padded}>
        {!ws.count && <p className={pstyles.note}>这段对话还没有文件。</p>}
        {ws.written.length > 0 && (
          <Section title="生成的文件">
            <FileCards wide cards={ws.written} running={!!live} open={(t) => onNav({ ...t, back: true })} current={null} />
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

// 抽出来的文字：从后端取，原件不会变，取过的记着（这次打开的页面里）
const parts = new Map();
function useParts(tid, id) {
  const [, bump] = useState(0);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setError(null);
    if (!tid || !id || parts.has(id)) return undefined;
    const ctl = new AbortController();
    threadFileParts(tid, id, ctl.signal).then((d) => { parts.set(id, d); bump((n) => n + 1); })
      .catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => ctl.abort();
  }, [tid, id, tries]);
  return { data: id ? parts.get(id) ?? null : null, error, retry: () => setTries((n) => n + 1) };
}

// PDF 原件：要带登录信息，取成 blob 给 iframe（浏览器自带的 PDF 查看器）。关上右栏就放掉，再开时浏览器缓存里有，不用再下一遍
function usePdf(tid, id) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(null);
  const [tries, setTries] = useState(0);
  useEffect(() => {
    setUrl(null);
    setError(null);
    if (!tid || !id) return undefined;
    const ctl = new AbortController();
    let made = null;
    threadFileBlob(tid, id, ctl.signal).then((b) => {
      if (ctl.signal.aborted) return;
      made = URL.createObjectURL(new Blob([b], { type: "application/pdf" }));
      setUrl(made);
    }).catch((e) => { if (!ctl.signal.aborted) setError(e.status === 404 ? "gone" : "failed"); });
    return () => {
      ctl.abort();
      if (made) URL.revokeObjectURL(made);
    };
  }, [tid, id, tries]);
  return { url, error, retry: () => setTries((n) => n + 1) };
}

const DOC_LIKE = new Set(["markdown", "docx"]); // 整篇当 Markdown 排（Word 抽出来的标题、表格已经是 Markdown）
const LINES = new Set(["code", "text"]); // 按行切的：接回去就是原文
const ext = (name) => (/\.([A-Za-z0-9]{1,8})$/.exec(name || "")?.[1] || "").toLowerCase();
const noCite = () => null;

// 复制、引用用的全文：按行切的接回原文；文档整篇；PPT、表格每段前面写段名
function fullText({ type, parts: ps }) {
  if (LINES.has(type)) return ps.map((p) => p.text).join("\n").replace(/\n+$/, ""); // 文件末尾的换行不多出一个空行
  if (DOC_LIKE.has(type)) return ps.map((p) => p.text).join("\n\n");
  return ps.map((p) => `${p.title}\n\n${p.text}`).join("\n\n");
}

function SentBody({ data, name }) {
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

// 右栏看用户发的一个文件（f：{id, name, type, size, pages}）。back：从文件列表点开的，左上角回列表
export function SentPanel({ tid, f, back, onNav, onClose, focus }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const pdf = f.type === "pdf" && !mobile;
  const text = useParts(tid, pdf ? null : f.id);
  const original = usePdf(tid, pdf ? f.id : null);
  const { data } = text;
  const error = pdf ? original.error : text.error;
  const retry = pdf ? original.retry : text.retry;
  const ready = pdf ? !!original.url : !!data;
  const bodyRef = useRef(null);
  const sel = useSelected(bodyRef, !pdf && !!data);
  const [saving, setSaving] = useState(null); // busy / failed
  useEffect(() => setSaving(null), [f.id]);
  useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = 0; }, [f.id]);
  const download = async () => {
    if (saving === "busy") return;
    setSaving("busy");
    try {
      await downloadThreadFile(tid, f);
      setSaving(null);
    } catch {
      setSaving("failed");
    }
  };
  const dlLabel = saving === "busy" ? "正在下载…" : saving === "failed" ? "没下载成，再点一次" : `下载 ${f.name}`;
  const acts = (
    <>
      {!pdf && <CopyAll text={data ? fullText(data) : null} reset={f.id} />}
      <button className="ws-icon-btn" aria-label={dlLabel} title={dlLabel} aria-busy={saving === "busy" || undefined} onClick={download}>
        {saving === "busy" ? <Spinner size={15} /> : <IconDownload size={17} />}
      </button>
    </>
  );
  const facts = saving === "failed" ? <span className={pstyles.bad}>没下载成，再点一次</span> : <span>{fileFacts(f)}</span>;

  let body;
  if (error) body = (
    <div className={pstyles.note}>
      {error === "gone" ? "这个文件已经不在了。" : "内容没取到。"}
      {error !== "gone" && <button className="ws-btn sm" onClick={retry}>再试一次</button>}
    </div>
  );
  else if (!ready) body = <div className={pstyles.loading} role="status" aria-label="正在打开"><Spinner size={16} /></div>;
  else if (pdf) body = <iframe className={pstyles.frame} title={`${f.name}（PDF）`} src={original.url} />;
  else body = (
    <>
      <SentBody data={data} name={f.name} />
      {data.more && (
        <div className={styles.more}>
          <span>只显示了前面一部分</span>
          <button className="ws-btn sm" onClick={download}><IconDownload size={13} />下载原件</button>
        </div>
      )}
    </>
  );
  const padded = !(ready && !error && (pdf || data?.type === "code"));
  return (
    <PanelShell label={`文件：${f.name}`} title={f.name} badge={!mobile && <span className={styles.headBadge}><FileBadge name={f.name} /></span>} facts={facts} acts={acts}
      onBack={back ? () => onNav({ list: true }) : null} onClose={onClose} focus={focus}>
      <div ref={bodyRef} className={pstyles.body + (padded ? " " + pstyles.padded : "") + (pdf && ready && !error ? " " + pstyles.framed : "")}>
        {body}
        <QuoteButton sel={sel} title={f.name} onDone={onClose} />
      </div>
    </PanelShell>
  );
}
