// 正文的小 Markdown：段落、标题、列表、引用块、表格、粗体、行内代码、链接，再加上引用角标 [s3fa9c1]。
// 只认这些常见写法，不认识的照原样显示；全部按文字渲染，不插入 HTML。
import React from "react";
import { isRule } from "./research.js";

const INLINE = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\])/g;

function inline(text, cite, key = "") {
  const out = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${n++}`;
    if (m[2]) out.push(<strong key={k}>{inline(m[2], cite, k)}</strong>);
    else if (m[3]) out.push(<code key={k}>{m[3]}</code>);
    else if (m[4]) out.push(<a key={k} href={m[5]} target="_blank" rel="noreferrer">{m[4]}</a>);
    else out.push(<React.Fragment key={k}>{cite(m[6])}</React.Fragment>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

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
      const items = [];
      for (; i < lines.length; i++) {
        const m = lines[i].match(ITEM);
        if (m) items.push({ indent: m[1].length, text: m[3] });
        else if (lines[i].trim() && items.length && /^\s+/.test(lines[i])) items.at(-1).text += " " + lines[i].trim(); // 续行
        else break;
      }
      const base = Math.min(...items.map((x) => x.indent));
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={`l${start}`}>
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
