// 对话页几处共用的小工具：状态文字、时间、来源的显示、实时片段和存下来的对话怎么合在一起、复制和导出的文字。
import { useEffect, useState } from "react";

export const ACTIVE = new Set(["queued", "running"]); // 服务器上还有执行没结束
export const QUEUED = "排队中，前面的任务跑完就开始";

// 时钟：有执行在跑每秒走一次，显示“已用 2 分 05 秒”；没有就每分钟走一次，过了零点，今天的时间换成写日期
export function useNow(active) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), active ? 1000 : 60000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

export function elapsed(since, now) {
  if (!since) return "";
  const s = Math.max(0, Math.round((now - Date.parse(since)) / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
}

// 今天的显示时刻，更早的显示日期
// 时间的写法，左栏和对话里一样：今天的只写钟点“14:32”，别的天写日期“10月30日”，不是今年的带上年份。
// 一句话、一条回答下面是 text：日期后面再跟钟点；full 精确到秒，放在悬停提示里
export function stamp(iso, now = Date.now()) {
  if (!iso) return null;
  const d = new Date(iso);
  const today = new Date(now);
  const pad = (n) => String(n).padStart(2, "0");
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const year = `${d.getFullYear()}年`;
  const day = d.toDateString() === today.toDateString() ? "" : `${d.getFullYear() === today.getFullYear() ? "" : year}${md}`;
  return { day, hm, text: day ? `${day} ${hm}` : hm, full: `${year}${md} ${hm}:${pad(d.getSeconds())}` };
}

// 左栏对话的时间：今天的写钟点，别的天只写日期
export function when(iso, now = Date.now()) {
  const s = stamp(iso, now);
  return s ? s.day || s.hm : "";
}

export const domain = (url) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
};

export const sourceName = (s) => s.title || domain(s.url);

// 正文里的引用：模型写的是来源 id，[s3fa9c1]
export const CITE = /\[(s[0-9a-f]{6}(?:[0-9a-f]{4})?)\]/g;
// 写到一半的引用（正文还在逐字出现，或者停在了半路）先不显示，免得露出 [sc91d6 这样的半截 id
export const cutPartialCite = (text) => text.replace(/\[(s[0-9a-f]{0,9})?$/, "");

// 表格表头下面的分隔行：| --- | :---: |
export const isRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
const TABLE_LINE = /^\s*\|/;

// 逐字出现时不该一个字一个字放的，显示到的位置 n 直接跳过去：
// - 引用 [s3fa9c1] 和链接的网址 ](https://…)：本身不显示，一个个字放只会让正文停一下。收全了整块跳过
// - 表格的分隔行 | --- | --- |：不显示，收全了整行跳过
// - 紧跟着的不显示的 Markdown 符号：换行、行首的列表符号 / # / >、** 和 `
export function typingUnit(text, n) {
  for (let next = unitEnd(text, n); next > n; next = unitEnd(text, n)) n = next;
  return n;
}

function unitEnd(text, n) {
  const k = text.lastIndexOf("[", n - 1);
  if (k >= 0 && n - k < 12) {
    const m = text.slice(k, k + 12).match(/^\[s[0-9a-f]{6}(?:[0-9a-f]{4})?\]/);
    if (m && k + m[0].length > n) return k + m[0].length;
  }
  const u = text.lastIndexOf("](", n - 1);
  if (u >= 0 && n - u < 300) {
    const m = text.slice(u, u + 300).match(/^\]\(https?:\/\/[^)\s]+\)/); // 和 Markdown 里认的链接一样
    if (m && u + m[0].length > n) return u + m[0].length;
  }
  const b = text.lastIndexOf("\n", n - 1) + 1;
  if (n > b && TABLE_LINE.test(text.slice(b, n)) && tableLinesAbove(text, b) === 1) {
    const e = text.indexOf("\n", n);
    if (e >= n) return e + 1; // 连换行一起：这一行不再是“正在写的那一行”
  }
  const m = text.slice(n, n + 8).match(n === b ? /^(\n+|[ \t]*(\d+[.)]|[-*+])[ \t]+|#{1,4}[ \t]+|>[ \t]?|\*\*|`)/ : /^(\n+|\*\*|`)/);
  return m ? n + m[0].length : n;
}

// 从 b 开始的这一行上面紧挨着几行表格（数到 2 为止）：0 是表头，1 是分隔行，2 起是数据行
function tableLinesAbove(text, b) {
  let c = 0;
  for (let e = b - 1; c < 2 && e > 0; c++) {
    const s = text.lastIndexOf("\n", e - 1) + 1;
    if (!TABLE_LINE.test(text.slice(s, e))) break;
    e = s - 1;
  }
  return c;
}

// 正在逐字出现的文字，最后一行可能停在半截的 Markdown 上：没配对的 ** 和 ` 先补上（刚出来的半个先去掉），
// 写到一半的链接先只显示文字，只有 # 或列表符号的行先不显示。表头一出来就按表格显示：分隔行还没写完时先补一行，
// 不然先露出一行竖线、等分隔行写完再突然变成表格。字出来时不先露出符号、等配上了再突然变样式
export function tidyTyping(text) {
  const lines = cutPartialCite(text).split("\n");
  const end = lines.length - (lines.length > 1 && !lines.at(-1).trim() ? 1 : 0); // 刚换行、新的一行还没字
  let j = end;
  while (j > 0 && TABLE_LINE.test(lines[j - 1])) j--;
  if (end - j === 1) lines.splice(j + 1, 0, "| --- |");
  else if (end - j === 2 && (end === lines.length || !isRule(lines[j + 1]))) lines[j + 1] = "| --- |"; // 分隔行还在写
  const t = lines.join("\n");
  const k = t.lastIndexOf("\n") + 1;
  let line = t.slice(k);
  if (/^\s*(#{1,4}|\d+[.)]?|[-*+])$/.test(line)) return t.slice(0, k);
  line = line.replace(/\[(?!s[0-9a-f]{6}(?:[0-9a-f]{4})?\])([^\]\n]*)(?:\](?:\([^)\s]*)?)?$/, "$1").replace(/([^*\s])\*$/, "$1");
  if ((line.split("**").length - 1) % 2) line = line.endsWith("**") ? line.slice(0, -2) : `${line}**`;
  if ((line.split("`").length - 1) % 2) line = line.endsWith("`") ? line.slice(0, -1) : `${line}\``;
  return t.slice(0, k) + line;
}
// 一条回答引用的来源，按第一次出现的先后编 1、2、3，每条回答各自编号。不在这条对话来源里的 id 不编号（角标显示问号）
export function citeNumbers(texts, sources) {
  const out = {};
  for (const t of texts) for (const m of t.matchAll(CITE)) if (sources?.[m[1]]) out[m[1]] ??= String(Object.keys(out).length + 1);
  return out;
}

// 一轮的条目分成过程和正文：最后一次搜索或读页之前的都是过程（包括中途说的话），之后的思考也算过程，之后的文字是正文。
// 正在跑时，正文后面又来了一步，那段文字就挪进过程（它确实是中途说的话）。被内容审核拦下的提示总是放在正文里
export function splitTurn(items) {
  const lastStep = items.findLastIndex((i) => i.type === "step");
  const process = [];
  const answer = [];
  items.forEach((it, k) => {
    if (it.type === "blocked") answer.push(it);
    else if (k <= lastStep || it.type === "thinking") process.push(it);
    else answer.push(it);
  });
  return { process, answer };
}

// 一条回答复制、导出时的文字：只要正文，引用换成页面上的编号，后面列出用到的来源（导出时写成 Markdown 链接）
export function answerText(items, sources, { links = false } = {}) {
  const nums = citeNumbers(items.filter((i) => i.type === "text").map((i) => i.text), sources);
  const body = splitTurn(items).answer.filter((i) => i.type === "text")
    .map((i) => cutPartialCite(i.text).replace(CITE, (_, sid) => (nums[sid] ? `[${nums[sid]}]` : ""))).join("\n\n").trim();
  const used = Object.entries(nums).map(([sid, n]) => {
    const s = sources[sid];
    return links ? `${n}. [${sourceName(s).replace(/[[\]]/g, "")}](${s.url})` : `[${n}] ${sourceName(s)} ${s.url}`;
  });
  return used.length ? `${body}\n\n来源：\n${links ? "\n" : ""}${used.join("\n")}` : body;
}

// 整段对话导出成 Markdown：每轮是用户的话（引用块）和回答，不含思考和搜索步骤
export function threadMarkdown(st) {
  const turns = st.turns.map((t) => {
    const asked = t.user.split("\n").map((l) => `> ${l}`).join("\n");
    let reply = answerText(t.items, st.sources, { links: true }) || "（这一轮没有回答）";
    if (t.record?.outcome === "stopped") reply += "\n\n（已停止）";
    if (t.items.some((i) => i.type === "blocked")) reply += "\n\n（内容审核没通过，这一轮停在这里）";
    return `${asked}\n\n${reply}`;
  });
  return `# ${st.title}\n\n${turns.join("\n\n---\n\n")}\n`;
}

// 复制到剪贴板。用 http 打开局域网地址时浏览器不给剪贴板接口，退回老办法
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch { /* 下面的老办法 */ }
  const ta = Object.assign(document.createElement("textarea"), { value: text });
  ta.style.cssText = "position:fixed;top:0;opacity:0";
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("浏览器不让复制");
}

