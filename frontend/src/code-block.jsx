// 回答里的代码块（2026-11-07 Sean 定，照搬 Fusion 的 CodeBlock）：
// 上面一条写语言、行数，右边“复制”；左边行号；超过 12 行先只显示前 12 行，底下“显示剩余 N 行代码”，上面那条的箭头展开、折叠。
// HTML 代码块还能“下载”成文件，回答写完才能点，免得下到半截代码。
// 没照搬的：HTML 的“运行”（Sean 定不要）、Fusion 按钮上的玻璃光斑（那是 Fusion 自己的视觉风格）
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronUp, ClipboardCopy, Download, FileText, Hash } from "lucide-react";
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
const HTML = new Set(["html", "htm"]);

// lang：小写的语言标记（没写是 ""）；text：代码原文；download：HTML 能不能下载（过程里的话不给）；ready：回答写完了
export function CodeBlock({ lang, text, download = false, ready = false }) {
  const [open, setOpen] = useState(false);
  const [copy, setCopy] = useState("idle"); // idle / copying / copied / failed
  const timer = useRef(null);
  const turn = useRef(0); // 代码变了、卸掉了：之前那次复制回来不再改提示
  const id = useId();
  const plain = PLAIN.has(lang);
  const color = useLazy("code", !plain);
  const html = download && HTML.has(lang);

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
              <button type="button" className={styles.button} onClick={() => saveHtml(text)} disabled={!ready}
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
      </div>
    </div>
  );
}

// ---------- 下载 HTML（照 Fusion） ----------
// 文件名取页面的 <title>（只解析不执行），去掉文件名里不能有的字符；没有标题叫 page.html。下的是模型写的原文
function fileName(html) {
  const title = new DOMParser().parseFromString(html, "text/html").title;
  const safe = title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
  return `${safe || "page"}.html`;
}

function saveHtml(text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/html;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: fileName(text) });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
