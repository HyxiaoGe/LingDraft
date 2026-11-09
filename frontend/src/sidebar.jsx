// 左栏：对话列表。最上面品牌、搜对话、收起（手机上是关闭抽屉），下面“新对话”和“资料库”，再下面按时间分组的对话，最下面额度和账号。
// 手机上是左边滑出的抽屉（遮罩也在这里）。简单的排版写在元素上（Tailwind），悬停、选中、伪元素、动画和手机抽屉在 sidebar.module.css。
// 照 Fusion 的样子（2026-11-07 Sean 嫌单调）：每行前面一个对话图标（正在回答换成转圈、没跑完换成感叹号），选中的那行是白底浮起来的一块，“新对话”用写字的图标
import { CircleAlert, LibraryBig, MessageSquare, MessagesSquare, SquarePen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { IconClose, IconDownload, IconEdit, IconMore, IconPanel, IconPinTop, IconSearch, IconTrash, Logo, Spinner } from "./icons.jsx";
import { useLayer } from "./layers.js";
import { CreditsFoot, UserFoot } from "./login.jsx";
import { ActionMenu, TitleInput } from "./menu.jsx";
import { SEARCH_ARIA_KEY, SEARCH_KEY, SIDE_ARIA_KEY, SIDE_KEY } from "./nav.jsx";
import { ACTIVE, elapsed, sayWhen, stamp, when } from "./research.js";
import styles from "./sidebar.module.css";

const cx = (...names) => names.filter(Boolean).join(" ");

// 分组（第 13 条、M1）：置顶的在最前（后置顶的在上），其余按最后动过的时间分今天、昨天、近 7 天、更早。
// 进行中、没跑完在每一行里用点和小字标，不再按状态分组：以前状态一变，那一行就在分组间跳
const GROUPS = [["pinned", "置顶"], ["today", "今天"], ["yesterday", "昨天"], ["week", "近 7 天"], ["older", "更早"]];
const timeOf = (t, now) => Date.parse(t.updated_at || "") || now; // 刚新建、列表里还没有的那一行按现在
function groupOf(t, now) {
  if (t.pinned_at) return "pinned";
  const d = new Date(now);
  const ago = (n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() - n).getTime(); // n 天前的 0 点
  const at = timeOf(t, now);
  return at >= ago(0) ? "today" : at >= ago(1) ? "yesterday" : at >= ago(6) ? "week" : "older";
}
const newest = (now) => (a, b) => timeOf(b, now) - timeOf(a, now);

// 标题太长、截成“…”的：鼠标停上去（或 Tab 到这一行），等半秒以后像跑马灯一样一直往左走，整条走过去，后面隔一段接着同一个标题，
// 一圈一圈不停（2026-11-07 Sean 要的）。每次停上去量一次标题多长，按每秒 40px 定一圈多久。
// 减少动效时不滚，靠悬停提示看全名
const REDUCE = matchMedia("(prefers-reduced-motion: reduce)");
const GAP = 32; // 标题和后面接着的那一份之间空多少
function marquee(e) {
  if (e.type === "pointerenter" ? e.pointerType !== "mouse" : !e.target.matches(":focus-visible")) return;
  const box = e.currentTarget.querySelector("[data-title]");
  if (!box) return;
  if (box.scrollWidth - box.clientWidth < 2 || REDUCE.matches) { box.removeAttribute("data-run"); return; }
  const lap = box.firstChild.firstChild.getBoundingClientRect().width + GAP; // 走一圈的距离：标题本身加中间的空
  box.dataset.run = "";
  box.style.setProperty("--gap", `${GAP}px`);
  box.style.setProperty("--far", `-${lap}px`);
  box.style.setProperty("--dur", `${(lap / 40).toFixed(2)}s`);
}

function rowSub(t, now) {
  if (t.status === "queued") return "排队中";
  if (t.status === "running") return t.running_since ? `进行中，已用 ${elapsed(t.running_since, now)}` : "进行中";
  if (t.status === "failed") return "没有完成，可以重试";
  return "";
}

// 列表每隔几秒取一次；当前这个线程以页面手上的状态为准，刚新建、列表里还没有的先补一行（左栏和搜对话的窗口都用）
export function withCurrent(threads, current, st, live, busy) {
  if (!current) return threads;
  const mine = threads.find((t) => t.thread_id === current);
  const title = live?.title || st?.title;
  const patch = { ...(busy ? { status: "running", running_since: st?.running_since || mine?.running_since } : st ? { status: st.status } : {}),
    ...(title ? { title } : {}) };
  return mine ? threads.map((t) => (t.thread_id === current ? { ...t, ...patch } : t))
    : [{ thread_id: current, title: live?.said || "新对话", status: "running", ...patch }, ...threads];
}

// 窄屏时这一栏是抽屉（nav.drawer 开着）：打开时焦点进来，Tab 在里面转，Esc、点外面、右上角的关闭都能关上。
// 抽屉里的菜单、改名框开着时，Esc 先归它们
const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])';

