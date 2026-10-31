// 画的图：generate_image 的结果（image-service，见 app/tools/images.py），照 Fusion 的图片卡片（GeneratedImages.tsx）：
// 生成中按要的比例显示点阵占位和用了几秒，出图后原位淡入；卡片下写明哪个模型画的，可以看描述、下载、再画一张，点图看大图
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconClose, IconDownload, IconLoop } from "./icons.jsx";

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
export const REGENERATE_REQUEST = "用同样的描述再生成一张";

// 这一轮的画图调用，一次一张卡片：画好的是图，还在画的是占位；没画成的就地说明，后面又画了的（重试）由后一张接替
export function imagesOf(items) {
  const calls = items.filter((i) => i.type === "step" && i.kind === "image");
  return calls.filter((s, k) => s.image || s.pending || k === calls.length - 1);
}

export const imageUrl = (tid, img) => `/api/research/threads/${encodeURIComponent(tid)}/images/${img.file}`;

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

export function ImageCards({ steps, tid, onRegenerate }) {
  const [viewing, setViewing] = useState(null);
  if (!steps.length) return null;
  return (
    <div className="ws-images">
      {steps.map((s) => (s.image
        ? <ImageCard key={s.id} img={s.image} src={imageUrl(tid, s.image)} onOpen={(back) => setViewing({ img: s.image, back })} onRegenerate={onRegenerate} />
        : <PendingCard key={s.id} s={s} />))}
      {viewing && <Viewer img={viewing.img} src={imageUrl(tid, viewing.img)} onClose={() => { viewing.back?.focus(); setViewing(null); }} />}
    </div>
  );
}

function PendingCard({ s }) {
  const running = !!s.pending;
  const secs = useElapsed(s.id, running);
  const [w, h] = ratioOf(s.aspect_ratio);
  const f = frame(w, h);
  return (
    <figure className="ws-image" style={{ width: f.width }}>
      <div className="ws-image-frame" style={f} role={running ? "status" : undefined} aria-live={running ? "polite" : undefined}>
        <Dots elapsed={secs} active={running} />
        <span className={running ? "sr" : "ws-image-note"}>{running ? "正在生成图片" : s.message || "图片生成失败"}</span>
        {running && <span className="ws-image-secs">{secs} 秒</span>}
      </div>
    </figure>
  );
}

function ImageCard({ img, src, onOpen, onRegenerate }) {
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
    <figure className="ws-image" style={{ width: f.width }}>
      <div className="ws-image-frame" style={f}>
        {state === "loading" && <Dots elapsed={0} active />}
        {state === "failed" ? (
          <div className="ws-image-failed">
            <span>图片加载失败</span>
            <button className="ws-btn sm" onClick={() => { setState("loading"); setAttempt((n) => n + 1); }}><IconLoop size={13} />重试</button>
          </div>
        ) : (
          <button className="ws-image-open" aria-label="看大图" onClick={(e) => onOpen(e.currentTarget)}>
            <img ref={imgRef} key={attempt} src={attempt ? `${src}?retry=${attempt}` : src} alt={img.prompt} data-loaded={state === "loaded"}
              data-instant={instant || undefined} onLoad={() => setState("loaded")} onError={() => setState("failed")} />
          </button>
        )}
      </div>
      <figcaption className="ws-image-cap">
        <div className="row">
          <span className="by">{model ? `本次生图由 ${model} 提供` : ""}</span>
          <button className="ws-image-act text" aria-expanded={promptOpen} onClick={() => setPromptOpen(!promptOpen)}>{promptOpen ? "收起描述" : "查看描述"}</button>
          <a className="ws-image-act" href={src} download={`lingdraft-image-${img.id.slice(0, 8)}.${img.file.split(".").pop()}`} aria-label="下载" title="下载">
            <IconDownload size={14} />
          </a>
          {onRegenerate && (
            <button className="ws-image-act" aria-label="重新生成" title="重新生成" onClick={() => onRegenerate(REGENERATE_REQUEST)}><IconLoop size={14} /></button>
          )}
        </div>
        {promptOpen && <p className="ws-image-prompt">{img.prompt}</p>}
      </figcaption>
    </figure>
  );
}

