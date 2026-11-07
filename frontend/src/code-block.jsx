// 回答里的代码块（2026-11-07 Sean 定，照搬 Fusion 的 CodeBlock 和 HtmlPreview）：
// 上面一条写语言、行数，右边“复制”；左边行号；超过 12 行先只显示前 12 行，底下“显示剩余 N 行代码”，上面那条的箭头展开、折叠。
// HTML 代码块还能“运行”（在隔开的框里跑，读不到本站的登录和存储）和“下载”；回答写完才能点，免得跑半截代码。
// 没照搬的：Fusion 按钮上的玻璃光斑（那是 Fusion 自己的视觉风格）
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronUp, ClipboardCopy, Download, FileText, Hash, Play, RotateCw, X } from "lucide-react";
import { useLazy } from "./lazy.js";
import { copyText } from "./research.js";
import styles from "./code-block.module.css";

const FOLD = 12;
const PLAIN = new Set(["", "text", "txt", "plaintext", "plain"]);

// 语言名：模型写的是小写缩写，换成常见写法；不认识的照写成大写
const LANG_NAMES = {
  js: "JavaScript", javascript: "JavaScript", jsx: "JSX", ts: "TypeScript", typescript: "TypeScript", tsx: "TSX",
  py: "Python", python: "Python", sh: "Shell", shell: "Shell", bash: "Bash", zsh: "Zsh", console: "Shell", powershell: "PowerShell", ps1: "PowerShell",
  json: "JSON", html: "HTML", htm: "HTML", xml: "XML", svg: "SVG", css: "CSS", scss: "SCSS", less: "LESS", sql: "SQL", java: "Java", go: "Go",
  rust: "Rust", rs: "Rust", c: "C", cpp: "C++", "c++": "C++", cs: "C#", csharp: "C#", yaml: "YAML", yml: "YAML", toml: "TOML", ini: "INI",
  md: "Markdown", markdown: "Markdown", diff: "Diff", dockerfile: "Dockerfile", docker: "Dockerfile", nginx: "Nginx", php: "PHP",
  rb: "Ruby", ruby: "Ruby", swift: "Swift", kt: "Kotlin", kotlin: "Kotlin", vue: "Vue", svelte: "Svelte",
};
const RUNNABLE = new Set(["html", "htm"]);

