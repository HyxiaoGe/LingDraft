// 答完提醒（评审第 46 条）：用户去了别的标签页、别的应用等着时，开着的这个对话答完（或没答完）了，标签页标题前加“（答完了）”或“（没答完）”，
// 网站图标右上角加一个靛青点；回到这个页面就恢复。用户点过“答完通知我”并且浏览器允许了，再发一条系统通知，点通知回到这个页面。
// 只管开着的这个对话：别的对话在后台跑完，左栏里它的状态会变，这里不提醒
import { useEffect, useRef, useState } from "react";
import { IconBell } from "./icons.jsx";

// ---- 要不要发系统通知：点了“答完通知我”记在本机（存不了就只记在这个页面里） ----
const PREF = "ws-notify-done";
let wanted = null;
function wants() {
  if (wanted == null) {
    try { wanted = localStorage.getItem(PREF) === "1"; } catch { wanted = false; }
  }
  return wanted;
}
function setWants(v) {
  wanted = v;
  try { if (v) localStorage.setItem(PREF, "1"); else localStorage.removeItem(PREF); } catch { /* 存不了：这个页面里照样算数 */ }
}
const permission = () => (typeof Notification === "undefined" ? "unsupported" : Notification.permission);

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

// 返回标题前要加的字（“答完了”“没答完”），没有就是 null。running：开着的对话这一轮在跑；st：它的状态；title：对话标题（通知里写）
export function useDoneAlert(tid, running, st, title) {
  const [done, setDone] = useState(null);
  const was = useRef({ tid, running });
  useEffect(() => {
    const prev = was.current;
    was.current = { tid, running };
    if (prev.tid !== tid) return setDone(null); // 换了对话
    if (!prev.running || running || !away()) return;
    const rec = st?.turns?.at(-1)?.record;
    const word = st?.status === "failed" || rec?.outcome === "stopped" || rec?.outcome === "blocked" ? "没答完" : "答完了";
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

// 等待行旁边的“答完通知我”：这一轮跑了 10 秒以上才出；浏览器不支持通知、或者已经拒绝过就不出（标题和图标的提醒照样有）。
// 点了才向浏览器要权限；允许了记在本机，以后每轮都通知，再点一下取消。since：这一轮开始的时间（毫秒）
const AFTER = 10_000;
export function NotifyToggle({ since, now }) {
  const [on, setOn] = useState(wants);
  const [perm, setPerm] = useState(permission);
  const [refused, setRefused] = useState(false);
  if (refused) return <span className="ws-notify-note">浏览器没允许通知，答完时标签页标题会提示</span>;
  if (perm === "unsupported" || perm === "denied" || now - since < AFTER) return null;
  const active = on && perm === "granted";
  const click = async () => {
    if (active) {
      setWants(false);
      setOn(false);
      return;
    }
    const p = perm === "granted" ? perm : await Notification.requestPermission();
    setPerm(p);
    if (p === "granted") {
      setWants(true);
      setOn(true);
    } else if (p === "denied") setRefused(true);
  };
  return (
    <button className={"ws-notify" + (active ? " on" : "")} aria-pressed={active} onClick={click}>
      <IconBell size={13} />{active ? "答完会通知你" : "答完通知我"}
    </button>
  );
}
