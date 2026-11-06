// 来源的显示：正文里的引用角标（悬停看来源卡），和每条回答下面这条回答用到的来源。
// 也放着能收起的内容共用的 Fold：收起来不卸掉，浏览器的查找搜得到
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconChevron, IconExternal } from "./icons.jsx";
import { domain, sourceName } from "./research.js";

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
  if (!host || noIcon.has(host) || failed === host) return <span className="ws-fav letter" aria-hidden="true">{(domain(url) || "?")[0].toUpperCase()}</span>;
  return <img className="ws-fav" src={`/api/favicon/${host}`} alt="" loading="lazy" onError={() => { noIcon.add(host); setFailed(host); }} />;
}

// 一个角标的来源卡：悬停或聚焦打开，移开收起；键盘按回车把焦点移进卡里，Esc 收起回到角标
function usePop() {
  const [rect, setRect] = useState(null);
  const mark = useRef(null);
  const box = useRef(null);
  const timer = useRef(null);
  const show = () => {
    clearTimeout(timer.current);
    if (mark.current) setRect(mark.current.getBoundingClientRect());
  };
  const hide = () => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setRect(null), 220);
  };
  const close = () => {
    clearTimeout(timer.current);
    setRect(null);
  };
  const back = () => {
    close();
    mark.current?.focus();
  };
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => {
    if (!rect) return undefined;
    const down = (e) => !box.current?.contains(e.target) && !mark.current?.contains(e.target) && close(); // 点别处就收起
    const moved = (e) => !box.current?.contains(e.target) && close(); // 滚动、窗口变化时卡跟不上角标，收起
    addEventListener("mousedown", down);
    addEventListener("scroll", moved, true);
    addEventListener("resize", close);
    return () => { removeEventListener("mousedown", down); removeEventListener("scroll", moved, true); removeEventListener("resize", close); };
  }, [rect]);
  return {
    open: !!rect,
    trigger: {
      ref: mark,
      onMouseEnter: show,
      onMouseLeave: hide,
      onFocus: show,
      onBlur: hide,
      onKeyDown: (e) => e.key === "Escape" && rect && back(),
      onClick: (e) => {
        if (e.detail === 0) { // 键盘按的回车或空格：把焦点移进卡里，“打开原文”才按得到
          show();
          requestAnimationFrame(() => box.current?.querySelector("a, button")?.focus());
        } else if (rect) close();
        else show();
      },
    },
    box: { boxRef: box, rect, onEnter: () => clearTimeout(timer.current), onLeave: hide, onClose: back },
  };
}

// 浮层放到页面根上（角标在段落里，卡不能插在段落中间），贴着角标：下面放不下就放上面，左右不出窗口
function floatStyle(rect, width, height) {
  const left = Math.max(12, Math.min(rect.left - 16, innerWidth - width - 12));
  const below = rect.bottom + 8 + height < innerHeight;
  return below ? { left, top: rect.bottom + 8, width } : { left, bottom: innerHeight - rect.top + 8, width };
}
const layer = (node) => createPortal(node, document.querySelector(".ws") || document.body);

// 来源卡上那段开头是从哪来的：读到的正文、查文档（Context7）查到的文档、搜索摘要
const EXCERPT_BADGES = { body: "读到正文", docs: "文档摘录", summary: "只读到搜索摘要" };

function SourcePop({ boxRef, rect, s, n, onEnter, onLeave, onClose }) {
  return layer(
    <div ref={boxRef} className="ws-pop" style={floatStyle(rect, 340, 220)} role="dialog" aria-label={`来源 ${n}`}
      onMouseEnter={onEnter} onMouseLeave={onLeave} onFocus={onEnter} onBlur={onLeave} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="ws-pop-top">
        <span className="lb">{n}</span>
        <span className={"ws-badge " + (s.excerpt_kind === "summary" ? "summary" : "body")}>{EXCERPT_BADGES[s.excerpt_kind] || EXCERPT_BADGES.summary}</span>
      </div>
      <div className="ws-pop-title">{sourceName(s)}</div>
      <div className="ws-pop-meta"><Favicon url={s.url} />{domain(s.url)}{s.published_at ? ` · 发布 ${s.published_at.slice(0, 10)}` : ""}</div>
      {s.excerpt && <div className="ws-pop-excerpt">{s.excerpt.length > 180 ? s.excerpt.slice(0, 180) + "…" : s.excerpt}</div>}
      <div className="ws-pop-actions">
        <a href={s.url} target="_blank" rel="noreferrer" className="ws-btn ghost sm"><IconExternal size={13} />打开原文</a>
      </div>
    </div>,
  );
}

// 正文里的引用角标：编号是这条回答里的，悬停看来源。不在这条对话来源里的 id 标成问号
export function Mark({ n, s }) {
  const pop = usePop();
  if (!s) return <span className="ws-mark off" title="这个来源不在搜索、读页的结果里">?</span>;
  return (
    <>
      <button className={"ws-mark" + (pop.open ? " on" : "")} aria-label={`来源 ${n}`} {...pop.trigger}>{n}</button>
      {pop.open && <SourcePop {...pop.box} s={s} n={n} />}
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
// 这次打开页面以后跑过的（ran）跑完也不自动收；重新打开的对话里收着。
// 记在模块里，不放组件状态：正在发的那一轮存下来以后换了 id、整块重新挂上，展开的不会自己收回去
const chosen = new Map();
const ran = new Set();
export function useFoldOpen(key, auto) {
  const [, bump] = useState(0);
  if (auto) ran.add(key);
  const open = chosen.has(key) ? chosen.get(key) : auto || ran.has(key);
  const set = (v) => { chosen.set(key, v); bump((n) => n + 1); };
  return [open, () => set(!open), () => set(true)];
}

// 这条回答用到的来源：默认收成一行（几个网站图标加条数），点开按在回答里第一次出现的先后列出，编号和正文里的角标一样；收着时列表也在页面里，查找搜得到
export function AnswerSources({ nums, sources }) {
  const [open, setOpen] = useState(false);
  const used = Object.entries(nums).map(([sid, n]) => [n, sources?.[sid]]).filter(([, s]) => s);
  if (!used.length) return null;
  const sites = [...new Map(used.map(([, s]) => [hostOf(s.url), s.url])).values()].slice(0, 6); // 同一个网站只放一个图标
  return (
    <div className="ws-cited">
      <button className="ws-cited-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="sites" aria-hidden="true">{sites.map((url) => <Favicon key={url} url={url} />)}</span>
        {used.length} 个来源
        <IconChevron size={13} className={"chev" + (open ? " up" : "")} />
      </button>
      <Fold open={open} onFound={() => setOpen(true)}>
        <ol aria-label="这条回答用到的来源">
          {used.map(([n, s]) => (
            <li key={s.id}>
              <span className="lb">{n}</span>
              <a href={s.url} target="_blank" rel="noreferrer" title={s.title ? `${s.title}\n${s.url}` : s.url}>{sourceName(s)}</a>
              <span className="meta"><Favicon url={s.url} />{domain(s.url)}{s.published_at ? ` · ${s.published_at.slice(0, 10)}` : ""}</span>
            </li>
          ))}
        </ol>
      </Fold>
    </div>
  );
}
