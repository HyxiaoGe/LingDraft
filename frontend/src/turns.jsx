// 轮次目录：一段对话三轮起才有。每轮一行（用户那句话的第一行），只列轮次，不列回答里的小标题（2026-11-07 Sean 定）。
// 正文右边放得下（见 timeline.jsx 的 RAIL_MIN）就放一栏；只放得下一列短横线（TICKS_MIN，要有鼠标）就放短横线，一轮一条；
// 再窄是顶栏右边一个“轮次”按钮，手机上点开是底部弹层，电脑上是下拉的浮层。每一轮最多显示两行。
// 一栏和短横线：鼠标停在某一轮上（或 Tab 到它），旁边出一张卡片，写这一轮问的话、回答的开头和发的时间（2026-11-07 Sean 要的，照豆包）。
// 跟着读到的位置高亮，点一行跳过去。跳的落点是 Turns 里每一轮的 section（data-turn 是第几轮，从 0 数）
import { useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { IconClose, IconList } from "./icons.jsx";
import { useLayer } from "./layers.js";
import { NavContext } from "./nav.jsx";
import { CITE, splitTurn, stamp } from "./research.js";
import sheet from "./sheet.module.css";
import styles from "./turns.module.css";

const MIN_TURNS = 3;

// 目录里的条目：[{i, text, ask, at, turn, running}]。text 是那句话的第一行（列表里用），ask 是整句、at 是发的时间、turn 是这一轮（卡片里用）。
// 正在发、服务器还没存下来的那句话也算一轮（和 Turns 里一样）；running：正在回答的最后一轮
export function turnEntries(st, live, running = false) {
  const turns = st?.turns || [];
  const said = turns.map((t) => ({ user: t.user, at: t.sent_at, turn: t }));
  if (live?.said && turns.length <= (live.turnsAtStart ?? 0)) said.push({ user: live.said, at: live.sentAt, turn: null });
  if (said.length < MIN_TURNS) return [];
  return said.map((s, i) => ({ i, text: (s.user || "").split("\n")[0].trim() || "（图片）", ask: (s.user || "").trim() || "（图片）", at: s.at, turn: s.turn,
    running: running && i === said.length - 1 }));
}

// 卡片里回答的开头：正文的纯文字，去掉引用和 Markdown 的记号（标题、引用、列表的 #、>、-，加粗，代码块的围栏，表格的竖线），只取前 200 个字（卡片里最多三行）
function replyPreview(items) {
  const md = splitTurn(items || []).answer.filter((i) => i.type === "text").map((i) => i.text).join("\n");
  return md.replace(CITE, "")
    .replace(/^\s*(`{3,}|~{3,}).*$/gm, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+(\[[ xX]\]\s+)?|\d+[.)]\s+)/gm, "")
    .replace(/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/gm, "")
    .replace(/\*\*|__|~~|`/g, "")
    .replace(/\s*\|\s*/g, " ")
    .replace(/\s+/g, " ").trim().slice(0, 200);
}

// 停在某一轮上时旁边的卡片。固定在窗口上（一栏自己在框里滚，放在里面会被剪掉），上下居中对着那一轮，顶多到顶栏下面、窗口底上面 8px；
// right 是卡片右边离窗口右边多远（调用的地方按一栏、短横线各自算）。不挡鼠标
function Peek({ id, entry, anchor, right }) {
  const ref = useRef(null);
  const [top, setTop] = useState(null);
  const reply = useMemo(() => (entry.turn && !entry.running ? replyPreview(entry.turn.items) : ""), [entry.turn, entry.running]);
  const s = stamp(entry.at);
  useLayoutEffect(() => {
    const h = ref.current.offsetHeight;
    setTop(Math.max(58 + 8, Math.min(anchor.top + anchor.height / 2 - h / 2, innerHeight - h - 8))); // 58：顶栏高
  }, [anchor, entry.ask, reply, entry.running]);
  return (
    <div ref={ref} id={id} role="tooltip" className={styles.peek} style={{ top: top ?? anchor.top, right, visibility: top == null ? "hidden" : undefined }}>
      <div className={styles.ask}>{entry.ask}</div>
      {entry.running ? <div className={styles.reply}>正在回答…</div> : reply && <div className={styles.reply}>{reply}</div>}
      {s && <time className={styles.when} dateTime={entry.at}>{s.text}</time>}
    </div>
  );
}

// 卡片跟着哪一轮：鼠标移进来、Tab 到哪一轮就是哪一轮；鼠标移出整个目录、焦点出了目录就收
function usePeek() {
  const [peek, setPeek] = useState(null); // {i, rect}
  const id = useId();
  const show = (i) => (e) => { if (e.type === "focus" || e.pointerType === "mouse") setPeek({ i, rect: e.currentTarget.getBoundingClientRect() }); };
  const hide = () => setPeek(null);
  const out = { onPointerLeave: hide, onBlur: (e) => { if (!e.currentTarget.contains(e.relatedTarget)) hide(); } };
  const item = (i) => ({ onPointerEnter: show(i), onFocus: show(i), "aria-describedby": peek?.i === i ? id : undefined });
  return { peek, id, hide, out, item };
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
      const sections = [...el.querySelectorAll("[data-turn]")];
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

// 目录本身：rail 和浮层、弹层共用。onJump(i) 跳到第 i 轮；点了以后 onDone（浮层、弹层收起）。item(i)：一栏里停上去出卡片（见 usePeek），有卡片就不用 title
function IndexList({ entries, at, onJump, onDone, item }) {
  return (
    <ol>
      {entries.map((e) => (
        <li key={e.i}>
          <button className={styles.turn + (e.i === at ? " " + styles.on : "")} aria-current={e.i === at ? "location" : undefined} title={item ? undefined : e.text}
            {...item?.(e.i)} onClick={() => { onJump(String(e.i)); onDone?.(); }}>
            <span className={styles.n}>{e.i + 1}</span><span className={styles.t}>{e.text}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

// 目录太长、自己在框里滚的时候：读到的那一轮跑出框了，把它滚进来（上下留 8px）。只滚这个框，不动页面。shape 变了（一栏和短横线互换）也对一次
function useInView(box, at, shape) {
  useEffect(() => {
    const el = box.current;
    const item = el?.querySelector('[aria-current="location"]');
    if (!item) return;
    const r = item.getBoundingClientRect();
    const b = el.getBoundingClientRect();
    const d = r.top < b.top + 8 ? r.top - b.top - 8 : r.bottom > b.bottom - 8 ? r.bottom - b.bottom + 8 : 0;
    if (d) el.scrollBy({ top: d, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [box, at, shape]);
}

// 宽屏：正文右边一栏，一直在。mini：只放得下一列短横线，一轮一条，点一条跳过去。两种都是停在某一轮上出卡片（Peek）
export function TurnIndex({ entries, scroller, onJump, mini = false }) {
  const at = useReading(scroller, entries.length);
  const box = useRef(null);
  useInView(box, at, mini);
  const { peek, id, hide, out, item } = usePeek();
  const entry = peek && entries.find((e) => e.i === peek.i);
  const cw = document.documentElement.clientWidth;
  // 卡片右边：一栏是离那一行左边 8px；短横线是离横线左头 8px（横线最长 20，靠右）
  const card = entry && <Peek id={id} entry={entry} anchor={peek.rect} right={mini ? cw - peek.rect.right + 20 + 8 : cw - peek.rect.left + 8} />;
  if (!mini) return (
    <nav ref={box} className={styles.rail} aria-label="轮次目录" onScroll={hide} {...out}>
      <div className={styles.title}>轮次</div>
      <IndexList entries={entries} at={at} onJump={onJump} item={item} />
      {card}
    </nav>
  );
  return (
    <nav className={styles.mini} aria-label="轮次目录" {...out}>
      {entries.map((e) => (
        <button key={e.i} className={styles.tick + (e.i === at ? " " + styles.on : "")} aria-label={`第 ${e.i + 1} 轮`} aria-current={e.i === at ? "location" : undefined}
          {...item(e.i)} onClick={() => onJump(String(e.i))} />
      ))}
      {card}
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
  useInView(box, at);
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
    <div className="relative flex">
      <button ref={btn} className="ws-icon-btn w-auto gap-1 px-2 text-[13px] whitespace-nowrap phone:w-11 phone:px-0" aria-label="轮次目录" title="轮次目录" aria-haspopup="dialog" aria-expanded={open}
        onClick={() => (open ? close() : setOpen(true))}>
        <IconList size={18} />{!mobile && <span>轮次</span>}
      </button>
      {open && (mobile ? (
        <>
          <div className={sheet.scrim} onClick={() => close()} aria-hidden="true" />
          <div ref={box} className={sheet.sheet + " " + styles.sheet} data-sheet role="dialog" aria-modal="true" aria-label="轮次目录" onKeyDown={trap}>
            <div className={sheet.head}>
              <b>轮次</b>
              <button className="ws-icon-btn" data-sheet-close aria-label="关闭" onClick={() => close()}><IconClose size={18} /></button>
            </div>
            <div className={sheet.body}>{list}</div>
          </div>
        </>
      ) : (
        <div ref={box} className={styles.pop} role="dialog" aria-label="轮次目录">
          <div className="flex items-center justify-between pb-1 pl-2 text-[13px]">
            <b className="font-semibold">轮次</b>
            <button className="ws-icon-btn" aria-label="关闭" onClick={() => close()}><IconClose size={16} /></button>
          </div>
          {list}
        </div>
      ))}
    </div>
  );
}
