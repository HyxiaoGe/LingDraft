// 正文的小 Markdown：段落、标题、列表、引用块、表格、粗体、行内代码、链接，再加上引用角标 [s3fa9c1]。
// 只认这些常见写法，不认识的照原样显示；全部按文字渲染，不插入 HTML。
import React from "react";
import { isRule } from "./research.js";

const INLINE = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\])/g;
const CITE_TAIL = /\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\]|[，。；：、！？）」』”’,.;:!?)]+/y; // 角标后面紧跟的角标和标点

function inline(text, cite, key = "") {
  const out = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index < last) continue; // 已经并进前一个角标那一串了
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${n++}`;
    last = m.index + m[0].length;
    if (m[2]) out.push(<strong key={k}>{inline(m[2], cite, k)}</strong>);
    else if (m[3]) out.push(<code key={k}>{m[3]}</code>);
    else if (m[4]) out.push(<a key={k} href={m[5]} target="_blank" rel="noreferrer">{m[4]}</a>);
    else { // 连着的几个角标和紧跟的标点，连同前面一个字放在一起不断行：角标是按钮，不然句号会单独掉到下一行、角标落在行首
      const run = [cite(m[6])];
      const before = typeof out.at(-1) === "string" && out.at(-1).match(/\S\s*$/u); // u：表情这类两个码元的字不劈开
      if (before) {
        out[out.length - 1] = out.at(-1).slice(0, before.index);
        run.unshift(before[0]);
      }
      for (let t; (CITE_TAIL.lastIndex = last, t = CITE_TAIL.exec(text)); last = CITE_TAIL.lastIndex) run.push(t[1] ? cite(t[1]) : t[0]);
      out.push(<span key={k} className="md-nobr">{run.map((x, j) => <React.Fragment key={j}>{x}</React.Fragment>)}</span>);
    }
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

// 第 i 行是空行，往下第一行非空的还是这个列表的项（同一种编号，或者缩进的子项）：项之间空一行的写法不拆成几个列表
function listGoesOn(lines, i, ordered, indent) {
  while (i < lines.length && !lines[i].trim()) i++;
  const m = i < lines.length && lines[i].match(ITEM);
  return !!m && (m[1].length > indent || /^\d/.test(m[2]) === ordered);
}

// cite(sid) 返回引用角标；正文还在逐字出现时，最后一段可能写到一半，照常渲染。
// 每块的 key 用它开始的那一行：逐字出现时段落、列表往下长，key 不变，已经显示的部分不会整块重建
export function Markdown({ text, cite }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    const start = i;
    if (!line.trim()) { i++; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const Tag = `h${Math.min(h[1].length + 2, 6)}`; // 正文里的标题比页面标题小
      blocks.push(<Tag key={i}>{inline(h[2], cite, `h${i}`)}</Tag>);
      i++;
      continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) { blocks.push(<hr key={i} />); i++; continue; }
    if (line.trim().startsWith("|") && i + 1 < lines.length && isRule(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      let j = i + 2;
      for (; j < lines.length && lines[j].trim().startsWith("|"); j++) rows.push(cells(lines[j]));
      blocks.push(
        <div key={i} className="md-table">
          <table>
            <thead><tr>{head.map((c, k) => <th key={k}>{inline(c, cite, `t${i}h${k}`)}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => <tr key={ri}>{head.map((_, k) => <td key={k}>{inline(r[k] || "", cite, `t${i}r${ri}c${k}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      i = j;
      continue;
    }
    if (line.startsWith(">")) {
      const quote = [];
      for (; i < lines.length && lines[i].startsWith(">"); i++) quote.push(lines[i].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={`q${start}`}>{inline(quote.join(" "), cite, `q${start}`)}</blockquote>);
      continue;
    }
    if (ITEM.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const first = ordered ? parseInt(line, 10) : 1; // 从模型写的第一个号开始；后面的号由列表自己数
      const items = [];
      let loose = false; // 项之间空了行：项和项之间也留出空隙
      for (; i < lines.length; i++) {
        const m = lines[i].match(ITEM);
        if (m) items.push({ indent: m[1].length, text: m[3] });
        else if (lines[i].trim() && items.length && /^\s+/.test(lines[i])) items.at(-1).text += " " + lines[i].trim(); // 续行
        else if (!lines[i].trim() && listGoesOn(lines, i, ordered, items[0].indent)) loose = true;
        else break;
      }
      const base = Math.min(...items.map((x) => x.indent));
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={`l${start}`} start={first !== 1 ? first : undefined} className={loose ? "loose" : undefined}>
          {items.map((x, k) => <li key={k} className={x.indent > base ? "sub" : undefined}>{inline(x.text, cite, `l${start}-${k}`)}</li>)}
        </List>,
      );
      continue;
    }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|\s*([-*+]|\d+[.)])\s)/.test(lines[i]) && !(lines[i].trim().startsWith("|") && isRule(lines[i + 1] || "")); i++) {
      para.push(lines[i]);
    }
    if (!para.length) { para.push(lines[i]); i++; }
    blocks.push(<p key={`p${start}`}>{para.flatMap((l, k) => (k ? [<br key={`br${k}`} />, ...inline(l, cite, `p${start}-${k}`)] : inline(l, cite, `p${start}-${k}`)))}</p>);
  }
  return <div className="md">{blocks}</div>;
}