// 存成文件下载
export function saveFile(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const itemKey = (i) => `${i.type}:${i.id}`;

// 这一轮的条目：存下来的（state.turns）为准，叠上还没存下来的实时片段。
// 思考和正文按（消息 id，种类）对上，工具步骤按调用 id 对上；存下来的步骤还在等结果时，用实时收到的：
// 结果已经来了用结果，子任务还在跑用它实时的条目（存下来的子任务要做完才有条目）。
// 重新回答时，旧回答的条目在新状态取回来之前就不显示了（live.hidden）
export function mergeItems(saved, live) {
  if (!live) return saved;
  const kept = live.hidden ? saved.filter((i) => !live.hidden.has(itemKey(i))) : saved;
  const have = new Map(kept.map((i) => [itemKey(i), i]));
  const out = kept.map((i) => {
    const l = i.type === "step" && i.pending ? live.items.find((x) => itemKey(x) === itemKey(i)) : null;
    return l ? { ...i, ...l } : i;
  });
  for (const i of live.items) if (!have.has(itemKey(i))) out.push(i);
  return out;
}

// 实时事件 → 这次执行里的条目（只记还在路上的；存下来以后 mergeItems 以存下来的为准）。
// 带 task 的是子 agent 的：放进派它的那个子任务（task 那一步）的 items，排法一样；它的阶段（排队、等模型）记在那一步上
export function addLive(l, ev, data) {
  if (!l) return l;
  if (data.task) return { ...l, items: addSub(l.items, ev, data) };
  const items = l.items.slice();
  if (ev === "delta") {
    const i = items.findIndex((x) => x.type === data.kind && x.id === data.id);
    // 想完了，思考那一段带着后端计的用时（ms）：以它为准，页面按收到的时间算的不准（中途打开时一下子补发、标签页在后台都会算短）
    const ms = data.ms != null ? { ms: data.ms } : {};
    if (i < 0) items.push({ type: data.kind, id: data.id, text: data.text, live: true, t0: Date.now(), ...ms });
    else items[i] = { ...items[i], text: items[i].text + data.text, ...ms };
    // 正文或下一步一开始，前面还没带用时的思考就算想完了，按页面这边的时间记
    if (data.kind === "text") stopThinking(items);
  } else if (ev === "step") {
    const s = { type: "step", ...data.step };
    const i = items.findIndex((x) => x.type === "step" && x.id === s.id);
    if (s.kind === "task") s.items = items[i]?.items || []; // 做完的那一步不带子 agent 的条目，留着实时收到的
    if (i < 0) items.push(s);
    else items[i] = s;
    stopThinking(items);
  } else if (ev === "phase") {
    return { ...l, phase: data.phase }; // 空档里在等什么（起标题、等模型……），页面据此说明
  } else {
    return l;
  }
  return { ...l, items };
}

function addSub(items, ev, { task, ...data }) {
  const out = items.slice();
  let i = out.findIndex((x) => x.type === "step" && x.id === task);
  // 子任务那一步总是先到；页面中途接上这次执行时可能没有，先占个位，mergeItems 叠到存下来的那一步上
  if (i < 0) i = out.push({ type: "step", kind: "task", id: task, pending: true, items: [] }) - 1;
  const sub = addLive({ items: out[i].items || [], phase: out[i].phase }, ev, data);
  out[i] = { ...out[i], items: sub.items, phase: sub.phase };
  return out;
}

function stopThinking(items) {
  for (let i = 0; i < items.length; i++) {
    if (items[i].type === "thinking" && items[i].live && items[i].ms == null) items[i] = { ...items[i], ms: Date.now() - items[i].t0 };
  }
}
