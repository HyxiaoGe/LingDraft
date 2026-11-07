// 手机上的底部弹层（来源用）：盖一层暗底，点暗底、按 Esc（外接键盘）、点关闭都收起，收起后焦点回到打开它的按钮；
// 打开时光标落在关闭钮上（里面的内容要换焦点的自己再挪），Tab 在里面转。样式和选模型的弹层同一套（sheet.module.css）
import { useEffect, useRef } from "react";
import { IconClose } from "./icons.jsx";
import { opener, useLayer } from "./layers.js";
import styles from "./sheet.module.css";

const FOCUSABLE = 'a[href], button:not(:disabled), [tabindex]:not([tabindex="-1"])';

// roomy：里面是一张卡（来源），四周都留 16
export function Sheet({ title, onClose, className = "", roomy, children }) {
  const ref = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  useEffect(() => {
    const back = opener(); // Safari 点按钮不给按钮焦点：用刚按下的那个按钮（待确认第 24 条）
    ref.current?.querySelector("[data-sheet-close]")?.focus();
    return () => { if (back?.isConnected) back.focus({ preventScroll: true }); };
  }, []);
  useLayer(true, () => closing.current()); // Esc（外接键盘）
  const keys = (e) => {
    if (e.key !== "Tab") return;
    e.stopPropagation(); // 开在抽屉上面时（抽屉里一行的菜单）：Tab 只在弹层里转，不让抽屉再转一次
    const els = [...ref.current.querySelectorAll(FOCUSABLE)];
    const i = els.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) {
      e.preventDefault();
      els.at(-1)?.focus();
    } else if (!e.shiftKey && i === els.length - 1) {
      e.preventDefault();
      els[0]?.focus();
    }
  };
  return (
    <>
      <div className={styles.scrim} onClick={() => closing.current()} aria-hidden="true" />
      <div ref={ref} className={[styles.sheet, roomy && styles.roomy, className].filter(Boolean).join(" ")} data-sheet role="dialog" aria-modal="true" aria-label={title} onKeyDown={keys}>
        <div className={styles.head}>
          <b>{title}</b>
          <button className="ws-icon-btn" data-sheet-close aria-label="关闭" onClick={() => closing.current()}><IconClose size={18} /></button>
        </div>
        <div className={styles.body}>{children}</div>
      </div>
    </>
  );
}
