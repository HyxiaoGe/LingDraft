// 对话页几处共用的小工具：状态文字、时间、来源的显示、实时片段和存下来的对话怎么合在一起、复制和导出的文字。
import { useEffect, useState } from "react";

export const ACTIVE = new Set(["queued", "running"]); // 服务器上还有执行没结束
export const QUEUED = "排队中，轮到了就开始";

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

// 每轮记下的模型（"厂商:模型"）显示成什么：能选的模型里用它的那个的名字（Auto 在第一个，跳过它，Auto 的那轮显示它实际用的）；
// 列表里没有的（不再提供的）写“已不再提供的模型”，不露后端的模型代号（批次 G 第 40 条）；列表还没取到时先不写
export function modelName(models, ref, gone = "已不再提供的模型") {
  if (!models?.length) return null;
  return models.slice(1).find((m) => m.model === ref)?.name || gone;
}

// 多少额度（后端按各模型的单价算好的 credits，1 credit = ¥0.001 成本；页面上叫“额度”，不露 credits）：
// 一律取整、千位加逗号，不到 1 写“不到 1 额度”，不换算成次数（2026-11-07 Sean 定，批次 G 第 39 条）
export function creditsText(n) {
  if (n == null) return null;
  return n < 1 ? "不到 1 额度" : `${Math.round(n).toLocaleString("en-US")} 额度`;
}

// 额度里最紧的那个窗口（自己的：今天、这周；所有游客合计的那个不显示），加上剩多少、是不是该提醒（用了 warn_at 以上）、是不是用完了。
// 只给最紧的那个：周额度只在它比今天剩的还少时才出现（2026-11-07 Sean 定，批次 E 第 27 条）。没取到、管理员不限是 null
export function creditsLeft(credits) {
  const ws = (credits?.windows || []).filter((w) => (w.kind === "day" || w.kind === "week") && w.limit != null);
  if (!ws.length) return null;
  const w = ws.reduce((a, b) => (b.limit - b.used < a.limit - a.used ? b : a));
  const remaining = Math.max(0, w.limit - w.used);
  const exhausted = w.used >= w.limit;
  return { ...w, remaining, exhausted, warn: w.used >= w.limit * (credits.warn_at ?? 0.8),
    span: w.kind === "week" ? "这周" : "今天", resetText: w.kind === "week" ? "下周一 0 点恢复" : "明天 0 点恢复" };
}

// 千分位整数（额度的数都这么写）
export const num = (n) => Math.floor(n).toLocaleString("en-US");

// 额度在页面上的说法（第 27 条：任何地方看到的都是这一种）。只说剩多少、什么时候恢复，不换算成次数、不加解释（2026-11-07 Sean 定：简单点）。
// left 是 creditsLeft 的结果：还有：“今天还剩 7,300 额度 · 明天 0 点恢复”；用完了：“今天的额度用完了 · 明天 0 点恢复”
export function creditsLine(left) {
  return left ? creditsParts(left).join(" · ") : null;
}

// 上面那句的两半：[还剩多少（或用完了）, 什么时候恢复]
function creditsParts(left) {
  const amount = left.exhausted ? `${left.span}的额度用完了` : `${left.span}还剩 ${num(left.remaining)} 额度`;
  return [amount, left.resetText];
}

// 左下角额度按钮上那几个字：剩多少；用完了写“额度用完了”
export function creditsShort(left) {
  return left.exhausted ? "额度用完了" : `剩 ${num(left.remaining)} 额度`;
}

// 游客登录以后会怎样：“登录后每天 10,000 额度”（login 来自 /api/credits 或 429 的 detail，见后端 app.quota）。
// 没有（已经登录、后端还没更新）是 null。prefix 为 false 时不带“登录”两个字（前面放的是登录按钮）
export function loginLine(src, prefix = true) {
  const day = src?.login?.day;
  if (!day) return null;
  return `${prefix ? "登录" : ""}后每天 ${num(day)} 额度`;
}

// 时间全站一种写法（批次 G 第 38 条）：今天“14:32”，昨天“昨天 14:32”，明天“明天 03:00”，今年别的天“11月5日 14:32”，往年“2025年11月5日”（不写钟点）。
// day 是哪天（今天为空串），hm 是钟点，text 是上面那种写法，full 精确到秒、放在悬停提示里
export function stamp(iso, now = Date.now()) {
  if (!iso) return null;
  const d = new Date(iso);
  const today = new Date(now);
  const pad = (n) => String(n).padStart(2, "0");
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const year = `${d.getFullYear()}年`;
  const off = (n) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + n).toDateString() === d.toDateString();
  const other = d.getFullYear() !== today.getFullYear();
  const day = off(0) ? "" : off(-1) ? "昨天" : off(1) ? "明天" : other ? `${year}${md}` : md;
  return { day, hm, text: !day ? hm : other && !off(-1) && !off(1) ? day : `${day} ${hm}`, full: `${year}${md} ${hm}:${pad(d.getSeconds())}` };
}