// lang：小写的语言标记（没写是 ""）；text：代码原文；run：能不能运行 HTML（过程里的话不给）；ready：回答写完了
export function CodeBlock({ lang, text, run = false, ready = false }) {
  const [open, setOpen] = useState(false);
  const [copy, setCopy] = useState("idle"); // idle / copying / copied / failed
  const timer = useRef(null);
  const turn = useRef(0); // 代码变了、卸掉了：之前那次复制回来不再改提示
  const id = useId();
  const plain = PLAIN.has(lang);
  const color = useLazy("code", !plain);
  const html = run && RUNNABLE.has(lang);
  const runner = useHtmlRunner(text);

  useEffect(() => {
    turn.current += 1;
    setCopy("idle");
    return () => {
      turn.current += 1;
      clearTimeout(timer.current);
    };
  }, [text]);

  const onCopy = async () => {
    const mine = turn.current;
    clearTimeout(timer.current);
    setCopy("copying");
    try {
      await copyText(text);
      if (turn.current !== mine) return;
      setCopy("copied");
      timer.current = setTimeout(() => setCopy("idle"), 2000);
    } catch {
      if (turn.current === mine) setCopy("failed");
    }
  };

  const lines = text.split("\n");
  const total = lines.length;
  const long = total > FOLD;
  const folded = long && !open;
  const shown = folded ? lines.slice(0, FOLD).join("\n") : text;
  const colored = useMemo(() => (color ? color(shown, lang) : null), [color, shown, lang]);
  const name = plain ? "纯文本" : LANG_NAMES[lang] || lang.toUpperCase();
  const nums = Array.from({ length: folded ? FOLD : total }, (_, i) => i + 1).join("\n");

  return (
    <div className={"md-wide " + styles.box}>
      <div className={styles.block} data-code-block="true">
        <div className={styles.header}>
          <div className={styles.meta}>
            <FileText aria-hidden="true" />
            <span className={styles.language}>{name}</span>
            <span className={styles.count}>
              <Hash aria-hidden="true" />
              <span>{folded ? `${FOLD}/${total} 行` : `${total} 行`}</span>
            </span>
            {long && (
              <button type="button" className={styles.iconButton} onClick={() => setOpen(folded)}
                title={folded ? "展开代码" : "折叠代码"} aria-label={folded ? "展开代码" : "折叠代码"} aria-expanded={!folded} aria-controls={id}>
                {folded ? <ChevronDown aria-hidden="true" /> : <ChevronUp aria-hidden="true" />}
              </button>
            )}
          </div>
          <div className={styles.actions}>
            {html && (
              <button type="button" className={styles.button} onClick={runner.run} disabled={!ready}
                title={ready ? "运行这段 HTML，看看效果" : "回答写完后可以运行"} aria-label="运行这段 HTML">
                <Play aria-hidden="true" />
                <span>{runner.preview ? "重新运行" : "运行"}</span>
              </button>
            )}
            {html && (
              <button type="button" className={styles.button} onClick={runner.download} disabled={!ready}
                title={ready ? "下载成 HTML 文件，用浏览器打开" : "回答写完后可以下载"} aria-label="下载成 HTML 文件">
                <Download aria-hidden="true" />
                <span>下载</span>
              </button>
            )}
            <button type="button" className={styles.button + (copy === "copied" ? " " + styles.success : "")} onClick={onCopy}
              disabled={copy === "copying"} title="复制代码" aria-label="复制代码">
              {copy === "copied" ? <Check aria-hidden="true" /> : <ClipboardCopy aria-hidden="true" />}
              <span aria-live="polite">{copy === "copied" ? "已复制" : copy === "copying" ? "复制中…" : "复制"}</span>
            </button>
          </div>
        </div>

        {copy === "failed" && <p role="alert" className={styles.error}>没复制成，可以选中代码手动复制，或者再点一次。</p>}

        <div id={id} className={styles.scroll} role="region" aria-label={`${name}代码`} tabIndex={0}>
          <div className={styles.row}>
            <div className={styles.gutter} aria-hidden="true" style={{ width: `${Math.max(String(total).length * 0.6 + 1, 2.5)}rem` }}>{nums}</div>
            <div className={styles.column}>
              <pre className={styles.code}>
                {colored != null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: colored }} /> : <code>{shown}</code>}
              </pre>
              {folded && (
                <div className={styles.expand}>
                  <div>
                    <button type="button" className={styles.expandButton} onClick={() => setOpen(true)} aria-expanded="false" aria-controls={id}>
                      <ChevronDown aria-hidden="true" />
                      <span>显示剩余 {total - FOLD} 行代码</span>
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {runner.preview && <HtmlPreview html={runner.preview.html} runId={runner.preview.runId} onRerun={runner.run} onClose={runner.close} />}
      </div>
    </div>
  );
}

// ---------- 运行 HTML（照 Fusion 的 HtmlPreview） ----------
// 框只给 allow-scripts 和弹框，不给 allow-same-origin：框里的页面算另一个来源，读不到本站的 cookie、存储和登录，
// 也动不了外面的页面；不给跳转、开新窗口，跑不出这个框
const SANDBOX = "allow-scripts allow-modals";
// 没有同源权限时碰 localStorage、sessionStorage 会直接报错，常见的待办、计分小例子就停在那一行：先放一个只在内存里的替身，关掉、重新运行就清空
const STORAGE_SHIM = "<script>(function(){function m(){var d=new Map();return{"
  + "getItem:function(k){k=String(k);return d.has(k)?d.get(k):null},"
  + "setItem:function(k,v){d.set(String(k),String(v))},"
  + "removeItem:function(k){d.delete(String(k))},"
  + "clear:function(){d.clear()},"
  + "key:function(i){var a=Array.from(d.keys());return i<a.length?a[i]:null},"
  + "get length(){return d.size}}}"
  + "[\"localStorage\",\"sessionStorage\"].forEach(function(n){try{window[n]}catch(e){"
  + "try{Object.defineProperty(window,n,{value:m(),configurable:true})}catch(_){}}})})();</script>";
