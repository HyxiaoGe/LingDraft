// 画的图：generate_image 的结果（图存在 image-service 里，后端按编号取，见 app/tools/image_mcp.py），照 Fusion 的图片卡片（GeneratedImages.tsx）：
// 生成中按要的比例占好位置，出图后原位淡入；卡片下写明哪个模型画的，可以改这张、看描述、下载、再画一张，点图看大图。
// 占位没照搬 Fusion 的点阵：灰底上一道斜光扫过，中间写正在生成和用了几秒（2026-10-31 Sean 定）
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconClose, IconDownload, IconEdit, IconLoop, Spinner } from "./icons.jsx";
import styles from "./images.module.css";
import { useLayer } from "./layers.js";

const MAX_W = 640;
const MAX_H = 560;
// 和 image-service 的 SUPPORTED_MODELS 对上；表里没有的照原样写模型名
const MODEL_NAMES = {
  "doubao-seedream-5-0-pro": "豆包 Seedream 5.0 Pro",
  "doubao-seedream-5-0-flash": "豆包 Seedream 5.0 Flash",
  "doubao-seedream-5-0-lite": "豆包 Seedream 5.0 Lite",
  "doubao-seedream-4-5": "豆包 Seedream 4.5",
  "doubao-seedream-4-0": "豆包 Seedream 4.0",
  "gemini-nano-banana-2.1": "Nano Banana 2.1",
  "nano-banana-pro-preview": "Nano Banana Pro",
  "gemini-3.1-flash-image": "Gemini 3.1 Flash Image",
  "gemini-3.1-flash-image-preview": "Gemini 3.1 Flash Image",
  "gemini-3-pro-image": "Gemini 3 Pro Image",
  "gemini-3-pro-image-preview": "Gemini 3 Pro Image",
  "gemini-2.5-flash-image": "Gemini 2.5 Flash Image",
  "gemini-2.5-flash-image-preview": "Gemini 2.5 Flash Image",
  "qwen-image-3.0-pro": "通义 Qwen-Image 3.0 Pro",
  "qwen-image-3.0": "通义 Qwen-Image 3.0",
};
const REGENERATE_REQUEST = "用同样的描述再生成一张";

// 这一轮的画图调用，一次一张卡片：画好的是图，还在画的是占位；没画成的就地说明，后面又画了的（重试）由后一张接替
// 画成了、图还在的那一步
export const drawn = (s) => !!s.image && !s.message;

export function imagesOf(items) {
  const calls = items.filter((i) => i.type === "step" && i.kind === "image");
  return calls.filter((s, k) => s.image || s.pending || k === calls.length - 1);
}

export const imageUrl = (tid, img) => `/api/research/threads/${encodeURIComponent(tid)}/images/${encodeURIComponent(img.id)}`;
const EXTENSIONS = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };

// 下载的文件名带上对话标题、是这段对话的第几张（批次 G 第 50 条）：“北京秋天的海报-2.png”。文件名里不能有的字符换成空格，太长截短；还没有标题的叫“灵稿的图”
export function imageFileName(title, n, mime) {
  const base = title && title !== "新对话" ? title.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").replace(/\s+/g, " ").trim() : "";
  const short = Array.from(base).slice(0, 40).join("").trim(); // 按字数截，不把表情这类两个码元的字切成半个
  return `${short || "灵稿的图"}-${n}.${EXTENSIONS[mime] || "png"}`;
}

// 图片的替代文字（读屏念的）：描述开头一句、最多 40 字，不把整段描述当图片说明念一遍；整段在“查看描述”里
const ALT_MAX = 40;
export function imageAlt(prompt) {
  const first = (prompt || "").trim().split(/(?<=[。！？!?；;])|\n|(?<=\.)\s/)[0].trim();
  const text = Array.from(first);
  return text.length ? `生成的图：${text.length > ALT_MAX ? text.slice(0, ALT_MAX).join("") + "…" : first}` : "生成的图";
}

// 改这张：把这张图取回来放进输入框当附件（和自己传的图走同一条路，大图在本机压一下，见 upload.js），接着说要怎么改、或照它画什么。
// 输入框收到 ws-attach-image（见 composer.jsx 的 Composer）；没取到时 detail 是 null，输入框下面说一声
async function editThis(src, name) {
  let file = null;
  try {
    const r = await fetch(src);
    if (r.ok) {
      const blob = await r.blob();
      file = new File([blob], name, { type: blob.type });
    }
  } catch { /* 网络断了：照没取到说 */ }
  dispatchEvent(new CustomEvent("ws-attach-image", { detail: file }));
}

function ratioOf(text) {
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(text || "");
  return m && Number(m[1]) > 0 && Number(m[2]) > 0 ? [Number(m[1]), Number(m[2])] : [1, 1];
}

// 卡片按图的比例定宽：竖图受高度限制，横图受宽度限制，正文栏窄了再由 max-width 收
function frame(w, h) {
  return { width: Math.round(w * Math.min(MAX_W / w, MAX_H / h)), aspectRatio: `${w} / ${h}` };
}

// 什么时候开始画的：页面第一次看到这一步的时候（刷新后从头数）
const firstSeen = new Map();
function useElapsed(id, running) {
  if (!firstSeen.has(id)) firstSeen.set(id, Date.now());
  const [, tick] = useState(0);
  const end = useRef(null);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [running]);
  if (!running && end.current == null) end.current = Date.now();
  return Math.max(0, Math.floor(((running ? Date.now() : end.current) - firstSeen.get(id)) / 1000));
}