// 写在一句话里的：今天的也说出“今天”（“今天 03:00 整理过”），别的同 text
export const sayWhen = (s) => (s.day ? s.text : `今天 ${s.hm}`);

// 左栏对话的时间：“今天”“昨天”两组里只写钟点（组名已经说了是哪天）；别的组只写哪天（今天、昨天、11月5日），一列里不混钟点和日期。
// 搜对话的窗口里不分组，照豆包写 stamp 的 text（14:32、昨天 20:53、11月5日 17:01）
export function when(iso, now = Date.now(), group) {
  const s = stamp(iso, now);
  return !s ? "" : group === "today" || group === "yesterday" ? s.hm : s.day || "今天";
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
const isRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
// 代码块的围栏行：``` 或 ~~~（三个起），后面可以跟语言名；反引号围栏的语言名里不能有反引号
export const FENCE = /^(\s*)(`{3,}|~{3,})([^`]*)$/;
// 收尾的围栏：和开头同一种符号、不比开头短，后面没有别的字
export const closesFence = (line, open) => {
  const m = line.match(FENCE);
  return !!m && m[2][0] === open[0] && m[2].length >= open.length && !m[3].trim();
};

// 这几行走完时还在代码块里（开了围栏还没收）
function fenceOpen(lines) {
  let open = null;
  for (const l of lines) {
    if (open) { if (closesFence(l, open)) open = null; }
    else open = l.match(FENCE)?.[2] || null;
  }
  return !!open;
}
const TABLE_LINE = /^\s*\|/;

// 还在写的文字，最后一行可能停在半截的 Markdown 上：没配对的 ** 和 ` 先补上（刚出来的半个先去掉），
// 写到一半的链接先只显示文字，只有 # 或列表符号的行先不显示。表头一出来就按表格显示：分隔行还没写完时先补一行，
// 不然先露出一行竖线、等分隔行写完再突然变成表格。字出来时不先露出符号、等配上了再突然变样式。
// 代码块里的、围栏那一行原样显示（代码里的 ` 和 ** 不是 Markdown）；只写了一两个 ` 的行先不显示（可能是围栏写到一半）
export function tidyTyping(text) {
  const lines = cutPartialCite(text).split("\n");
  const end = lines.length - (lines.length > 1 && !lines.at(-1).trim() ? 1 : 0); // 刚换行、新的一行还没字
  if (end > 0 && (fenceOpen(lines.slice(0, end - 1)) || FENCE.test(lines[end - 1]))) return lines.join("\n");
  let j = end;
  while (j > 0 && TABLE_LINE.test(lines[j - 1])) j--;
  if (end - j === 1) lines.splice(j + 1, 0, "| --- |");
  else if (end - j === 2 && (end === lines.length || !isRule(lines[j + 1]))) lines[j + 1] = "| --- |"; // 分隔行还在写
  const t = lines.join("\n");
  const k = t.lastIndexOf("\n") + 1;
  let line = t.slice(k);
  if (/^\s*(#{1,4}|\d+[.)]?|[-*+]|`{1,2}|~{1,2})$/.test(line)) return t.slice(0, k);
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
    else if (k <= lastStep || it.type === "thinking" || it.type === "guide") process.push(it); // 用户插进这一轮的话也在过程里
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

// 整段对话导出成 Markdown：每轮是用户的话（引用块，回答过程中插进去的话接在后面）和回答，不含思考和搜索步骤
export function threadMarkdown(st) {
  const quote = (text) => text.split("\n").map((l) => `> ${l}`).join("\n");
  const turns = st.turns.map((t) => {
    const asked = [t.user, ...t.items.filter((i) => i.type === "guide").map((i) => `（回答过程中插话）${i.text}`)].map(quote).join("\n>\n");
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
// 做完的那一步带着它得到的来源（子 agent 的也算这条线程的），记在 sources 里：一轮跑完才存，执行中库里没有这一轮的来源。
// 带 task 的是子 agent 的：放进派它的那个子任务（task 那一步）的 items，排法一样；它的阶段（排队、等模型）记在那一步上
export function addLive(l, ev, data) {
  if (!l) return l;
  if (data.sources) l = { ...l, sources: { ...l.sources, ...data.sources } };
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
  } else if (ev === "guide") {
    if (!items.some((x) => x.type === "guide" && x.id === data.id)) items.push({ type: "guide", ...data }); // 用户插进这一轮的话放进对话了
    stopThinking(items);
  } else if (ev === "phase") {
    return { ...l, phase: data.phase }; // 空档里在等什么（等模型、重写参数……），页面据此说明
  } else if (ev === "restart") {
    // 模型生成到一半断了、正在再调一次：刚才流出来的半截（思考和正文，id 是那条消息的；没流出东西时是空的）不要了，空档里说明在重新生成
    const kept = data.id ? items.filter((x) => !((x.type === "thinking" || x.type === "text") && x.id === data.id)) : items;
    return { ...l, items: kept, phase: "restart" };
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