// 框里的页面内容高度变了告诉外面，框跟着撑开（最高到屏幕的八成），不用在小框里来回滑
const HEIGHT_MESSAGE = "lingdraft-html-preview-height";
const HEIGHT_REPORTER = "<script>(function(){var last=0;function send(){"
  + "var h=Math.ceil(document.documentElement.scrollHeight);"
  + `if(h!==last){last=h;parent.postMessage({type:"${HEIGHT_MESSAGE}",height:h},"*")}}`
  + "if(window.ResizeObserver){var ro=new ResizeObserver(send);ro.observe(document.documentElement);"
  + "document.addEventListener(\"DOMContentLoaded\",function(){if(document.body)ro.observe(document.body)})}"
  + "window.addEventListener(\"load\",send)})();</script>";
const MIN_HEIGHT = 320;

// 替身要在页面自己的脚本之前跑，又不能插在 <!DOCTYPE> 前面（页面会进怪异模式）：依次找 <head>、<html>、DOCTYPE 后面插，都没有就放最前面
function sandboxDocument(html) {
  for (const pattern of [/<head(\s[^>]*)?>/i, /<html(\s[^>]*)?>/i, /<!doctype[^>]*>/i]) {
    const m = pattern.exec(html);
    if (m) {
      const end = m.index + m[0].length;
      return html.slice(0, end) + STORAGE_SHIM + HEIGHT_REPORTER + html.slice(end);
    }
  }
  return STORAGE_SHIM + HEIGHT_REPORTER + html;
}

// 下载的文件名取页面的 <title>（只解析不执行），去掉文件名里不能有的字符；没有标题叫 page.html
function fileName(html) {
  const title = new DOMParser().parseFromString(html, "text/html").title;
  const safe = title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
  return `${safe || "page"}.html`;
}

function useHtmlRunner(text) {
  const [preview, setPreview] = useState(null);
  // 运行用点下去那一刻的代码：之后代码再变不自动重跑，要再点“重新运行”
  const run = () => setPreview((cur) => ({ html: text, runId: (cur?.runId ?? 0) + 1 }));
  const close = () => setPreview(null);
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/html;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: fileName(text) });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return { preview, run, close, download };
}

function HtmlPreview({ html, runId, onRerun, onClose }) {
  const frame = useRef(null);
  const [fit, setFit] = useState({ runId, height: MIN_HEIGHT }); // 重新运行回到起始高度，等新页面报
  const height = fit.runId === runId ? fit.height : MIN_HEIGHT;
  useEffect(() => {
    const onMessage = (e) => {
      if (e.source !== frame.current?.contentWindow) return;
      const d = e.data;
      if (d?.type !== HEIGHT_MESSAGE || typeof d.height !== "number" || !Number.isFinite(d.height)) return;
      setFit({ runId, height: Math.max(MIN_HEIGHT, Math.ceil(d.height)) });
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [runId]);
  return (
    <div className={styles.preview}>
      <div className={styles.previewBar}>
        <span>运行结果（单独隔开运行，读不到你的账号）</span>
        <div className={styles.previewActions}>
          <button type="button" className={styles.previewButton} onClick={onRerun} aria-label="重新运行"><RotateCw aria-hidden="true" /></button>
          <button type="button" className={styles.previewButton} onClick={onClose} aria-label="关掉运行结果"><X aria-hidden="true" /></button>
        </div>
      </div>
      <iframe key={runId} ref={frame} className={styles.previewFrame} style={{ "--preview-height": `${height}px` }}
        title="HTML 运行结果" sandbox={SANDBOX} referrerPolicy="no-referrer" srcDoc={sandboxDocument(html)} />
    </div>
  );
}
