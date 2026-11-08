// 对话页的导航：桌面上左栏能整栏收起（收起后顶栏左边出展开钮、搜对话和新对话），窄屏（≤720px）左栏改成从左边滑出的抽屉，
// 顶栏是“抽屉钮 + 标题 + 新对话”。对话页用这条顶栏，状态由 Workspace 经 NavContext 给。
import { createContext, useContext, useEffect, useState } from "react";
import { SquarePen } from "lucide-react";
import { IconMenu, IconPanel, IconSearch } from "./icons.jsx";
import { TitleInput } from "./menu.jsx";

export const NavContext = createContext(null);

export const MOBILE = "(max-width: 720px)";
export const SPLIT = "(min-width: 1024px)"; // 右栏和对话左右分栏（窄一点的桌面右栏盖在对话上，见 artifact.module.css）

// 触屏（手指点的）：程序不把光标放进输入框，免得自己弹出键盘（批次 G 第 42 条）
export const touch = () => matchMedia("(pointer: coarse)").matches;

// 临时的输入框（改这句话、左栏改名）关掉以后，光标回到对话的输入框（等它从页面上撤掉再放；输入框听这个事件，见 composer.jsx）。
// force：光标停在别的按钮上、选着字也放过去（用户点开文件、关掉右栏和大图，是要接着打字，2026-11-09 Sean 要的）
export const focusInput = (force = false) => setTimeout(() => dispatchEvent(new CustomEvent("ws-focus-input", { detail: force })));

// 关掉大图、右栏以后光标去哪：桌面上回输入框；手机上（全屏的盖着输入框，触屏还会弹出键盘）回到点开它的那个按钮
export function typeNext(back) {
  if (matchMedia(MOBILE).matches) back?.focus?.({ preventScroll: true });
  else focusInput(true);
}

// 最近一次是用键盘还是用鼠标、手指：键盘选的对话焦点留在左栏那一行，鼠标点开的光标进输入框（批次 G 第 42 条）
let keyed = false;
addEventListener("keydown", (e) => { if (!["Shift", "Control", "Alt", "Meta"].includes(e.key)) keyed = true; }, true);
addEventListener("pointerdown", () => { keyed = false; }, true);
export const byKeyboard = () => keyed;

// 媒体查询现在对不对得上，窗口变了跟着变
export function useMedia(query) {
  const [on, setOn] = useState(() => matchMedia(query).matches);
  useEffect(() => {
    const mq = matchMedia(query);
    const change = () => setOn(mq.matches);
    change();
    mq.addEventListener("change", change);
    return () => mq.removeEventListener("change", change);
  }, [query]);
  return on;
}

// 收起、展开左栏的快捷键：⌘⇧S（Mac）/ Ctrl+Shift+S，写在按钮的提示里
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const SIDE_KEY = MAC ? "⌘⇧S" : "Ctrl+Shift+S";
export const SIDE_ARIA_KEY = MAC ? "Meta+Shift+S" : "Control+Shift+S";
export const isSideKey = (e) => (MAC ? e.metaKey : e.ctrlKey) && e.shiftKey && !e.altKey && e.code === "KeyS";
// 搜对话的窗口：⌘K / Ctrl+K（见 search.jsx；左栏收起、手机上抽屉没开也能直接打开）
export const SEARCH_KEY = MAC ? "⌘K" : "Ctrl+K";
export const SEARCH_ARIA_KEY = MAC ? "Meta+K" : "Control+K";
export const isSearchKey = (e) => (MAC ? e.metaKey : e.ctrlKey) && !e.shiftKey && !e.altKey && e.code === "KeyK";

// 顶栏：左边是导航按钮，中间标题（那栏和正文同宽 832，标题在里面居中，2026-11-07 Sean 定；两边的按钮放不下时标题那栏让出来），右边窄屏时是新对话。
// ≤1100 时顶栏左内边距 14 + 8 的间隔和正文左右边距 22 对上（2026-11-07 Sean 批的刻度外例外）。title 为 null 是还不知道标题（打开中），显示一条占位。
// end：右边再放的（对话页三轮起的轮次目录按钮，见 turns.jsx；这个对话的“⋯”菜单，见 timeline.jsx）。
// edit：在顶栏上改标题时是 {value, onDone}，标题换成输入框
export function HeadBar({ title, end, edit }) {
  const nav = useContext(NavContext);
  const side = nav?.mobile ? (
    <button className="ws-icon-btn" data-nav="drawer" aria-label="打开对话列表" aria-controls="ws-side" aria-expanded={nav.drawer} onClick={nav.openDrawer}>
      <IconMenu size={18} />
    </button>
  ) : nav?.collapsed ? (
    <>
      <button className="ws-icon-btn" data-nav="expand" aria-label="展开侧栏" title={`展开侧栏（${SIDE_KEY}）`} aria-keyshortcuts={SIDE_ARIA_KEY} onClick={nav.toggleSide}>
        <IconPanel size={18} />
      </button>
      <button className="ws-icon-btn" aria-label="搜索对话" title={`搜索对话（${SEARCH_KEY}）`} aria-keyshortcuts={SEARCH_ARIA_KEY} aria-haspopup="dialog" onClick={nav.search}>
        <IconSearch size={18} />
      </button>
      <button className="ws-icon-btn" aria-label="新对话" title="新对话" onClick={nav.newChat}><SquarePen size={18} /></button>
    </>
  ) : null;
  return (
    <header className={"grid h-[58px] flex-none grid-cols-[1fr_minmax(0,832px)_1fr] items-center gap-2 border-b border-line px-3 narrow:pl-3.5 print:hidden!"
      + " phone:h-12 phone:grid-cols-[1fr_minmax(0,max-content)_1fr] phone:gap-1 phone:px-1"}>
      <div className="flex items-center gap-1">{side}</div>
      {edit ? <TitleInput value={edit.value} head onDone={edit.onDone} /> : (
        <div className="min-w-0 truncate text-center text-[16px] font-semibold" title={title || undefined}>
          {title ?? <span className="mx-auto block h-4 w-2/5 max-w-[240px] rounded-sm bg-hover" aria-label="正在打开对话" role="status" />}
        </div>
      )}
      <div className="flex items-center justify-end gap-1">
        {end}
        {nav?.mobile && <button className="ws-icon-btn" aria-label="新对话" onClick={nav.newChat}><SquarePen size={18} /></button>}
      </div>
    </header>
  );
}
