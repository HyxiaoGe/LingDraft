// 对话的“⋯”菜单（左栏每一行、顶栏都用）：桌面上是贴着按钮的小浮层，手机上是底部弹层（规范：手机上菜单用 bottom sheet）。
// 还有改标题的输入框：左栏的行里、顶栏标题处都用，右边写着字数和上限（第 14 条之外顺带的“重命名显示字数上限”）
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLayer } from "./layers.js";
import { Sheet } from "./sheet.jsx";

export const TITLE_MAX = 40; // 后端 TITLE_MAX：超过了 400

// items：[{key, icon, text, onClick, disabled, danger}]；note：菜单下面的一句说明（为什么有的点不了）。
// anchor：打开它的按钮（桌面上按它的位置摆，Esc 收起时焦点回到它；点它自己由它切换开关）
export function ActionMenu({ label, anchor, mobile, items, note, onClose }) {
  if (mobile) {
    return createPortal(
      <Sheet title={label} onClose={onClose} className="ws-act-sheet">
        {items.map((it) => (
          <button key={it.key} className={"ws-sheet-act" + (it.danger ? " danger" : "")} disabled={it.disabled} onClick={it.onClick}>
            {it.icon}{it.text}
          </button>
        ))}
        {note && <div className="ws-menu-note">{note}</div>}
      </Sheet>,
      document.querySelector(".ws") || document.body, // 颜色等变量定义在 .ws 上，挂到 body 上弹层就没有底色（和来源弹层一样挂在 .ws 里）
    );
  }
  return <PopMenu label={label} anchor={anchor} items={items} note={note} onClose={onClose} />;
}

function PopMenu({ label, anchor, items, note, onClose }) {
  const box = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  useEffect(() => { box.current?.querySelector("button:not(:disabled)")?.focus(); }, []);
  useEffect(() => {
    const close = () => closing.current();
    const down = (e) => !box.current?.contains(e.target) && !anchor?.contains(e.target) && close();
    addEventListener("mousedown", down);
    addEventListener("scroll", close, true); // 列表滚动、窗口变化时菜单跟不上按钮，收起
    addEventListener("resize", close);
    return () => {
      removeEventListener("mousedown", down);
      removeEventListener("scroll", close, true);
      removeEventListener("resize", close);
    };
  }, [anchor]);
  useLayer(true, () => { anchor?.focus(); closing.current(); }); // Esc：收起，焦点回到打开它的按钮
  // 上下键在几项之间走（跳过点不了的），Tab 照常离开
  const arrows = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const els = [...box.current.querySelectorAll("button:not(:disabled)")];
    const i = els.indexOf(document.activeElement);
    els[(i + (e.key === "ArrowDown" ? 1 : -1) + els.length) % els.length]?.focus();
  };
  const rect = anchor?.getBoundingClientRect() || { top: 0, bottom: 0, right: 196 };
  const height = 36 * items.length + 8 + (note ? 48 : 0);
  const below = rect.bottom + 4 + height < innerHeight;
  const style = { left: Math.max(8, rect.right - 196), width: 196, ...(below ? { top: rect.bottom + 4 } : { bottom: innerHeight - rect.top + 4 }) };
  return (
    <div ref={box} className="ws-menu" role="menu" aria-label={label} style={style} onKeyDown={arrows}>
      {items.map((it) => (
        <button key={it.key} role="menuitem" className={it.danger ? "danger" : undefined} disabled={it.disabled} onClick={it.onClick}>
          {it.icon}{it.text}
        </button>
      ))}
      {note && <div className="ws-menu-note">{note}</div>}
    </div>
  );
}

// 改标题：回车或点别处保存，Esc 不改；onDone(新标题或 null, 怎么结束的："enter" | "escape" | "blur")。
// 空的、没改的由调用的地方当没改；回车、Esc 结束的，焦点由调用的地方放回去（点别处结束的焦点已经在别处了）
export function TitleInput({ value, className = "", onDone }) {
  const [v, setV] = useState(value);
  const ref = useRef(null);
  const done = useRef(false);
  const id = useId();
  useEffect(() => { ref.current?.select(); }, []);
  const finish = (title, how) => {
    if (done.current) return;
    done.current = true;
    onDone(title, how);
  };
  return (
    <span className={"ws-title-input " + className}>
      <input ref={ref} className="ws-rename" data-esc value={v} maxLength={TITLE_MAX} aria-label="对话标题" aria-describedby={id}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); finish(v.trim(), "enter"); }
          else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(null, "escape"); }
        }}
        onBlur={() => finish(v.trim(), "blur")} />
      <span id={id} className={"ws-count" + (v.length >= TITLE_MAX ? " full" : "")} aria-label={`${v.length} 个字，最多 ${TITLE_MAX} 个`}>
        {v.length}/{TITLE_MAX}
      </span>
    </span>
  );
}
