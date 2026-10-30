// 调研工作台几处共用的小工具：状态文字、时间、来源的合并与显示。
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

export function scopeLine(scope) {
  if (!scope) return "";
  const parts = [["时间", scope.time_range], ["地域", scope.region], ["信源", scope.lang], ["不含", scope.exclude], ["交付", scope.deliverable], ["补充", scope.extra]]
    .filter(([, v]) => v).map(([k, v]) => `${k} ${v}`);
  return parts.length ? parts.join(" · ") : "按需求原话定了，没有额外限制";
}

export const domain = (url) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
};

export const sourceName = (s) => s.title || domain(s.url);
// 报告块：交付以后存在 report_blocks 里，终审时随提问一起来
export const reportBlocks = (st) => (st?.report_blocks?.length ? st.report_blocks : st?.interrupt?.blocks || []);
// 来源在这版报告里的编号（[3]）：后端按正文里第一次出现的先后编，每版重新编；报告没引用的来源没有编号
export const sourceNumbers = (blocks) => Object.fromEntries(blocks.filter((b) => b.type === "source").map((b) => [b.source_id, b.label]));
export const SSTATUS = { candidate: "候选", adopted: "系统选用", excluded: "你排除了" };

export const CHECK_LABEL = {
  answer_present: "调研有答案", claims_sourced: "论断有来源", no_excluded_sources: "没引用已排除的来源", inference_marked: "推断已标记",
  sources_complete: "来源信息完整", no_orphaned_claims: "没有失去来源的论断", scope_time: "时间在范围内",
  report_valid: "报告有带来源的论断", question_answered: "回答了研究问题",
};
export const checkLabel = (c) => CHECK_LABEL[c.name] || c.name;

// 这一轮调研已经做过的步骤（服务器存下来的）+ 还没存下来的实时进度（按工具调用 id 合并，存下来的为准）
export function roundSteps(saved, acts) {
  const ids = new Set(saved.map((s) => s.id));
  const pending = acts.filter((a) => !ids.has(a.id)).map((a) => a.step || { id: a.id, kind: a.kind, text: a.stage, pending: true });
  return [...saved, ...pending];
}

// 还没有报告时右栏列的来源：已入库的 + 这一轮先算出编号的 + 实时进度里刚找到的（按网址去重，读到正文的优先）
export function foundSources(st, acts) {
  const out = new Map();
  const put = (s) => {
    const prev = out.get(s.url);
    if (!prev) out.set(s.url, s);
    else if (s.body && !prev.body) out.set(s.url, { ...prev, body: true, title: prev.title || s.title });
  };
  for (const s of st?.sources || []) put({ id: s.id, url: s.url, title: s.title, body: s.excerpt_kind === "body", published_at: s.published_at });
  for (const s of st?.current_round?.sources || []) put({ id: s.id, url: s.url, title: s.title, body: s.excerpt_kind === "body", published_at: s.published_at });
  for (const a of acts || []) for (const f of a.found || []) put({ id: null, url: f.url, title: f.title, body: f.body });
  return [...out.values()];
}