// href：对话 id → 地址（新对话是 null）；onLibrary：去资料库页（没有登录功能的部署没有资料库，是 null，不放这一行）；
// library：现在在资料库页（那一行是选中的，对话、新对话都不是）
export function Sidebar({ threads, current, st, live, busy, now, href, onSelect, manage, onSettings, onLibrary, library, onSearch, auth, onLogin, credits, onCredits, nav }) {
  const [menu, setMenu] = useState(null); // 打开菜单的那一行：{id, anchor: 那一行的“⋯”}
  const [renaming, setRenaming] = useState(null); // 正在改名的线程 id
  const box = useRef(null);
  const list = useRef(null);
  const newBtn = useRef(null);
  const modal = nav.mobile && nav.drawer;
  useEffect(() => { if (modal) box.current?.querySelector('[data-nav="close"]')?.focus(); }, [modal]);
  useLayer(modal, () => nav.closeDrawer()); // 抽屉上又开了菜单、弹层时，Esc 先收那些（它们在这一层上面）
  const onKeyDown = (e) => {
    if (!modal) return;
    if (e.key === "Tab") {
      const all = [...box.current.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
      if (!all.length) return;
      const [first, last] = [all[0], all.at(-1)];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  const rows = withCurrent(threads || [], current, st, live, busy);
  // 标题一样、右边的时间也一样的几条（比如更早的两个“广州今日天气”，右边都只写日期）：下面多一行日期和钟点，好分清。
  // 右边的时间已经分得清（今天、昨天的写到钟点）就不再多写一行
  const twinKey = (t) => `${t.title}\n${when(t.updated_at, now, groupOf(t, now))}`;
  const seen = {};
  for (const t of rows) seen[twinKey(t)] = (seen[twinKey(t)] || 0) + 1;
  const groups = GROUPS.map(([key, name]) => {
    const items = rows.filter((t) => groupOf(t, now) === key);
    items.sort(key === "pinned" ? (a, b) => Date.parse(b.pinned_at) - Date.parse(a.pinned_at) : newest(now));
    return [key, name, items];
  }).filter(([, , items]) => items.length);

  // 删掉一行以后焦点放到下一行（没有就上一行、再没有就“新对话”）：删的按钮跟着那一行没了，焦点不掉到页面上
  const neighbor = (id) => {
    const links = [...list.current.querySelectorAll("a[data-id]")];
    const i = links.findIndex((a) => a.dataset.id === id);
    return links[i + 1] || links[i - 1] || newBtn.current;
  };
  const focusRow = (id) => list.current?.querySelector(`a[data-id="${CSS.escape(id)}"]`)?.focus();
  const menuRow = menu && rows.find((t) => t.thread_id === menu.id); // 菜单开着时那一行变了（开始回答了）按新的
  const sz = nav.mobile ? 18 : 14;

  const row = (t, group) => {
    if (renaming === t.thread_id) {
      return (
        <div key={t.thread_id} className={cx(styles.row, styles.renaming)}>
          <TitleInput value={t.title} onDone={(title, how) => {
            setRenaming(null);
            if (title && title !== t.title) manage.rename(t.thread_id, title);
            if (how !== "blur") requestAnimationFrame(() => focusRow(t.thread_id)); // 回车、Esc：回到这一行；点了别处就在别处
          }} />
        </div>
      );
    }
    const sel = !library && t.thread_id === current;
    const open = menu?.id === t.thread_id;
    const status = ACTIVE.has(t.status) ? "active" : t.status === "failed" ? "failed" : "idle";
    const twin = seen[twinKey(t)] > 1 && stamp(t.updated_at, now);
    const sub = rowSub(t, now) || (twin ? sayWhen(twin) : "");
    return (
      <div key={t.thread_id} className={cx(styles.row, sel && styles.sel, open && styles.menu)} onPointerEnter={marquee} onFocus={marquee}>
        <a href={href(t.thread_id)} data-id={t.thread_id} className={styles.link} aria-current={sel ? "page" : undefined}
          onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onSelect(t.thread_id); }}>
          <span className={cx(styles.icon, styles[status])}>
            {status === "active" ? <Spinner size={15} /> : status === "failed" ? <CircleAlert size={15} strokeWidth={1.8} /> : <MessageSquare size={15} strokeWidth={1.8} />}
          </span>
          <span className={styles.body}>
            <span className={styles.title} data-title title={REDUCE.matches ? t.title : undefined}>
              <span><span>{t.title}</span><span aria-hidden="true">{t.title}</span></span>
            </span>
            {sub && <span className={styles.sub}>{sub}</span>}
          </span>
          <span className={styles.time}>{when(t.updated_at, now, group)}</span>
        </a>
        <button className={styles.more} aria-label={`“${t.title}”的更多操作`} aria-haspopup={nav.mobile ? "dialog" : "menu"} aria-expanded={open}
          onClick={(e) => setMenu(open ? null : { id: t.thread_id, anchor: e.currentTarget })}>
          <IconMore size={15} />
        </button>
      </div>
    );
  };

  return (
    <>
      {modal && <div className={styles.scrim} onClick={nav.closeDrawer} aria-hidden="true" />}
      <aside ref={box} id="ws-side" className={styles.side} onKeyDown={onKeyDown}
        {...(modal ? { role: "dialog", "aria-modal": true, "aria-label": "对话列表" } : {})}>
        {/* 品牌这行：右边是搜对话（照豆包）、收起（桌面）或关闭（抽屉），两个钮上下往外让 4px，不把这行撑高 */}
        <div className="flex items-center gap-[9px] px-1.5 phone:min-h-11">
          <Logo size={28} className="flex-none" />
          <span className="font-serif text-[17px] leading-none font-semibold tracking-[.04em]">灵稿</span>
          <span className={cx(styles.tag, "text-[12px] text-muted")}>对话</span>
          <button className="ws-icon-btn -my-1 mr-0 ml-auto phone:my-0" aria-label="搜索对话" title={nav.mobile ? undefined : `搜索对话（${SEARCH_KEY}）`}
            aria-keyshortcuts={SEARCH_ARIA_KEY} aria-haspopup="dialog" onClick={onSearch}><IconSearch size={18} /></button>
          {nav.mobile
            ? <button className="ws-icon-btn my-0 -mr-2 ml-0" data-nav="close" aria-label="关闭对话列表" onClick={nav.closeDrawer}><IconClose size={18} /></button>
            : <button className="ws-icon-btn -my-1 -mr-1 ml-0" data-nav="collapse" aria-label="收起侧栏" title={`收起侧栏（${SIDE_KEY}）`} aria-keyshortcuts={SIDE_ARIA_KEY}
              onClick={nav.toggleSide}><IconPanel size={18} /></button>}
        </div>
        <div className="flex flex-col gap-1">
          <button ref={newBtn} className={cx(styles.newChat, "flex h-10 items-center gap-2 rounded-md border border-line2 bg-paper px-3 py-0 text-[13.5px] font-medium phone:h-11")}
            onClick={() => onSelect(null)} aria-current={!current && !library ? "page" : undefined}>
            <SquarePen size={15} strokeWidth={1.8} className="text-accent" /> 新对话
          </button>
          {/* 资料库（11-10 从设置里搬出来，同一天改成自己一页、叫资料库）：一行字，图标、字和下面对话那几行对齐；在资料库页时和选中的对话一个样子 */}
          {onLibrary && (
            <a href="/library" className={cx(styles.place, "flex h-9 items-center gap-[9px] rounded-md px-2.5 text-[13px] text-ink no-underline phone:h-11")}
              aria-current={library ? "page" : undefined}
              onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onLibrary(); }}>
              <LibraryBig size={15} strokeWidth={1.8} className={library ? "text-accent" : "text-muted"} />资料库
            </a>
          )}
        </div>
        <nav ref={list} className="-mx-1 flex flex-1 flex-col gap-[18px] overflow-y-auto px-1" aria-label="对话列表">
          {threads === null && (
            <div className="flex flex-col gap-4 px-2.5 py-2" role="status" aria-label="正在取对话列表">
              {[78, 62, 70, 54, 66].map((w) => (
                <span key={w} className="flex items-center gap-[9px]"><span className="block size-[15px] flex-none rounded-sm bg-hover" /><span className="block h-4 rounded-sm bg-hover" style={{ width: `${w}%` }} /></span>
              ))}
            </div>
          )}
          {threads !== null && rows.length === 0 && (
            <div className="flex flex-col items-center gap-2 px-2 pt-8 text-center text-[13px] leading-[1.6] text-muted">
              <MessagesSquare size={22} strokeWidth={1.6} className="text-faint" />还没有对话
            </div>
          )}
          {groups.map(([key, name, items]) => (
            <div className="flex flex-col gap-px" key={key} role="group" aria-label={name}>
              <div className="flex gap-1.5 px-2.5 pb-1 text-[12px] font-medium text-muted" aria-hidden="true">
                {name}<span className="font-normal text-faint tabular-nums">{items.length}</span>
              </div>
              {items.map((t) => row(t, key))}
            </div>
          ))}
        </nav>
        {/* 最下面一行：左边额度，右边账号（游客是“游客”头像，没有登录功能的部署只有“设置”） */}
        <div className="flex items-center gap-3 border-t border-line px-2 pt-2 pb-0.5 phone:min-h-11 phone:px-1 phone:pt-1 phone:pb-0">
          <CreditsFoot credits={credits} guest={!auth.me} onLogin={auth.login ? onLogin : null} onOpen={onCredits} mobile={nav.mobile} />
          <span className="ml-auto flex min-w-0 items-center"><UserFoot auth={auth} onLogin={onLogin} onSettings={onSettings} mobile={nav.mobile} /></span>
        </div>
        {menuRow && (
          <RowMenu key={menuRow.thread_id} t={menuRow} anchor={menu.anchor} mobile={nav.mobile} size={sz} onClose={() => setMenu(null)}
            onPin={() => { manage.pin(menuRow.thread_id, !menuRow.pinned_at); setMenu(null); }}
            onRename={() => { setRenaming(menuRow.thread_id); setMenu(null); }}
            onExport={() => { manage.exportMd(menuRow.thread_id); setMenu(null); }}
            onDelete={() => {
              const next = neighbor(menuRow.thread_id);
              setMenu(null);
              manage.remove(menuRow.thread_id);
              next?.focus();
            }} />
        )}
      </aside>
    </>
  );
}

// 一行对话的菜单：置顶、重命名、导出、删除。删除不再先确认，删了 5 秒内能撤销（第 44 条）；正在回答的不能改名、删除，没跑完的不能改名
function RowMenu({ t, anchor, mobile, size, onClose, onPin, onRename, onExport, onDelete }) {
  const running = ACTIVE.has(t.status);
  const note = running ? "正在回答，答完或停下后才能改名、删除" : t.status === "failed" ? "这段对话没有完成，重试以后才能改名" : null;
  const items = [
    { key: "pin", icon: <IconPinTop size={size} />, text: t.pinned_at ? "取消置顶" : "置顶", onClick: onPin },
    { key: "rename", icon: <IconEdit size={size} />, text: "重命名", disabled: t.status !== "idle", onClick: onRename },
    { key: "export", icon: <IconDownload size={size} />, text: "导出为 Markdown", onClick: onExport },
    { key: "delete", icon: <IconTrash size={size} />, text: "删除", danger: true, disabled: running, onClick: onDelete },
  ];
  return <ActionMenu label={`“${t.title}”的操作`} anchor={anchor} mobile={mobile} items={items} note={note} onClose={onClose} />;
}
