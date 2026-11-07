// 正文的 Markdown（2026-11-07 Sean 定：不再手写，用现成的库；样式照搬 Fusion，见 markdown.module.css）：react-markdown 解析，
// remark-gfm 管表格、删除线、任务列表、裸链接，remark-breaks 让单个换行照样换行（中文回答靠它分行），remark-math + KaTeX 显示公式（有公式才去取 KaTeX，见 math.js）。
// 直接出 React 元素，不插入 HTML；模型写的 HTML 照字面显示（<br> 例外，换行：表格格子里换行只能这么写）。
// 代码块见 code-block.jsx、表格见 md-table.jsx（都照搬 Fusion）。自己加的：引用角标 [s3fa9c1]（连同前面一个字、后面的标点不断行）、
// 标题比页面标题小几档、图片只给链接（模型写的图片地址多半打不开，也不替用户去别的网站取东西）
import { createContext, useContext } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { CodeBlock } from "./code-block.jsx";
import { useLazy } from "./lazy.js";
import { MdTable } from "./md-table.jsx";
import { FENCE, closesFence } from "./research.js";
import styles from "./markdown.module.css";

// ---------- 交给解析之前：公式的几种写法、表格里代码的竖线 ----------
// 代码块、行内代码、$$ 公式块里面不动。
// - \( … \) 换成 $ … $，单独一行的 \[ 和 \] 换成 $$（DeepSeek 常这么写）；
// - 单个 $ 照 Pandoc 的规矩才算公式：开头的 $ 后面紧跟非空白，收尾的 $ 前面紧跟非空白、后面不是数字。
//   别的 $ 转义成字面的 $，“从 $5 涨到 $10”不会被当成公式；
// - 表格那一行里，行内代码中的 | 转义：不然 `a|b` 会被劈成两格（GFM 的规矩如此，模型常不转义）

function prepLine(s, out) {
  s = s.replace(/\\\((.+?)\\\)/g, (_, m) => `$${m.trim()}$`);
  // 单个 $：成对的留着，落单的转义
  let res = "";
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\") { res += s.slice(i, i + 2); i += 2; continue; }
    if (ch !== "$") { res += ch; i++; continue; }
    if (s[i + 1] === "$") { // $$ … $$ 写在一行里：原样交给解析
      const end = s.indexOf("$$", i + 2);
      if (end > 0) { out.math = true; res += s.slice(i, end + 2); i = end + 2; continue; }
      res += "\\$\\$"; i += 2; continue;
    }
    let j = -1;
    if (s[i + 1] && !/\s/.test(s[i + 1])) {
      for (let k = i + 1; k < s.length; k++) {
        if (s[k] === "\\") { k++; continue; }
        if (s[k] === "$") {
          if (s[k + 1] === "$") break;
          if (!/\s/.test(s[k - 1]) && !/\d/.test(s[k + 1] || "")) j = k;
          break;
        }
      }
    }
    if (j > 0) { out.math = true; res += s.slice(i, j + 1); i = j + 1; }
    else { res += "\\$"; i++; }
  }
  return res;
}

