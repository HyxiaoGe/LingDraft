// 答完提醒（评审第 46 条）：用户去了别的标签页、别的应用等着时，开着的这个对话答完（或没有答完）了，标签页标题前加“（答完了）”或“（没有答完）”，
// 网站图标右上角加一个靛青点；回到这个页面就恢复。用户在设置窗口里打开了通知并且浏览器允许了，再发一条系统通知，点通知回到这个页面。
// 只管开着的这个对话：别的对话在后台跑完，左栏里它的状态会变，这里不提醒
import { useEffect, useRef, useState } from "react";

// ---- 要不要发系统通知：在设置窗口（settings.jsx）里打开，记在本机（存不了就只记在这个页面里）。等待行旁边的“答完通知我”2026-11-07 Sean 定去掉 ----
const PREF = "ws-notify-done";
let wanted = null;
export function wants() {
  if (wanted == null) {
    try { wanted = localStorage.getItem(PREF) === "1"; } catch { wanted = false; }
  }
  return wanted;
}
export function setWants(v) {
  wanted = v;
  try { if (v) localStorage.setItem(PREF, "1"); else localStorage.removeItem(PREF); } catch { /* 存不了：这个页面里照样算数 */ }
}
export const permission = () => (typeof Notification === "undefined" ? "unsupported" : Notification.permission);

// 用户没在看这个页面：切去了别的标签页，或者在别的应用里
const away = () => document.hidden || !document.hasFocus();

// ---- 网站图标加点：拿原来的 favicon.svg，右上角画一个靛青点，描一圈纸色的边（深色底上看得清）。颜色取页面的设计变量 ----
let dotted = null;
async function dottedIcon() {
  if (dotted) return dotted;
  const css = getComputedStyle(document.querySelector(".ws") || document.documentElement);
  const text = await (await fetch("/favicon.svg")).text();
  const dot = `<circle cx="50" cy="14" r="12" fill="${css.getPropertyValue("--accent").trim()}" stroke="${css.getPropertyValue("--paper").trim()}" stroke-width="4"/>`;
  dotted = "data:image/svg+xml," + encodeURIComponent(text.replace("</svg>", `${dot}</svg>`));
  return dotted;
}
function setIcon(url) {
  for (const l of document.querySelectorAll('link[rel~="icon"]')) {
    if (l.dataset.was == null) {
      l.dataset.was = l.getAttribute("href");
      l.dataset.wasType = l.getAttribute("type") || "";
    }
    l.setAttribute("href", url || l.dataset.was);
    l.setAttribute("type", url ? "image/svg+xml" : l.dataset.wasType);
  }
}

// 返回标题前要加的字（“答完了”“没有答完”），没有就是 null。running：开着的对话这一轮在跑；st：它的状态；title：对话标题（通知里写）
export function useDoneAlert(tid, running, st, title) {
  const [done, setDone] = useState(null);
  const was = useRef({ tid, running });
  useEffect(() => {
    const prev = was.current;
    was.current = { tid, running };
    if (prev.tid !== tid) return setDone(null); // 换了对话
    if (!prev.running || running || !away()) return;
    const rec = st?.turns?.at(-1)?.record;
    const word = st?.status === "failed" || rec?.outcome === "stopped" || rec?.outcome === "blocked" ? "没有答完" : "答完了";
    setDone(word);
    if (wants() && permission() === "granted") {
      try {
        const n = new Notification(`灵稿${word}`, { body: title || "", tag: `lingdraft-${tid}` });
        n.onclick = () => {
          window.focus();
          n.close();
        };
      } catch { /* 有的浏览器（安卓 Chrome）只许从 Service Worker 发：标题和图标照样提醒 */ }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tid, running]);
  useEffect(() => {
    if (!done) return undefined;
    let off = false;
    dottedIcon().then((url) => !off && setIcon(url)).catch(() => {});
    const back = () => !away() && setDone(null);
    document.addEventListener("visibilitychange", back);
    addEventListener("focus", back);
    return () => {
      off = true;
      setIcon(null);
      document.removeEventListener("visibilitychange", back);
      removeEventListener("focus", back);
    };
  }, [done]);
  return done;
}
