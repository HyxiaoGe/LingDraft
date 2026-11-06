// 输入框里没发出去的字：每个对话各存一份在本机（localStorage），新对话记在 "new" 下。
// 切去记忆页、海报页、刷新、登录（整页重载，游客的对话并进账号，id 不变）回来都还在；发出去就清掉，退出登录时全部清掉（见 auth.js 的 logout）。
// 最多留 30 份、7 天不碰就过期；存不下（隐私模式、满了）就只在这次打开的页面里有效，和以前一样
const KEY = "lingdraft.drafts";
const MAX = 30;
const TTL = 7 * 24 * 3600 * 1000;

function load() {
  try {
    const d = JSON.parse(localStorage.getItem(KEY) || "{}");
    return d && typeof d === "object" && !Array.isArray(d) ? d : {};
  } catch {
    return {};
  }
}

function store(d) {
  const now = Date.now();
  const kept = Object.entries(d)
    .filter(([, e]) => typeof e?.text === "string" && e.text && now - e.at < TTL)
    .sort((a, b) => b[1].at - a[1].at)
    .slice(0, MAX);
  try {
    if (kept.length) localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(kept)));
    else localStorage.removeItem(KEY);
  } catch { /* 存不下就算了 */ }
}

export function getDraft(key) {
  const e = load()[key];
  return e && typeof e.text === "string" && Date.now() - e.at < TTL ? e.text : "";
}

// 每次都重新读一遍再写：别的标签页存的草稿不会被这里盖掉
export function setDraft(key, text) {
  const d = load();
  if (text.trim()) d[key] = { text, at: Date.now() };
  else delete d[key];
  store(d);
}

export function clearDrafts() {
  try { localStorage.removeItem(KEY); } catch { /* 同上 */ }
}
