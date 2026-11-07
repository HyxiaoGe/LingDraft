// 删了能撤销（评审第 44 条）：点删除马上从页面上拿掉，底部出“已删除……　撤销”，5 秒内点撤销原样放回；过了 5 秒才真的去删，
// 所以撤销是完整恢复，不用后端再存一份。对话列表、顶栏、记忆列表共用一个（同一时间只有一条提示）。
// - 鼠标停在提示上、键盘焦点在提示里时不走表，移开接着走（剩多少接着走多少）；
// - 又删了一条：上一条马上删掉，提示换成新的这条；
// - 关页面、整页刷新、离开对话页（去海报页）时还没过期的照样删，用 keepalive 发出去；
// - 键盘：提示出来时按 ⌘Z / Ctrl+Z 撤销（光标在有字的输入框里时归输入框自己的撤销），焦点在提示里按 Esc 是“不撤销了”，马上删。
//   删的那一行没了以后焦点放在哪，由删的地方自己定（列表里放到下一行），不抢到提示上：抽屉开着时提示在抽屉外面，抢过来 Tab 就出了抽屉
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { IconUndo } from "./icons.jsx";
import { NavContext, touch } from "./nav.jsx";
import styles from "./undo.module.css";

const UNDO_MS = 5000;
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const UNDO_KEY = MAC ? "⌘Z" : "Ctrl+Z";
// 光标在有字的输入框里：⌘Z 归输入框自己撤销打的字；空的输入框（比如顶栏删了以后光标落在新对话的输入框）照样撤销删除
const typing = (el) => {
  const f = el?.closest?.("input, textarea, [contenteditable='true']");
  return !!f && (f.isContentEditable ? f.textContent : f.value) !== "";
};

// 要撤销的一条：{kind: 删的是什么（"thread"、"memory"）, text: 提示里的话, commit({keepalive}): 真的去删（出错由它自己处理）, undo(): 放回去}
export function useUndo() {
  const [item, setItem] = useState(null);
  const cur = useRef(null); // 等着的那一条：{item, left: 还剩几毫秒, at: 这次开始走表的时间, timer}
  const api = useRef(null);
  if (!api.current) {
    const stopClock = (c) => {
      if (!c.timer) return;
      clearTimeout(c.timer);
      c.timer = null;
      c.left -= Date.now() - c.at;
    };
    const commit = (c, keepalive = false) => {
      if (!c || cur.current !== c) return;
      stopClock(c);
      cur.current = null;
      setItem(null);
      c.item.commit({ keepalive });
    };
    const startClock = (c) => {
      if (c.timer || cur.current !== c) return;
      c.at = Date.now();
      c.timer = setTimeout(() => commit(c), Math.max(0, c.left));
    };
    api.current = {
      push(it) {
        commit(cur.current); // 上一条不等了
        const c = { item: it, left: UNDO_MS, timer: null };
        cur.current = c;
        setItem(it);
        startClock(c);
      },
      undo() {
        const c = cur.current;
        if (!c) return;
        stopClock(c);
        cur.current = null;
        setItem(null);
        c.item.undo();
      },
      now: (keepalive = false) => commit(cur.current, keepalive), // 不撤销了：马上删
      flush(kind) { if (cur.current?.item.kind === kind) commit(cur.current); }, // 离开记忆列表（切回设置、关窗口）：记忆的不等了（回来再取时它还在后端，会又冒出来）
      pause: () => cur.current && stopClock(cur.current),
      resume: () => cur.current && startClock(cur.current),
    };
  }
  useEffect(() => {
    const a = api.current;
    const leave = () => a.now(true);
    const key = (e) => {
      if (!cur.current || e.repeat || !(MAC ? e.metaKey : e.ctrlKey) || e.shiftKey || e.altKey || e.code !== "KeyZ" || typing(e.target)) return;
      e.preventDefault();
      a.undo();
    };
    addEventListener("pagehide", leave);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("pagehide", leave);
      removeEventListener("keydown", key);
      leave(); // 离开对话页（去海报页）：不等了
    };
  }, []);
  return { ...api.current, item };
}

// 提示本身：桌面上在左下角；手机上在输入框上面（抽屉开着时贴底）。
// 外面这层一直在（读屏的播报区要先在页面上，换了内容才会念），有要撤销的才有内容
export function UndoToast({ undo }) {
  const it = undo.item;
  const nav = useContext(NavContext);
  const [lift, setLift] = useState(null); // 手机上离底多远：输入框那一块的高度
  useLayoutEffect(() => {
    if (!it || !nav?.mobile || nav.drawer) return setLift(null);
    const place = () => {
      const dock = document.querySelector("[data-dock]");
      setLift(dock ? Math.max(0, innerHeight - dock.getBoundingClientRect().top) : null);
    };
    place();
    addEventListener("resize", place);
    return () => removeEventListener("resize", place);
  }, [it, nav?.mobile, nav?.drawer]);
  return (
    <div className={styles.host} role="status" aria-live="polite" style={lift != null ? { bottom: lift + 8 } : undefined}>
      {it && (
        <div className={styles.toast} data-esc onPointerEnter={undo.pause} onPointerLeave={undo.resume} onFocus={undo.pause}
          onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) undo.resume(); }}
          onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); undo.now(); } }}>
          <span className={styles.text}>{it.text}</span>
          <button className={styles.act} title={`撤销（${UNDO_KEY}）`} aria-keyshortcuts={MAC ? "Meta+Z" : "Control+Z"} onClick={undo.undo}>
            <IconUndo size={14} />撤销{!touch() && <kbd className="ws-kbd" aria-hidden="true">{UNDO_KEY}</kbd>}
          </button>
        </div>
      )}
    </div>
  );
}
