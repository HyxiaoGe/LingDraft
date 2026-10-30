// 对话页几处共用的小工具：状态文字、时间、来源的显示、实时片段和存下来的对话怎么合在一起。
import { useEffect, useState } from "react";

export const ACTIVE = new Set(["queued", "running"]); // 服务器上还有执行没结束
export const QUEUED = "排队中，前面的任务跑完就开始";

// 每秒走一次的时钟：显示“已用 2 分 05 秒”
export function useNow(active) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
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
export function when(iso, now = Date.now()) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date(now);
  const pad = (n) => String(n).padStart(2, "0");
  return d.toDateString() === today.toDateString() ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
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
export const cutPartialCite = (text) => text.replace(/\[s[0-9a-f]{0,9}$/, "");
// 一条回答引用的来源，按第一次出现的先后编 1、2、3，每条回答各自编号。不在这条对话来源里的 id 不编号（角标显示问号）
export function citeNumbers(texts, sources) {
  const out = {};
  for (const t of texts) for (const m of t.matchAll(CITE)) if (sources?.[m[1]]) out[m[1]] ??= String(Object.keys(out).length + 1);
  return out;
}

export const itemKey = (i) => `${i.type}:${i.id}`;

// 这一轮的条目：存下来的（state.turns）为准，叠上还没存下来的实时片段。
// 思考和正文按（消息 id，种类）对上，工具步骤按调用 id 对上；存下来的步骤还在等结果时，用实时收到的结果。
// 重新回答时，旧回答的条目在新状态取回来之前就不显示了（live.hidden）
export function mergeItems(saved, live) {
  if (!live) return saved;
  const kept = live.hidden ? saved.filter((i) => !live.hidden.has(itemKey(i))) : saved;
  const have = new Map(kept.map((i) => [itemKey(i), i]));
  const out = kept.map((i) => {
    const l = i.type === "step" && i.pending ? live.items.find((x) => itemKey(x) === itemKey(i)) : null;
    return l && !l.pending ? l : i;
  });
  for (const i of live.items) if (!have.has(itemKey(i))) out.push(i);
  return out;
}

// 实时事件 → 这次执行里的条目（只记还在路上的；存下来以后 mergeItems 以存下来的为准）
export function addLive(l, ev, data) {
  if (!l) return l;
  const items = l.items.slice();
  if (ev === "delta") {
    const i = items.findIndex((x) => x.type === data.kind && x.id === data.id);
    if (i < 0) items.push({ type: data.kind, id: data.id, text: data.text, live: true, t0: Date.now() });
    else items[i] = { ...items[i], text: items[i].text + data.text };
    // 正文或下一步一开始，前面的思考就算想完了：记下用了多久
    if (data.kind === "text") stopThinking(items);
  } else if (ev === "step") {
    const s = { type: "step", ...data.step };
    const i = items.findIndex((x) => x.type === "step" && x.id === s.id);
    if (i < 0) items.push(s);
    else items[i] = s;
    stopThinking(items);
  } else {
    return l;
  }
  return { ...l, items };
}

function stopThinking(items) {
  for (let i = 0; i < items.length; i++) {
    if (items[i].type === "thinking" && items[i].live && !items[i].ms) items[i] = { ...items[i], ms: Date.now() - items[i].t0 };
  }
}
