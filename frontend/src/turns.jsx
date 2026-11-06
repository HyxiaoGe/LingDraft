// 轮次目录：一段对话三轮起（或者有一条回答分了三个以上的小标题）才有。每轮一行（用户那句话），正在读的那一轮下面列出回答里的小标题（大纲）。
// 正文右边放得下（见 timeline.jsx 的 RAIL_MIN）就放一栏；放不下顶栏右边一个“轮次”按钮，手机上点开是底部弹层，电脑上是下拉的浮层。
// 跟着读到的位置高亮，点一行跳过去。跳的落点是 Turns 里每一轮的 section（data-turn 是第几轮，从 0 数）
import { useContext, useEffect, useRef, useState } from "react";
import { IconClose, IconList } from "./icons.jsx";
import { NavContext } from "./nav.jsx";
import { CITE, FENCE, closesFence, mergeItems, splitTurn } from "./research.js";

const MIN_TURNS = 3;
const MIN_HEADINGS = 3;

// 回答里的小标题：正文（不含过程里中途说的话）顶层的 # 行，跳过代码块。只列最浅的两层；level 是 # 的个数，n 是它在这一轮所有小标题里排第几（跳过去时按这个找）
function headings(answer) {
  const all = [];
  for (const it of answer) {
    if (it.type !== "text") continue;
    let fence = null;
    for (const line of it.text.split("\n")) {
      if (fence) { if (closesFence(line, fence)) fence = null; continue; }
      const f = line.match(FENCE);
      if (f) { fence = f[2]; continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) all.push({ level: h[1].length, text: plain(h[2]), n: all.length });
    }
  }
  const top = Math.min(...all.map((h) => h.level));
  return all.filter((h) => h.level <= top + 1 && h.text).map((h) => ({ ...h, sub: h.level > top }));
}

// 小标题里的 Markdown 符号、引用去掉，只留字
const plain = (s) => s.replace(CITE, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*_`]/g, "").trim();

// 目录里的条目：[{i, text, heads}]。正在发、服务器还没存下来的那句话也算一轮（和 Turns 里一样）
export function turnEntries(st, live, running) {
  const turns = st?.turns || [];
  const all = turns.map((t) => ({ user: t.user, items: t.items }));
  if (live?.said && turns.length <= (live.turnsAtStart ?? 0)) all.push({ user: live.said, items: [] });
  const out = all.map((t, i) => {
    const items = mergeItems(t.items || [], i === all.length - 1 && running ? live : null);
    return { i, text: (t.user || "").split("\n")[0].trim() || "（图片）", heads: headings(splitTurn(items).answer) };
  });
  return out.length >= MIN_TURNS || out.some((e) => e.heads.length >= MIN_HEADINGS) ? out : [];
}

// 现在读到哪：一条横线（滚动区顶上往下 80px，矮的时候四分之一），最后一个顶边过了这条线的那一轮、那个小标题。
// 线不能太低：跳到一个小标题，它顶着上沿，下一节的标题离得近时也不该先亮。
// 滚到底了算最后一轮、看得见的最后一个小标题：最后一节短的时候，它的标题永远过不了那条线
function useReading(scroller, entries) {
  const [at, setAt] = useState({ turn: 0, head: -1 });
  const sig = entries.map((e) => e.heads.length).join(",");
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const box = el.getBoundingClientRect();
      const line = box.top + Math.min(80, el.clientHeight / 4);
      const sections = [...el.querySelectorAll(".ws-turn")];
      let turn = 0;
      sections.forEach((s, i) => { if (s.getBoundingClientRect().top <= line) turn = i; });
      const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 4;
      if (bottom) turn = sections.length - 1;
      let head = -1;
      headingEls(sections[turn]).forEach((h, n) => { if (h.getBoundingClientRect().top <= (bottom ? box.bottom : line)) head = n; });
      setAt((a) => (a.turn === turn && a.head === head ? a : { turn, head }));
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
  }, [scroller, sig]);
  return at;
}

// 一轮里正文的小标题（和 headings 数的一样：每条正文 Markdown 顶层的标题）
const headingEls = (section) => (section ? [...section.querySelectorAll(".ws-reply > .md > :is(h3, h4, h5, h6)")] : []);

// 跳到第 i 轮的第 n 个小标题（n 没给就跳到这一轮开头）
function jumpHeading(scroller, i, n) {
  const el = headingEls(scroller.current?.querySelector(`[data-turn="${i}"]`))[n];
  if (!el) return false;
  el.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  return true;
}

// 目录本身：rail 和浮层、弹层共用。onJump(i) 跳到第 i 轮；点了以后 onDone（浮层、弹层收起）
function IndexList({ entries, at, scroller, onJump, onDone }) {
  const go = (i, n) => {
    if (n == null || !jumpHeading(scroller, i, n)) onJump(String(i));
    onDone?.();
  };
  return (
    <ol className="ws-index-list">
      {entries.map((e) => {
        const current = e.i === at.turn;
        const heads = current || entries.length < MIN_TURNS ? e.heads : [];
        return (
          <li key={e.i}>
            <button className={"ws-index-turn" + (current ? " on" : "")} aria-current={current ? "location" : undefined} title={e.text} onClick={() => go(e.i)}>
              <span className="n">{e.i + 1}</span><span className="t">{e.text}</span>
            </button>
            {heads.length > 0 && (
              <ol className="ws-index-heads">
                {heads.map((h) => (
                  <li key={h.n}>
                    <button className={"ws-index-head" + (h.sub ? " sub" : "") + (current && h.n === at.head ? " on" : "")} title={h.text} onClick={() => go(e.i, h.n)}>{h.text}</button>
                  </li>
                ))}
              </ol>
            )}
          </li>
        );
      })}
    </ol>
  );
}

// 宽屏：正文右边一栏，一直在
export function TurnIndex({ entries, scroller, onJump }) {
  const at = useReading(scroller, entries);
  return (
    <nav className="ws-rail" aria-label="轮次目录">
      <div className="ws-rail-title">轮次</div>
      <IndexList entries={entries} at={at} scroller={scroller} onJump={onJump} />
    </nav>
  );
}

// 窄的时候：顶栏右边的“轮次”按钮。手机上点开是底部弹层，电脑上是按钮下面的浮层；Esc、点外面、点关闭都收起，收起后光标回到按钮
export function TurnIndexButton({ entries, scroller, onJump }) {
  const nav = useContext(NavContext);
  const mobile = !!nav?.mobile;
  const [open, setOpen] = useState(false);
  const at = useReading(scroller, entries);
  const btn = useRef(null);
  const box = useRef(null);
  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) btn.current?.focus();
  };
  useEffect(() => {
    if (!open) return;
    (box.current?.querySelector('[aria-current="location"]') || box.current?.querySelector("button"))?.focus();
    const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    const down = (e) => { if (!box.current?.contains(e.target) && !btn.current?.contains(e.target)) close(false); };
    addEventListener("keydown", key, true);
    addEventListener("mousedown", down);
    return () => {
      removeEventListener("keydown", key, true);
      removeEventListener("mousedown", down);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  useEffect(() => { setOpen(false); }, [mobile]);
  // 跳过去以后光标落在那一轮上（见 Conversation 的 jumpTo），不回按钮
  const done = () => setOpen(false);
  const list = <IndexList entries={entries} at={at} scroller={scroller} onJump={onJump} onDone={done} />;
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
