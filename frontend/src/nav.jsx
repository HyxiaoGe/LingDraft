// 对话页的导航：桌面上左栏能整栏收起（收起后顶栏左边出展开钮和新对话），窄屏（≤720px）左栏改成从左边滑出的抽屉，
// 顶栏是“抽屉钮 + 标题 + 新对话”。对话页和记忆页共用这条顶栏，状态由 Workspace 经 NavContext 给。
import { createContext, useContext, useEffect, useState } from "react";
import { IconMenu, IconPanel, IconPlus } from "./icons.jsx";

export const NavContext = createContext(null);

export const MOBILE = "(max-width: 720px)";

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

// 顶栏：左边是导航按钮，中间标题（和正文同宽、居中），右边窄屏时是新对话。title 为 null 是还不知道标题（打开中），显示一条占位
export function HeadBar({ title }) {
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
      <div className="ws-head-side">{side}</div>
      <div className="ws-head-title" title={title || undefined}>
        {title ?? <span className="ws-skel ws-skel-title" aria-label="正在打开对话" role="status" />}
      </div>
      <div className="ws-head-side end">
        {nav?.mobile && <button className="ws-icon-btn" aria-label="新对话" onClick={nav.newChat}><IconPlus size={18} /></button>}
      </div>
    </header>
  );
}
