// 右上角的通知（2026-11-13，定时任务第 3 步，照豆包）：定时任务在别的对话里跑完了，弹一条——钟图标、任务名、回答开头两行，
// 点了进那段对话，过几秒自己消失（鼠标停在上面、键盘走到里面、人没在看页面时不走表），右上角的 × 马上关掉。最多同时三条，新的在上。
// 哪些该弹由 Workspace 定（见 workspace.jsx 的 takeList：页面开着时新到的才弹，正看着的那段对话不弹）；左栏那一行的未读数是另一回事，不跟着通知消失。
// 外面这层一直在（读屏的播报区要先在页面上，换了内容才会念）
import { useEffect, useState } from "react";
import { away } from "./alert.jsx";
import { IconClock, IconClose } from "./icons.jsx";
import styles from "./notice.module.css";

const STAY_MS = 8000;
export const NOTICES_MAX = 3;

// 通知里任务名下面那一行：回答的开头；没有（没跑成、只出了文件没说话）就说一句
export const noticeLine = (n) => n.text || (n.ok ? "有新结果" : "这一次没有跑成");

function Notice({ n, href, onOpen, onClose }) {
  const [held, setHeld] = useState(false);
  useEffect(() => {
    if (held) return undefined;
    let timer = null;
    const arm = () => {
      clearTimeout(timer);
      timer = away() ? null : setTimeout(onClose, STAY_MS);
    };
    arm();
    document.addEventListener("visibilitychange", arm);
    addEventListener("focus", arm);
    addEventListener("blur", arm);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", arm);
      removeEventListener("focus", arm);
      removeEventListener("blur", arm);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [held]);
  return (
    <div className={styles.notice + (n.ok ? "" : " " + styles.failed)} onPointerEnter={() => setHeld(true)} onPointerLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setHeld(false); }}>
      <a className={styles.main} href={href} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onOpen(); }}>
        <span className={styles.icon}><IconClock size={16} stroke={1.8} /></span>
        <span className={styles.body}>
          <span className={styles.title}>{n.title}</span>
          <span className={styles.text}>{n.text}</span>
        </span>
      </a>
      <button className={styles.close} aria-label={`关闭“${n.title}”的通知`} onClick={onClose}><IconClose size={14} /></button>
    </div>
  );
}

// items：[{key, thread_id, title, text, ok}]，新的在前
export function Notices({ items, href, onOpen, onClose }) {
  return (
    <div className={styles.host} role="status" aria-live="polite">
      {items.map((n) => <Notice key={n.key} n={n} href={href(n.thread_id)} onOpen={() => onOpen(n)} onClose={() => onClose(n.key)} />)}
    </div>
  );
}