function prep(text) {
  const out = { math: false };
  const lines = text.split("\n");
  let fence = null;
  let block = false; // 在 $$ 公式块里
  for (let n = 0; n < lines.length; n++) {
    const line = lines[n];
    if (fence) { if (closesFence(line, fence)) fence = null; continue; }
    const f = line.match(FENCE);
    if (f) { fence = f[2]; continue; }
    const t = line.trim();
    if (t === "\\[" || t === "\\]") { lines[n] = line.replace(t, "$$$$"); block = t === "\\["; out.math = true; continue; }
    if (t.startsWith("$$") && (t === "$$" || !t.slice(2).includes("$$"))) { block = !block; out.math = true; continue; }
    if (block) { if (t.endsWith("$$")) block = false; continue; } // 收尾的 $$ 写在最后一行公式后面
    // 行内代码（成对的反引号）原样留着，只收拾外面的字
    const row = t.startsWith("|");
    lines[n] = line.split(/(`+[^`]*?`+)/).map((seg, k) => (k % 2 ? (row ? seg.replace(/(?<!\\)\|/g, "\\|") : seg) : prepLine(seg, out))).join("");
  }
  return { text: lines.join("\n"), math: out.math };
}

// ---------- 解析之后：裸链接、引用角标、<br> ----------
// remark-gfm 认出来的裸链接会把后面紧跟的中文一起吞进去（“详见 https://x.com/page，这里说明了。”整句成了链接）：
// 在全角标点、引用角标处截断；紧跟 ASCII 字母数字的中文也截断（“/page这里”）；斜杠、等号后面的中文算网址的一部分（维基百科的中文地址）
const txt = (value) => ({ type: "text", value });
const FULL_PUNCT = /[，。；：、！？（）「」『』“”‘’《》【】…—]/;
function linkEnd(s) {
  let end = s.search(/\[s[0-9a-f]{6}(?:[0-9a-f]{4})?\]/);
  for (let i = 0; i < (end < 0 ? s.length : end); i++) {
    if (s.charCodeAt(i) < 128) continue;
    if (FULL_PUNCT.test(s[i]) || /[A-Za-z0-9]/.test(s[i - 1] || "")) { end = i; break; }
  }
  return end;
}
const URL_START = /(?<![A-Za-z0-9@])(?:https?:\/\/|www\.)/gi;
// 一串字里的网址：从开头取到空白为止，按 linkEnd 截断，去掉末尾的标点和多出来的右括号。截下来的字里还可能有网址（中间没空格时 GFM 把它们连成了一个）
function linkNodes(value) {
  const out = [];
  let last = 0;
  URL_START.lastIndex = 0;
  for (let m; (m = URL_START.exec(value)); ) {
    const run = value.slice(m.index).match(/^[^\s<]+/)[0];
    const end = linkEnd(run);
    let link = (end < 0 ? run : run.slice(0, end)).replace(/[.,;:!?'*_~]+$/, "");
    while (link.endsWith(")") && link.split("(").length < link.split(")").length) link = link.slice(0, -1);
    if (link.length <= m[0].length) continue;
    if (m.index > last) out.push(txt(value.slice(last, m.index)));
    out.push({ type: "link", url: (/^www\./i.test(link) ? "http://" : "") + link, title: null, children: [txt(link)] });
    last = URL_START.lastIndex = m.index + link.length;
  }
  if (last < value.length) out.push(txt(value.slice(last)));
  return out;
}
function remarkLinks() {
  const walk = (node) => {
    const kids = node.children || [];
    for (let i = 0; i < kids.length; i++) {
      const a = kids[i];
      if (a.type !== "link") { walk(a); continue; }
      const t = a.children.length === 1 && a.children[0].type === "text" ? a.children[0].value : null;
      // 只管裸链接：文字就是网址（www. 开头的前面补了 http://），邮箱不管
      if (!t || !a.url.endsWith(t) || !/^(https?:\/\/|www\.)/i.test(t) || linkEnd(t) < 0) continue;
      const parts = linkNodes(t);
      kids.splice(i, 1, ...parts);
      i += parts.length - 1;
    }
  };
  return walk;
}

const CITE_ONE = /\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\]/g;
const CITE_TAIL = /\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\]|[，。；：、！？）」』”’,.;:!?)]+/y; // 角标后面紧跟的角标和标点
const citeNode = (sid) => ({ type: "element", tagName: "cite-mark", properties: { sid }, children: [] });

// 连着的几个角标和紧跟的标点，连同前面一个字放在一起不断行：角标是按钮，不然句号会单独掉到下一行、角标落在行首
function splitCites(value) {
  const out = [];
  let last = 0;
  for (const m of value.matchAll(CITE_ONE)) {
    if (m.index < last) continue; // 已经并进前一个角标那一串了
    let before = value.slice(last, m.index);
    const run = [];
    const b = before.match(/\S\s*$/u); // u：表情这类两个码元的字不劈开
    if (b) { run.push(txt(b[0])); before = before.slice(0, b.index); }
    if (before) out.push(txt(before));
    run.push(citeNode(m[1]));
    last = m.index + m[0].length;
    for (let t; (CITE_TAIL.lastIndex = last, t = CITE_TAIL.exec(value)); last = CITE_TAIL.lastIndex) run.push(t[1] ? citeNode(t[1]) : txt(t[0]));
    out.push({ type: "element", tagName: "span", properties: { className: ["md-nobr"] }, children: run });
  }
  if (!out.length) return null;
  if (last < value.length) out.push(txt(value.slice(last)));
  return out;
}

function rehypeCites() {
  const walk = (node) => {
    if (!node.children || node.tagName === "code" || node.tagName === "pre") return;
    for (let i = 0; i < node.children.length; i++) {
      const ch = node.children[i];
      if (ch.type === "text") {
        const parts = splitCites(ch.value);
        if (parts) { node.children.splice(i, 1, ...parts); i += parts.length - 1; }
      } else if (ch.type === "raw" && /^<br\s*\/?>$/i.test(ch.value.trim())) {
        node.children[i] = { type: "element", tagName: "br", properties: {}, children: [] }; // 表格格子里换行只能这么写
      } else walk(ch);
    }
  };
  return walk;
}

// ---------- 自己画的几样 ----------
const hastText = (node) => (node.type === "text" ? node.value : (node.children || []).map(hastText).join(""));

// cite：引用角标；download：HTML 代码块能不能下载（过程里的话不给）；ready：回答写完了（写完才能下载）
const MdContext = createContext({ cite: () => null, download: false, ready: false });

// 代码块：语言取自 language-xxx；公式块（KaTeX 还没取回来时）原样显示
function Pre({ node, children }) {
  const { download, ready } = useContext(MdContext);
  const code = node?.children?.find((x) => x.tagName === "code");
  const cls = (code?.properties?.className || []).map(String);
  const lang = (cls.find((x) => x.startsWith("language-")) || "").slice(9).toLowerCase();
  if (!code || lang === "math") return <pre>{children}</pre>;
  return <CodeBlock lang={lang} text={hastText(code).replace(/\n$/, "")} download={download} ready={ready} />;
}

// 组件要定在模块里：写在 components 里的箭头函数每次渲染都是新组件，角标、代码块会被卸掉重挂（新字一到就一次）
const CiteMark = ({ node }) => useContext(MdContext).cite(node.properties.sid);
const Table = ({ children }) => <MdTable>{children}</MdTable>;

const heading = (Tag) => function Heading({ node, ...props }) { return <Tag {...props} />; };
const COMPONENTS = {
  h1: heading("h3"), h2: heading("h4"), h3: heading("h5"), h4: heading("h6"), h5: heading("h6"), h6: heading("h6"), // 正文里的标题比页面标题小
  pre: Pre,
  table: Table,
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
  "cite-mark": CiteMark,
  img: ({ src, alt }) => (src ? <a href={src} target="_blank" rel="noreferrer">{alt || src}</a> : alt || null),
};

// 单个 ~ 不算删除线：中文常写“10~20 元”“3~5 天”，默认会把两个 ~ 之间划掉（Fusion 也这么关的）；删除线只认 ~~
const GFM = [remarkGfm, { singleTilde: false }];

// cite(sid) 返回引用角标；正文还在逐字出现时，最后一段可能写到一半，照常渲染（写到一半的符号见 research.tidyTyping）
export function Markdown({ text, cite, download = false, ready = false }) {
  const { text: src, math } = prep(text.replace(/\r\n/g, "\n"));
  const tex = useLazy("math", math);
  return (
    <div className={"md " + styles.content}>
      <MdContext.Provider value={{ cite, download, ready }}>
        <ReactMarkdown
          remarkPlugins={[GFM, remarkBreaks, remarkMath, remarkLinks]}
          rehypePlugins={[rehypeCites, ...(tex ? [tex] : [])]}
          components={COMPONENTS}>
          {src}
        </ReactMarkdown>
      </MdContext.Provider>
    </div>
  );
}
