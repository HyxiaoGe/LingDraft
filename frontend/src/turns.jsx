// 轮次目录：一段对话三轮起才有。每轮一行（用户那句话的第一行），只列轮次，不列回答里的小标题（2026-11-07 Sean 定）。
// 正文右边放得下（见 timeline.jsx 的 RAIL_MIN）就放一栏；放不下顶栏右边一个“轮次”按钮，手机上点开是底部弹层，电脑上是下拉的浮层。
// 跟着读到的位置高亮，点一行跳过去。跳的落点是 Turns 里每一轮的 section（data-turn 是第几轮，从 0 数）
import { useContext, useEffect, useRef, useState } from "react";
import { IconClose, IconList } from "./icons.jsx";
import { useLayer } from "./layers.js";
import { NavContext } from "./nav.jsx";

const MIN_TURNS = 3;

// 目录里的条目：[{i, text}]。正在发、服务器还没存下来的那句话也算一轮（和 Turns 里一样）
export function turnEntries(st, live) {
  const said = (st?.turns || []).map((t) => t.user);
  if (live?.said && said.length <= (live.turnsAtStart ?? 0)) said.push(live.said);
  if (said.length < MIN_TURNS) return [];
  return said.map((u, i) => ({ i, text: (u || "").split("\n")[0].trim() || "（图片）" }));
}

// 现在读到哪一轮：一条横线（滚动区顶上往下 80px，矮的时候四分之一），最后一个顶边过了这条线的那一轮。
// 滚到底了算最后一轮：最后一轮短的时候，它的顶边永远过不了那条线
function useReading(scroller, count) {
  const [at, setAt] = useState(0);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const line = el.getBoundingClientRect().top + Math.min(80, el.clientHeight / 4);
      const sections = [...el.querySelectorAll(".ws-turn")];
      let turn = 0;
      sections.forEach((s, i) => { if (s.getBoundingClientRect().top <= line) turn = i; });
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 4) turn = sections.length - 1;
      setAt(turn);
    };
    const later = () => { if (!frame) frame = requestAnimationFrame(measure); };
    measure();
    el.addEventListener("scroll", later, { passive: true });
    const ro = new ResizeObserver(later);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => {
      el.removeEventListener("scroll", later);
      ro.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [scroller, count]);
  return at;
}

// 目录本身：rail 和浮层、弹层共用。onJump(i) 跳到第 i 轮；点了以后 onDone（浮层、弹层收起）
function IndexList({ entries, at, onJump, onDone }) {
  return (
    <ol className="ws-index-list">
      {entries.map((e) => (
        <li key={e.i}>
          <button className={"ws-index-turn" + (e.i === at ? " on" : "")} aria-current={e.i === at ? "location" : undefined} title={e.text}
            onClick={() => { onJump(String(e.i)); onDone?.(); }}>
            <span className="n">{e.i + 1}</span><span className="t">{e.text}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

// 宽屏：正文右边一栏，一直在
export function TurnIndex({ entries, scroller, onJump }) {
  const at = useReading(scroller, entries.length);
  return (
    <nav className="ws-rail" aria-label="轮次目录">
      <div className="ws-rail-title">轮次</div>
      <IndexList entries={entries} at={at} onJump={onJump} />
    </nav>
  );
}

// 窄的时候：顶栏右边的“轮次”按钮。手机上点开是底部弹层，电脑上是按钮下面的浮层；Esc、点外面、点关闭都收起，收起后光标回到按钮
export function TurnIndexButton({ entries, scroller, onJump }) {
  const nav = useContext(NavContext);
  const mobile = !!nav?.mobile;
  const [open, setOpen] = useState(false);
  const at = useReading(scroller, entries.length);
  const btn = useRef(null);
  const box = useRef(null);
  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) btn.current?.focus();
  };
  useEffect(() => {
    if (!open) return;
    (box.current?.querySelector('[aria-current="location"]') || box.current?.querySelector("button"))?.focus();
    const down = (e) => { if (!box.current?.contains(e.target) && !btn.current?.contains(e.target)) close(false); };
    addEventListener("mousedown", down);
    return () => removeEventListener("mousedown", down);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  useLayer(open, () => close()); // Esc：收起，焦点回到按钮
  useEffect(() => { setOpen(false); }, [mobile]);
  // 跳过去以后光标落在那一轮上（见 Conversation 的 jumpTo），不回按钮
  const done = () => setOpen(false);
  const list = <IndexList entries={entries} at={at} onJump={onJump} onDone={done} />;
  const trap = (e) => {
    if (e.key !== "Tab") return;
    const els = [...box.current.querySelectorAll("button")];
    const i = els.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); els.at(-1)?.focus(); }
    else if (!e.shiftKey && i === els.length - 1) { e.preventDefault(); els[0]?.focus(); }
  };
  return (
    <div className="ws-index-btn-box">
      <button ref={btn} className="ws-icon-btn ws-index-btn" aria-label="轮次目录" title="轮次目录" aria-haspopup="dialog" aria-expanded={open}
        onClick={() => (open ? close() : setOpen(true))}>
        <IconList size={18} />{!mobile && <span>轮次</span>}
      </button>
      {open && (mobile ? (
        <>
          <div className="ws-sheet-scrim" onClick={() => close()} aria-hidden="true" />
          <div ref={box} className="ws-sheet ws-index-sheet" role="dialog" aria-modal="true" aria-label="轮次目录" onKeyDown={trap}>
            <div className="ws-sheet-head">
              <b>轮次</b>
              <button className="ws-icon-btn ws-sheet-close" aria-label="关闭" onClick={() => close()}><IconClose size={18} /></button>
            </div>
            <div className="ws-sheet-body">{list}</div>
          </div>
        </>
      ) : (
        <div ref={box} className="ws-index-pop" role="dialog" aria-label="轮次目录">
          <div className="ws-index-pop-head">
            <b>轮次</b>
            <button className="ws-icon-btn" aria-label="关闭" onClick={() => close()}><IconClose size={16} /></button>
          </div>
          {list}
        </div>
      ))}
    </div>
  );
}
