// 来源的显示：正文里的引用角标（桌面悬停预览、点了钉住；手机点了从底部弹出），和每条回答下面这条回答用到的来源。
// 知识库里的来源（2026-11-09）：图标是文件夹，网址那行写“知识库 · 第几段”，“查看原文”在右栏（手机上整屏）打开那个文件、翻到那一段（见 workspace-files.jsx）。
// 也放着能收起的内容共用的 Fold：收起来不卸掉，浏览器的查找搜得到
import React, { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { partUnit } from "./files.jsx";
import { IconChevron, IconClose, IconExternal, IconFolder } from "./icons.jsx";
import { useLayer } from "./layers.js";
import { NavContext } from "./nav.jsx";
import { Sheet } from "./sheet.jsx";
import { domain, isKnowledge, sourceName } from "./research.js";
import styles from "./sources.module.css";

// 来源网站的图标，后端代取（/api/favicon/域名）；取不到显示域名首字母。取不到的域名记下来，这个页面里不再请求
const noIcon = new Set();
const hostOf = (url) => {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
};
function Favicon({ url }) {
  const host = hostOf(url);
  const [failed, setFailed] = useState(null);
  if (!host || noIcon.has(host) || failed === host) return <span className={styles.fav + " " + styles.letter} aria-hidden="true">{(domain(url) || "?")[0].toUpperCase()}</span>;
  return <img className={styles.fav} src={`/api/favicon/${host}`} alt="" loading="lazy" onError={() => { noIcon.add(host); setFailed(host); }} />;
}

// 一条来源的图标：网页是网站图标，知识库的是文件夹
function SourceIcon({ s }) {
  if (isKnowledge(s)) return <span className={styles.fav + " " + styles.folder} aria-hidden="true"><IconFolder size={10} /></span>;
  return <Favicon url={s.url} />;
}

// 知识库来源的文件名（标题是“文件名 · 段的标题”，见后端 sources.collect_sources）和在哪：“知识库 · 第 3 段”（PDF 第几页、PPT 第几张）
const kbFile = (s) => (s.title || "").split(" · ")[0];
const kbExt = (s) => (/\.([A-Za-z0-9]{1,5})$/.exec(kbFile(s))?.[1] || "").toLowerCase();
const place = (s) => (isKnowledge(s) ? `知识库${s.part ? ` · 第 ${s.part} ${partUnit(kbExt(s))}` : ""}` : where(s.url));

// 在右栏打开知识库来源的那个文件、翻到那一段。done：先收起来源卡、弹层
function useOpenKnowledge() {
  const nav = useContext(NavContext);
  return (s, done) => {
    done?.();
    nav?.openKnowledge?.({ id: s.file_id, name: kbFile(s) }, s.part || null);
  };
}

// 一个角标的来源卡（桌面）：悬停或键盘移上去是预览，移开就收；点一下钉住：滚动时跟着角标走（评审第 21 条：查来源常在滚动着读的时候），
// 角标滚出看得见的地方、点外面、Esc、点卡上的关闭、Tab 到别处才收。键盘按回车钉住并把焦点移进卡里（落在“打开原文”），Esc 收起回到角标。
// 触屏（iPad 这类宽屏也一样）只认点：手指按下去浏览器也会当成“移上去”，不拿它当悬停。手机上点开的是底部弹层（SourceSheet），这里只记开没开
function usePop(mobile) {
  const [pop, setPop] = useState(null); // {rect, pinned}；手机上 {sheet: true}
  const mark = useRef(null);
  const box = useRef(null);
  const timer = useRef(null);
  const quiet = useRef(false); // 收起以后把焦点还给角标：这次聚焦不再打开预览
  const show = (pinned) => {
    clearTimeout(timer.current);
    const el = mark.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setPop((o) => ({ rect, pinned: pinned || !!o?.pinned }));
  };
  const hide = () => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setPop((o) => (o?.pinned ? o : null)), 220);
  };
  const close = () => {
    clearTimeout(timer.current);
    setPop(null);
  };
  const back = () => {
    close();
    quiet.current = true;
    mark.current?.focus();
    quiet.current = false;
  };
  useEffect(() => () => clearTimeout(timer.current), []);
  const open = !!pop;
  useEffect(() => {
    if (!open || mobile) return undefined;
    const down = (e) => !box.current?.contains(e.target) && !mark.current?.contains(e.target) && close(); // 点别处就收起
    const follow = () => { // 滚动、窗口变了：卡跟着角标挪；角标滚出了对话区看得见的地方就收起
      const el = mark.current;
      if (!el?.isConnected) return close();
      const r = el.getBoundingClientRect();
      const view = el.closest("[data-scroll]")?.getBoundingClientRect() || { top: 0, bottom: innerHeight };
      if (r.bottom < view.top || r.top > view.bottom) return close();
      setPop((o) => (o ? { ...o, rect: r } : o));
    };
    const moved = (e) => !box.current?.contains(e.target) && !mark.current?.contains(e.target) && close(); // Tab 到了卡和角标以外：钉住的也收
    addEventListener("pointerdown", down);
    addEventListener("scroll", follow, true);
    addEventListener("resize", follow);
    addEventListener("focusin", moved);
    return () => {
      removeEventListener("pointerdown", down);
      removeEventListener("scroll", follow, true);
      removeEventListener("resize", follow);
      removeEventListener("focusin", moved);
    };
  }, [open, mobile]);
  // Esc：焦点在卡里或角标上，收起并回到角标；在别处（比如输入框），只收起，光标不动。手机上是底部弹层，由它自己管
  useLayer(open && !mobile, () => {
    const at = document.activeElement;
    if (box.current?.contains(at) || at === mark.current) back();
    else close();
  });
  const mouse = (e) => e.pointerType === "mouse" && !mobile;
  return {
    open,
    close,
    trigger: {
      ref: mark,
      onPointerEnter: (e) => mouse(e) && show(false),
      onPointerLeave: (e) => mouse(e) && hide(),
      onFocus: (e) => !mobile && !quiet.current && e.target.matches(":focus-visible") && show(false),
      onBlur: () => !mobile && hide(),
      onClick: (e) => {
        if (mobile) return setPop({ sheet: true });
        if (e.detail === 0) { // 键盘按的回车或空格：钉住，把焦点移进卡里，“打开原文”才按得到
          show(true);
          requestAnimationFrame(() => box.current?.querySelector("[data-source-link]")?.focus());
        } else if (pop?.pinned) close();
        else show(true);
      },
    },
    box: { boxRef: box, rect: pop?.rect, onEnter: () => clearTimeout(timer.current), onLeave: hide, onClose: back },
  };
}