// 看大图：盖住整个页面，点空白处、按 Esc 或点关闭都收起。光标先放到关闭按钮上，Esc 归这里，不会去停止回答
function Viewer({ img, src, onClose }) {
  const close = useRef(null);
  useEffect(() => { close.current?.focus(); }, []);
  return (
    <div className="ws-viewer" role="dialog" aria-modal="true" aria-label="图片" onClick={onClose}
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
      <img src={src} alt={img.prompt} onClick={(e) => e.stopPropagation()} />
      <button ref={close} className="ws-viewer-close" aria-label="关闭" onClick={onClose}><IconClose size={20} /></button>
    </div>
  );
}

// 生图占位点阵（照 Fusion 的 ImageGenerationDots）：每个点按位置错开明暗，形成斜着扫过的波纹；
// 一块主色光斑在框里游走，范围随用时变大。只是让人看着在动，不代表真实进度。没画成、停了只留暗点
const SPACING = 12;
const FRAME_MS = 1000 / 30;

function cssColor(el, name, fallback) {
  return getComputedStyle(el).getPropertyValue(name).trim() || fallback;
}

function Dots({ elapsed, active }) {
  const ref = useRef(null);
  const elapsedRef = useRef(elapsed);
  useEffect(() => { elapsedRef.current = elapsed; }, [elapsed]);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const base = cssColor(canvas, "--muted", "#535b66");
    const glow = cssColor(canvas, "--accent", "#2c4f95");
    let width = 0;
    let height = 0;
    let raf = 0;
    let drawnAt = 0;
    const t0 = performance.now();
    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = devicePixelRatio || 1;
      width = r.width;
      height = r.height;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const draw = (now) => {
      const t = reduced ? 0 : (now - t0) / 1000;
      ctx.clearRect(0, 0, width, height);
      const cols = Math.floor(width / SPACING);
      const rows = Math.floor(height / SPACING);
      const ox = (width - (cols - 1) * SPACING) / 2;
      const oy = (height - (rows - 1) * SPACING) / 2;
      const sx = 0.5 + 0.32 * Math.sin(t * 0.9);
      const sy = 0.5 + 0.32 * Math.cos(t * 0.7);
      const spread = 0.16 + 0.22 * (1 - Math.exp(-elapsedRef.current / 20));
      const twoSq = 2 * spread * spread;
      for (let row = 0; row < rows; row += 1) {
        for (let col = 0; col < cols; col += 1) {
          const x = ox + col * SPACING;
          const y = oy + row * SPACING;
          ctx.globalAlpha = 0.28;
          ctx.fillStyle = base;
          ctx.beginPath();
          ctx.arc(x, y, 1.1, 0, Math.PI * 2);
          ctx.fill();
          if (!active) continue;
          const u = x / width;
          const v = y / height;
          const spot = Math.exp(-((u - sx) ** 2 + (v - sy) ** 2) / twoSq);
          const wave = 0.5 + 0.5 * Math.sin((u * 0.8 + v) * Math.PI * 2.4 - t * 2.6);
          const k = Math.min(1, 0.08 + 0.6 * spot + 0.45 * wave * (0.25 + 0.75 * spot));
          if (k < 0.06) continue;
          ctx.globalAlpha = k;
          ctx.fillStyle = glow;
          ctx.beginPath();
          ctx.arc(x, y, 1 + 1.4 * k, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      ctx.globalAlpha = 1;
    };
    const loop = (now) => {
      if (now - drawnAt >= FRAME_MS) {
        drawnAt = now;
        draw(now);
      }
      raf = requestAnimationFrame(loop);
    };
    resize();
    const ro = new ResizeObserver(() => { resize(); draw(performance.now()); });
    ro.observe(canvas);
    if (active && !reduced) raf = requestAnimationFrame(loop);
    else draw(performance.now());
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, [active]);
  return <canvas ref={ref} className="ws-image-dots" aria-hidden="true" />;
}
