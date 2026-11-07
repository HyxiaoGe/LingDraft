// 对话页的导航：桌面上左栏能整栏收起（收起后顶栏左边出展开钮和新对话），窄屏（≤720px）左栏改成从左边滑出的抽屉，
// 顶栏是“抽屉钮 + 标题 + 新对话”。对话页、设置页、记忆页共用这条顶栏，状态由 Workspace 经 NavContext 给。
import { createContext, useContext, useEffect, useState } from "react";
import { IconChevron, IconMenu, IconPanel, IconPlus } from "./icons.jsx";
import { TitleInput } from "./menu.jsx";

export const NavContext = createContext(null);

export const MOBILE = "(max-width: 720px)";

// 触屏（手指点的）：程序不把光标放进输入框，免得自己弹出键盘（批次 G 第 42 条）
export const touch = () => matchMedia("(pointer: coarse)").matches;
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
// 搜索对话标题：⌘K / Ctrl+K（左栏收起时先展开，手机上先开抽屉）
export const SEARCH_KEY = MAC ? "⌘K" : "Ctrl+K";
export const SEARCH_ARIA_KEY = MAC ? "Meta+K" : "Control+K";
export const isSearchKey = (e) => (MAC ? e.metaKey : e.ctrlKey) && !e.shiftKey && !e.altKey && e.code === "KeyK";

// 顶栏：左边是导航按钮，中间标题（和正文同宽、居中），右边窄屏时是新对话。title 为 null 是还不知道标题（打开中），显示一条占位。
// end：右边再放的（对话页三轮起的轮次目录按钮，见 turns.jsx；这个对话的“⋯”菜单，见 timeline.jsx）。
// edit：在顶栏上改标题时是 {value, onDone}，标题换成输入框
// back：标题左边的返回（记忆页回设置页）：{label, onClick}
export function HeadBar({ title, end, edit, back }) {
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
      <button className="ws-icon-btn" aria-label="新对话" title="新对话" onClick={nav.newChat}><IconPlus size={18} /></button>
    </>
  ) : null;
  return (
    <header className="ws-head">
      <div className="ws-head-side">
        {side}
        {back && <button className="ws-btn ghost sm ws-head-back" aria-label={`返回${back.label}`} onClick={back.onClick}><IconChevron size={14} className="back" />{back.label}</button>}
      </div>
      {edit ? <TitleInput value={edit.value} className="head" onDone={edit.onDone} /> : (
        <div className="ws-head-title" title={title || undefined}>
          {title ?? <span className="ws-skel ws-skel-title" aria-label="正在打开对话" role="status" />}
        </div>
      )}
      <div className="ws-head-side end">
        {end}
        {nav?.mobile && <button className="ws-icon-btn" aria-label="新对话" onClick={nav.newChat}><IconPlus size={18} /></button>}
      </div>
    </header>
  );
}