// 浮层放到页面根上（角标在段落里，卡不能插在段落中间），贴着角标：下面放不下就放上面，左右不出窗口
function floatStyle(rect, width, height) {
  const left = Math.max(12, Math.min(rect.left - 16, innerWidth - width - 12));
  const below = rect.bottom + 8 + height < innerHeight;
  return below ? { left, top: rect.bottom + 8, width } : { left, bottom: innerHeight - rect.top + 8, width };
}
const layer = (node) => createPortal(node, document.querySelector(".ws") || document.body);

// 来源卡上那段开头是从哪来的：读到的全文、查文档（Context7）查到的文档、只有搜索摘要。说法照术语表（已读全文 / 仅摘要）
const EXCERPT_BADGES = { body: "已读全文", docs: "文档摘录", summary: "仅摘要", knowledge: "知识库" };

// 网址写成“域名 + 路径的开头”：同一个网站的几篇靠它分得清
function where(url) {
  try {
    const u = new URL(url);
    const path = decodeURI(u.pathname).replace(/\/$/, "");
    return domain(url) + (path ? (path.length > 28 ? path.slice(0, 28) + "…" : path) : "");
  } catch {
    return domain(url);
  }
}

// 一条来源：编号和徽标、标题、网址和发布日期、开头一段、打开原文（知识库的是在右栏看）。桌面的来源卡和手机的底部弹层共用。
// onClose：卡上的关闭（桌面）；onDone：在右栏看之前先把卡、弹层收起来
function SourceBody({ s, n, onClose, onDone }) {
  const openKb = useOpenKnowledge();
  return (
    <>
      <div className={styles.top}>
        <span className={styles.lb}>{n}</span>
        <span className={styles.badge + " " + (s.excerpt_kind === "summary" ? styles.summary : styles.full)}>{EXCERPT_BADGES[s.excerpt_kind] || EXCERPT_BADGES.summary}</span>
        {onClose && <button className={"ws-icon-btn " + styles.close} aria-label="关闭" onClick={onClose}><IconClose size={14} /></button>}
      </div>
      <div className={styles.name}>{sourceName(s)}</div>
      <div className={styles.site}><SourceIcon s={s} />{place(s)}{s.published_at ? ` · 发布 ${s.published_at.slice(0, 10)}` : ""}</div>
      {s.excerpt && <div className={styles.excerpt}>{s.excerpt.length > 180 ? s.excerpt.slice(0, 180) + "…" : s.excerpt}</div>}
      <div className={styles.actions}>
        {isKnowledge(s) ? <button className="ws-btn ghost sm" data-source-link onClick={() => openKb(s, onDone)}><IconFolder size={13} />查看原文</button>
          : <a href={s.url} target="_blank" rel="noreferrer" className="ws-btn ghost sm" data-source-link><IconExternal size={13} />打开原文</a>}
      </div>
    </>
  );
}

