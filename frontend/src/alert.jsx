// 答完提醒（评审第 46 条）：用户去了别的标签页、别的应用等着时，开着的这个对话答完（或没有答完、停下来等他回答）了，标签页标题前加“（答完了）”“（没有答完）”或“（等你回答）”，
// 网站图标右上角加一个靛青点；回到这个页面就恢复。用户在设置窗口里打开了通知并且浏览器允许了，再发一条系统通知，点通知回到这个页面。
// 只管开着的这个对话：别的对话在后台跑完，左栏里它的状态会变，这里不提醒。
// 定时任务跑完的（2026-11-13，见最下面的 useTaskAlert）是另一路：哪段对话的都提醒，标题前加“（定时任务有新结果）”，系统通知里是任务名和回答的开头
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
export const away = () => document.hidden || !document.hasFocus();

// ---- 网站图标加点：拿 32 像素的网站图标，右上角画一个靛青点，描一圈纸色的边。颜色取页面的设计变量 ----
let dotted = null;
async function dottedIcon() {
  if (dotted) return dotted;
  const css = getComputedStyle(document.querySelector(".ws") || document.documentElement);
  const img = new Image();
  img.src = "/icon-32.png";
  await img.decode();
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const g = c.getContext("2d");
  g.drawImage(img, 0, 0, 32, 32);
  g.beginPath();
  g.arc(25, 7, 6, 0, 2 * Math.PI);
  g.fillStyle = css.getPropertyValue("--accent").trim();
  g.fill();
  g.lineWidth = 2;
  g.strokeStyle = css.getPropertyValue("--paper").trim();
  g.stroke();
  dotted = c.toDataURL("image/png");
  return dotted;
}
function setIcon(url) {
  for (const l of document.querySelectorAll('link[rel~="icon"]')) {
    if (l.dataset.was == null) {
      l.dataset.was = l.getAttribute("href");
      l.dataset.wasType = l.getAttribute("type") || "";
    }
    l.setAttribute("href", url || l.dataset.was);
    l.setAttribute("type", url ? "image/png" : l.dataset.wasType);
  }
}

// 人不在时亮着的记号（标题前要加的字）：亮着时网站图标带点，人回到这个页面就灭。
// 答完提醒、定时任务的通知各有一个：图标上的点有一个亮着就留着
let lit = 0;
function dot(on) {
  lit += on ? 1 : -1;
  if (!lit) setIcon(null);
  else if (on && lit === 1) dottedIcon().then((url) => lit > 0 && setIcon(url)).catch(() => {});
}
function useAwayMark() {
  const [mark, setMark] = useState(null);
  const on = !!mark;
  useEffect(() => {
    if (!on) return undefined;
    dot(true);
    const back = () => !away() && setMark(null);
    document.addEventListener("visibilitychange", back);
    addEventListener("focus", back);
    return () => {
      dot(false);
      document.removeEventListener("visibilitychange", back);
      removeEventListener("focus", back);
    };
  }, [on]);
  return [mark, setMark];
}

// 返回标题前要加的字（“答完了”“没有答完”“等你回答”），没有就是 null。running：开着的对话这一轮在跑；st：它的状态；title：对话标题（通知里写）
export function useDoneAlert(tid, running, st, title) {
  const [done, setDone] = useAwayMark();
  const was = useRef({ tid, running });
  useEffect(() => {
    const prev = was.current;
    was.current = { tid, running };
    if (prev.tid !== tid) return setDone(null); // 换了对话
    if (!prev.running || running || !away()) return;
    const rec = st?.turns?.at(-1)?.record;
    const word = st?.status === "asking" ? "等你回答" : st?.status === "failed" || rec?.outcome === "stopped" || rec?.outcome === "blocked" ? "没有答完" : "答完了";
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
  return done;
}

// 定时任务跑完了而用户没在看这个页面：标题前加“定时任务有新结果”（没跑成是“定时任务没跑成”）、图标加点；
// 打开了通知的再发一条系统通知——任务名、回答的开头，点了回到页面并打开那段对话。人在看页面时什么都不做（页面右上角自己会弹，见 notice.jsx）。
// 返回 [标题前要加的字, 来了一条通知时调的 raise(n, open)]：n 是 {thread_id, title, text}，ok 是跑成没有；open(thread_id) 打开那段对话。
// 开着的那段对话在跑时这里和答完提醒都会发：两条用同一个 tag，后到的这条（带任务名和回答开头）换掉前一条，不响第二次
export function useTaskAlert() {
  const [mark, setMark] = useAwayMark();
  const raise = (n, open) => {
    if (!away()) return;
    setMark(n.ok ? "定时任务有新结果" : "定时任务没跑成");
    if (!wants() || permission() !== "granted") return;
    try {
      const x = new Notification(n.title, { body: n.text, tag: `lingdraft-${n.thread_id}` });
      x.onclick = () => {
        window.focus();
        x.close();
        open(n.thread_id);
      };
    } catch { /* 同上：有的浏览器只许从 Service Worker 发 */ }
  };
  return [mark, raise];
}