// title、base：对话标题、这一轮之前已经有几张图，下载的文件名用（见 imageFileName）
export function ImageCards({ steps, tid, running, onRegenerate, title, base = 0 }) {
  const [viewing, setViewing] = useState(null);
  if (!steps.length) return null;
  let n = base;
  return (
    <div className={styles.images}>
      {/* 画成了却带着说明的：以前的图已经不在了（见 history.IMAGE_GONE），照没画成的样子说一声 */}
      {steps.map((s) => (drawn(s)
        ? <ImageCard key={s.id} img={s.image} src={imageUrl(tid, s.image)} name={imageFileName(title, ++n, s.image.mime_type)}
          onOpen={(back) => setViewing({ img: s.image, back })} onRegenerate={onRegenerate} />
        : <PendingCard key={s.id} s={s} running={running} />))}
      {viewing && <Viewer img={viewing.img} src={imageUrl(tid, viewing.img)} onClose={() => { viewing.back?.focus(); setViewing(null); }} />}
    </div>
  );
}

// 还在画的占位，或者没画成的说明。还没有结果、这一轮却不在跑了（出错停在这一步）：没画完，不再数秒
function PendingCard({ s, running: turnRunning }) {
  const running = turnRunning && !!s.pending;
  const secs = useElapsed(s.id, running);
  const [w, h] = ratioOf(s.aspect_ratio);
  const f = frame(w, h);
  return (
    <figure className={styles.image} style={{ width: f.width }}>
      <div className={styles.frame + (running ? " " + styles.busy : "")} style={f}>
        {running ? (
          <div className={styles.wait} role="status">
            <Spinner size={13} /><span>正在生成图片</span><span className={styles.secs} aria-hidden="true">· {secs} 秒</span>
          </div>
        ) : <span className={styles.note}>{s.pending ? "没有画完" : s.message || "图片生成失败"}</span>}
      </div>
    </figure>
  );
}

function ImageCard({ img, src, name, onOpen, onRegenerate }) {
  const [state, setState] = useState("loading"); // loading → loaded / failed
  const [instant, setInstant] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [promptOpen, setPromptOpen] = useState(false);
  const imgRef = useRef(null);
  const f = frame(img.width, img.height);
  const model = MODEL_NAMES[img.model] || img.model;
  // 已经在浏览器缓存里的图（切回这段对话）直接显示，不再淡入
  useLayoutEffect(() => {
    if (!imgRef.current?.complete || !imgRef.current.naturalWidth) return;
    setInstant(true);
    setState("loaded");
  }, []);
  return (
    <figure className={styles.image} style={{ width: f.width }}>
      <div className={styles.frame + (state === "loading" ? " " + styles.busy : "")} style={f}>
        {state === "failed" ? (
          <div className={styles.failed}>
            <span>图片加载失败</span>
            <button className="ws-btn sm" onClick={() => { setState("loading"); setAttempt((n) => n + 1); }}><IconLoop size={13} />重试</button>
          </div>
        ) : (
          <button className={styles.open} aria-label="看大图" onClick={(e) => onOpen(e.currentTarget)}>
            <img ref={imgRef} key={attempt} src={attempt ? `${src}?retry=${attempt}` : src} alt={imageAlt(img.prompt)} data-loaded={state === "loaded"}
              data-instant={instant || undefined} onLoad={() => setState("loaded")} onError={() => setState("failed")} />
          </button>
        )}
      </div>
      <figcaption className={styles.cap}>
        <div className={styles.row}>
          <span className={styles.by}>{model ? `本次生图由 ${model} 提供` : ""}</span>
          <button className={styles.act + " " + styles.text} onClick={() => editThis(src, name)}><IconEdit size={14} />改这张</button>
          <button className={styles.act + " " + styles.text} aria-expanded={promptOpen} onClick={() => setPromptOpen(!promptOpen)}>{promptOpen ? "收起描述" : "查看描述"}</button>
          <a className={styles.act} href={src} download={name} aria-label="下载" title="下载">
            <IconDownload size={14} />
          </a>
          {onRegenerate && (
            <button className={styles.act} aria-label="重新生成" title="重新生成" onClick={() => onRegenerate(REGENERATE_REQUEST)}><IconLoop size={14} /></button>
          )}
        </div>
        {promptOpen && <p className={styles.prompt}>{img.prompt}</p>}
      </figcaption>
    </figure>
  );
}

// 看大图：盖住整个页面，点空白处、按 Esc 或点关闭都收起。光标先放到关闭按钮上，Esc 收的是这一层，不会去停止回答。
// 挂到页面最外层：放在缩略图里会吃到缩略图的样式（铺满裁切），大图只剩中间一截。
// download：下载的文件名，给了右上角关闭旁边多一个下载（文件列表里点开的；对话里图片卡片上本来就有下载）
export function Viewer({ img, src, onClose, download }) {
  const close = useRef(null);
  useEffect(() => { close.current?.focus(); }, []);
  useLayer(true, onClose);
  return createPortal(
    <div className={styles.viewer} role="dialog" aria-modal="true" aria-label="图片" onClick={onClose}>
      <img src={src} alt={img.alt ?? imageAlt(img.prompt)} onClick={(e) => e.stopPropagation()} />
      <div className={styles.viewerActs}>
        {download && (
          <a className={styles.close} href={src} download={download} aria-label="下载" title="下载" onClick={(e) => e.stopPropagation()}><IconDownload size={20} /></a>
        )}
        <button ref={close} className={styles.close} aria-label="关闭" onClick={onClose}><IconClose size={20} /></button>
      </div>
    </div>,
    document.querySelector(".ws") || document.body,
  );
}