function SourcePop({ boxRef, rect, s, n, onEnter, onLeave, onClose }) {
  return layer(
    <div ref={boxRef} className={styles.pop} style={floatStyle(rect, 340, 220)} role="dialog" aria-label={`来源 ${n}`}
      onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave}>
      <SourceBody s={s} n={n} onClose={onClose} onDone={onClose} />
    </div>,
  );
}

// 手机上点角标：底部弹出这条来源（M5）。下面能切到这一轮的全部来源，点一条看那一条
function SourceSheet({ n, nums, sources, onClose }) {
  const all = Object.entries(nums || {}).map(([sid, k]) => [k, sources?.[sid]]).filter(([, x]) => x).sort((a, b) => a[0] - b[0]);
  const [cur, setCur] = useState(n);
  const [list, setList] = useState(false);
  const body = useRef(null);
  const first = useRef(true);
  const s = all.find(([k]) => k === cur)?.[1];
  useEffect(() => { // 打开时光标在关闭钮上（Sheet 管）；切换以后落在新内容上：列表落在刚才看的那条，卡落在“打开原文”
    if (first.current) {
      first.current = false;
      return;
    }
    if (!body.current) return;
    (list ? body.current.querySelector('[aria-current="true"]') : body.current.querySelector("[data-source-link]"))?.focus();
  }, [list, cur]);
  return layer(
    <Sheet title={list ? `这一轮的全部 ${all.length} 个来源` : `来源 ${cur}`} onClose={onClose} className={styles.sheet} roomy>
      <div ref={body}>
        {list ? (
          <ol className={styles.list}>
            {all.map(([k, x]) => (
              <li key={x.id}>
                <button aria-current={k === cur ? "true" : undefined} onClick={() => { setCur(k); setList(false); }}>
                  <span className={styles.lb}>{k}</span>
                  <span className={styles.txt}><span className={styles.t}>{sourceName(x)}</span><span className={styles.meta}>{place(x)}{x.published_at ? ` · ${x.published_at.slice(0, 10)}` : ""}</span></span>
                </button>
              </li>
            ))}
          </ol>
        ) : s && (
          <>
            <SourceBody s={s} n={cur} onDone={onClose} />
            {all.length > 1 && <button className={"ws-btn ghost " + styles.all} onClick={() => setList(true)}>这一轮的全部 {all.length} 个来源</button>}
          </>
        )}
      </div>
    </Sheet>,
  );
}

// 正文里的引用角标：编号是这条回答里的。桌面悬停预览、点了钉住；手机点了从底部弹出。不在这条对话来源里的 id 标成问号。
// nums、sources：这一轮所有角标的编号和来源，手机弹层里列全部来源用
export function Mark({ n, s, nums, sources }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const pop = usePop(mobile);
  if (!s) return <span className={styles.mark + " " + styles.off} title="这个来源不在搜索、读页的结果里">?</span>;
  return (
    <>
      <button className={styles.mark + (pop.open ? " " + styles.on : "")} aria-label={`来源 ${n}`} aria-haspopup="dialog" {...pop.trigger}>{n}</button>
      {pop.open && (mobile ? <SourceSheet n={n} nums={nums || { [s.id]: n }} sources={sources || { [s.id]: s }} onClose={pop.close} /> : <SourcePop {...pop.box} s={s} n={n} />)}
    </>
  );
}

