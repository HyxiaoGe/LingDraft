// 用户发的文件，一个一张小卡片：左边是类型角标（PDF 红、Word 蓝、Excel 绿、PPT 橙，别的灰），右边文件名和一行说明。
// 输入框里选好的（composer.jsx：上传中、解析中、读不了，右上角去掉）和对话里发出去的（timeline.jsx、右栏的文件列表：点开在右栏看，见 workspace-files.jsx）都用它
import { useContext, useState } from "react";
import { downloadThreadFile } from "./api.js";
import { IconClose, Spinner } from "./icons.jsx";
import { NavContext } from "./nav.jsx";
import styles from "./files.module.css";

const KINDS = { pdf: "pdf", docx: "word", doc: "word", xlsx: "excel", xls: "excel", csv: "excel", pptx: "ppt", ppt: "ppt" };
const ext = (name) => (/\.([A-Za-z0-9]{1,5})$/.exec(name || "")?.[1] || "").toLowerCase();

// 角标上的字：扩展名（最多 4 个字母），没有扩展名写 FILE
export function Badge({ name }) {
  const e = ext(name);
  return <span className={styles.badge} data-kind={KINDS[e] || "other"} aria-hidden="true">{(e || "file").slice(0, 4).toUpperCase()}</span>;
}

export function sizeText(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

const TYPE_NAMES = { pdf: "PDF", docx: "Word", xlsx: "Excel", pptx: "PPT", csv: "CSV", markdown: "Markdown" };
// 解析好了的文件那行说明：类型 · 大小 · 几页（PPT 几张、Excel 几个表）
export function fileFacts(f) {
  const kind = TYPE_NAMES[f.type] || (ext(f.name) ? ext(f.name).toUpperCase() : "文本");
  const pages = f.pages ? (f.type === "pptx" ? `${f.pages} 张` : f.type === "xlsx" ? `${f.pages} 个表` : `${f.pages} 页`) : "";
  return [kind, sizeText(f.size), pages].filter(Boolean).join(" · ");
}

// 读文件那一步按“页”“张”还是“段”说（PDF 一段就是一页，PPT 一段是一张）
export const partUnit = (type) => (type === "pdf" ? "页" : type === "pptx" ? "张" : "段");

// name、sub：文件名和说明；bad：读不了（说明标成警示色）；busy：上传、解析中，角标换成转圈；on：右栏正开着它。
// onOpen：点卡片做什么（在右栏看、下载），没有就不能点；onRemove：右上角去掉（输入框里的）
export function FileCard({ name, sub, bad, busy, on, wide, onOpen, openLabel, onRemove }) {
  const body = (
    <>
      {busy ? <span className={styles.badge} data-kind="busy"><Spinner size={14} /></span> : <Badge name={name} />}
      <span className={styles.text}>
        <span className={styles.name}>{name}</span>
        {sub && <span className={styles.sub + (bad ? " " + styles.bad : "")}>{sub}</span>}
      </span>
    </>
  );
  return (
    <div className={styles.card + (bad ? " " + styles.failed : "") + (on ? " " + styles.on : "") + (wide ? " " + styles.wide : "")} title={name}>
      {onOpen ? <button className={styles.inner} aria-label={openLabel} aria-pressed={on === undefined ? undefined : on} onClick={onOpen}>{body}</button>
        : <div className={styles.inner}>{body}</div>}
      {onRemove && <button className={styles.remove} aria-label={`去掉 ${name}`} onMouseDown={(e) => e.preventDefault()} onClick={onRemove}><IconClose size={12} /></button>}
    </div>
  );
}

// 对话里用户那句话带的文件（[{id, name, type, size, pages}]），在气泡上面靠右一排。tid：对话编号，有才能点
// （刚发出去、还没存下来的那句先不能点）；panel：右栏（{open, current}，见 workspace.jsx），点了在右栏看，current 是右栏正开着的
export function SentFiles({ files, tid, panel }) {
  return <div className={styles.sent}>{files.map((f) => <SentFile key={f.id} f={f} tid={tid} panel={panel} />)}</div>;
}

// 手机上 PDF 不在页面里看（iOS 只显示第一页、安卓多半直接下载，2026-11-09 Sean 定先不处理）：点了下载原件
export const previewable = (f, mobile) => !(mobile && f.type === "pdf");

// 一个发过的文件：点了在右栏看（back：从文件列表点开的，右栏左上角回列表；列表里的铺满那一栏）；看不了的、没有右栏的下载原件。
// 跑命令产出的文件也用它（commands.jsx，f 多一项 preview）：没抽出文字的（preview 是 false）只能下载；wide：和模型写的文件卡片一样大
export function SentFile({ f, tid, panel, back = false, wide = back }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const [state, setState] = useState(null); // busy：下载中；gone：文件不在了（删掉的）；failed：没下载成
  const view = !!panel && f.preview !== false && previewable(f, mobile);
  const download = async () => {
    if (state === "busy") return;
    setState("busy");
    try {
      await downloadThreadFile(tid, f);
      setState(null);
    } catch (e) {
      setState(e.status === 404 ? "gone" : "failed");
    }
  };
  const open = !tid ? undefined : view ? () => panel.open({ sent: f, back }) : download;
  const sub = state === "busy" ? "正在下载…" : state === "gone" ? "文件已经不在了" : state === "failed" ? "没下载成，再点一次" : fileFacts(f);
  return <FileCard name={f.name} sub={sub} bad={state === "gone" || state === "failed"} busy={state === "busy"} onOpen={open} wide={wide}
    on={view ? panel.current?.sent?.id === f.id : undefined} openLabel={view ? `打开 ${f.name}` : `下载 ${f.name}`} />;
}

// 这段对话里用户发过的文件，按编号和名字都能查（读文件那一步：做完了记的是名字，还在读、没读成的记的是编号）
export function fileIndex(turns) {
  const out = {};
  for (const t of turns || []) {
    for (const f of t.files || []) {
      out[f.id] = f;
      out[f.name] ??= f;
    }
  }
  return out;
}
