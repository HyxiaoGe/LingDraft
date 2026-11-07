// 搜对话（2026-11-07 Sean 定，照豆包）：左栏品牌那行右边的放大镜（左栏收起时在顶栏左边，⌘K / Ctrl+K 也行）打开一个大窗口，
// 上面是搜索框，下面是对话列表：没打字时是最近的对话（按最后动过的时间，不分置顶），打字边打边筛，只搜标题（第 13 条、M1）。
// ↑↓ 挑一条、回车打开，鼠标移到哪条挑哪条；点窗口外、Esc、右上角的关闭都能关，后面开着的对话不动。手机上从底部升起
import { useEffect, useId, useRef, useState } from "react";
import { IconClose, IconSearch } from "./icons.jsx";
import { opener, useLayer } from "./layers.js";
import { touch } from "./nav.jsx";
import { ACTIVE, stamp } from "./research.js";
import dialog from "./dialog.module.css";
import styles from "./search.module.css";

// 搜到的那几个字标出来（不分大小写）
function Hit({ text, q }) {
  const i = q ? text.toLowerCase().indexOf(q) : -1;
  if (i < 0) return text;
  return <>{text.slice(0, i)}<mark>{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>;
}

// threads：对话列表（null 是还在取）；href(id)：那段对话的地址（按住 ⌘、Ctrl 点在新标签页打开）；onOpen(id)：关窗口、打开那段对话
export function SearchDialog({ threads, now, href, onOpen, onClose }) {
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0); // 挑中的是第几条
  const box = useRef(null);
  const input = useRef(null);
  const list = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  const id = useId();
  useEffect(() => {
    const back = opener(); // 关上以后焦点回去；打开它的按钮在收起的抽屉里（看不见）就回到抽屉钮
    if (touch()) box.current?.focus({ preventScroll: true }); // 触屏：不替用户把光标放进框里，免得自己弹出键盘（批次 G 第 42 条）
    else input.current?.focus();
    return () => {
      const ok = back?.isConnected && back.checkVisibility?.({ visibilityProperty: true }) !== false;
      (ok ? back : document.querySelector('[data-nav="drawer"]'))?.focus({ preventScroll: true });
    };
  }, []);
  useLayer(true, () => closing.current());

  const q = query.trim().toLowerCase();
  const time = (t) => Date.parse(t.updated_at || "") || now; // 刚新建、还没有时间的按现在
  const rows = threads && threads.filter((t) => !q || (t.title || "").toLowerCase().includes(q)).sort((a, b) => time(b) - time(a));
  const pick = rows?.length ? Math.min(at, rows.length - 1) : -1;
  useEffect(() => setAt(0), [q]);
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [pick, q]);

  const onKey = (e) => {
    if (e.nativeEvent.isComposing || !rows?.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setAt(Math.max(0, Math.min(rows.length - 1, pick + (e.key === "ArrowDown" ? 1 : -1))));
    } else if (e.key === "Enter" && pick >= 0) {
      e.preventDefault();
      onOpen(rows[pick].thread_id);
    }
  };
  // Tab 只在窗口里转：搜索框、清空、关闭（列表里的对话用 ↑↓ 挑，不进 Tab 顺序）
  const trap = (e) => {
    if (e.key !== "Tab") return;
    const els = [...box.current.querySelectorAll("input, button")].filter((x) => !x.disabled && x.offsetParent !== null);
    e.preventDefault();
    const i = els.indexOf(document.activeElement);
    els[i < 0 ? 0 : (i + (e.shiftKey ? els.length - 1 : 1)) % els.length]?.focus();
  };
  const optId = (t) => `${id}-${t.thread_id}`;

  return (
    <div className={`${dialog.overlay} ${dialog.large}`} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} className={`${dialog.dialog} ${dialog.large} flex flex-col`} role="dialog" aria-modal="true" aria-label="搜索对话" tabIndex={-1} onKeyDown={trap}>
        <div className={styles.head}>
          <div className={styles.box} onMouseDown={(e) => { if (!e.target.closest("input, button")) { e.preventDefault(); input.current.focus(); } }}>
            <IconSearch size={16} />
            <input ref={input} data-find type="text" value={query} placeholder="搜索对话标题" aria-label="搜索对话标题" role="combobox" aria-expanded="true"
              aria-controls={`${id}-list`} aria-autocomplete="list" aria-activedescendant={pick >= 0 ? optId(rows[pick]) : undefined}
              enterKeyHint="go" autoComplete="off" spellCheck={false} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey} />
            {query && (
              <button className={styles.clear} aria-label="清空" onClick={() => { setQuery(""); input.current.focus(); }}><IconClose size={14} /></button>
            )}
          </div>
          <button className={`${dialog.close} ${dialog.inline}`} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>
        </div>
        <div className={styles.body}>
          {rows === null ? (
            <div className={styles.skel} role="status" aria-label="正在取对话列表">
              {[62, 48, 70, 54, 66, 44].map((w) => <span key={w} className="ws-skel ws-skel-row" style={{ width: `${w}%` }} />)}
            </div>
          ) : rows.length === 0 ? (
            <div className={styles.empty}>{q ? `没有标题里有“${query.trim()}”的对话。只搜标题，不搜对话内容。` : "还没有对话"}</div>
          ) : (
            <>
              <div className={styles.label} id={`${id}-label`}>{q ? <>标题里有“{query.trim()}”<span>{rows.length}</span></> : "最近对话"}</div>
              <div ref={list} className="flex flex-col" id={`${id}-list`} role="listbox" aria-labelledby={`${id}-label`}>
                {rows.map((t, i) => {
                  const s = stamp(t.updated_at, now);
                  const status = ACTIVE.has(t.status) ? "active" : t.status === "failed" ? "failed" : "";
                  return (
                    <a key={t.thread_id} id={optId(t)} className={styles.row} role="option" aria-selected={i === pick} tabIndex={-1} href={href(t.thread_id)}
                      onMouseMove={() => i !== pick && setAt(i)}
                      onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onOpen(t.thread_id); }}>
                      <span className={styles.title} title={t.title}><Hit text={t.title || "新对话"} q={q} /></span>
                      {status && <span className={styles.dot + " " + styles[status]} title={status === "active" ? "进行中" : "没有完成"} />}
                      {status && <span className="ws-sr">{status === "active" ? "，进行中" : "，没有完成"}</span>}
                      <span className={styles.time} title={s?.full}>{s?.text}</span>
                    </a>
                  );
                })}
              </div>
            </>
          )}
        </div>
        <div className="ws-sr" role="status">{q && rows ? (rows.length ? `找到 ${rows.length} 段对话` : "没有找到") : ""}</div>
      </div>
    </div>
  );
}