// ---- 收起来的内容 ----

// 收起时不从页面上拿掉，标 hidden="until-found"：看不见，浏览器的页内查找（Ctrl+F）还搜得到；搜到时浏览器先去掉 hidden 再发 beforematch，
// 这里跟着记成展开（onFound）。不认 until-found 的浏览器当普通 hidden，和以前收起一样。
// React 18 不认 hidden 的这个字符串值，每次渲染用 ref 自己设（浏览器搜到时去掉了、状态没跟上，下一次渲染也能对回来）。
// 外面这层不带样式：hidden 时浏览器给它 content-visibility: hidden，高度收成 0，可它自己的内边距、边框还会占地方，所以样式都放在里面那层
export function Fold({ open, onFound, children }) {
  const ref = useRef(null);
  const found = useRef(onFound);
  found.current = onFound;
  useLayoutEffect(() => {
    const el = ref.current;
    if (open) el.removeAttribute("hidden");
    else if (el.getAttribute("hidden") !== "until-found") el.setAttribute("hidden", "until-found");
  });
  useEffect(() => {
    const el = ref.current;
    const on = () => found.current?.();
    el.addEventListener("beforematch", on);
    return () => el.removeEventListener("beforematch", on);
  }, []);
  return <div ref={ref} className="ws-fold">{children}</div>;
}

// 能收起的一块展开没展开。用户点过的、查找时搜到的按用户的来（chosen）；没点过的：auto（比如这一轮正在跑）就展开，
// 跑完就收（过程、思考都是）；重新打开的对话里收着。
// 记在模块里，不放组件状态：正在发的那一轮存下来以后整块重新挂上，用户点开的不会自己收回去
const chosen = new Map();
export function useFoldOpen(key, auto) {
  const [, bump] = useState(0);
  const open = chosen.has(key) ? chosen.get(key) : auto;
  const set = (v) => { chosen.set(key, v); bump((n) => n + 1); };
  return [open, () => set(!open), () => set(true)];
}

// 这条回答用到的来源：默认收成一行（几个网站图标加条数），点开按在回答里第一次出现的先后列出，编号和正文里的角标一样；收着时列表也在页面里，查找搜得到。
// 每条：标题最多两行，下面一行是“域名 + 路径的开头”和发布日期，同一个网站的几篇分得清（评审第 21 条）
export function AnswerSources({ nums, sources }) {
  const [open, setOpen] = useState(false);
  const openKb = useOpenKnowledge();
  const used = Object.entries(nums).map(([sid, n]) => [n, sources?.[sid]]).filter(([, s]) => s);
  if (!used.length) return null;
  const sites = [...new Map(used.map(([, s]) => [isKnowledge(s) ? "kb:" : hostOf(s.url), s])).values()].slice(0, 6); // 同一个网站只放一个图标，知识库的一个文件夹
  return (
    <div className={styles.cited}>
      <button className={styles.citedHead} aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={styles.sites} aria-hidden="true">{sites.map((s) => <SourceIcon key={s.id} s={s} />)}</span>
        {used.length} 个来源
        <IconChevron size={13} className={"chev" + (open ? " up" : "")} />
      </button>
      <Fold open={open} onFound={() => setOpen(true)}>
        <ol aria-label="这条回答用到的来源">
          {used.map(([n, s]) => (
            <li key={s.id}>
              <span className={styles.lb}>{n}</span>
              <span className={styles.txt}>
                {isKnowledge(s) ? <button className={styles.kbLink} title={s.title} onClick={() => openKb(s)}>{sourceName(s)}</button>
                  : <a href={s.url} target="_blank" rel="noreferrer" title={s.title ? `${s.title}\n${s.url}` : s.url}>{sourceName(s)}</a>}
                <span className={styles.meta}><SourceIcon s={s} />{place(s)}{s.published_at ? ` · ${s.published_at.slice(0, 10)}` : ""}</span>
                {!isKnowledge(s) && <span className={styles.url}>{s.url}</span>}{/* 只在打印时出现：纸上点不了链接，印出网址 */}
              </span>
            </li>
          ))}
        </ol>
      </Fold>
    </div>
  );
}
