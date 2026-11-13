// 不是一打开就要的几块，用到才去取（各自一个文件，不放进打开页面就要下载的那一包）：资料库页、定时任务页、设置窗口、搜对话的窗口、海报页。
// 对话页空下来以后先悄悄取好（warm），点开时一般已经在了；还没取到的那一小会儿，页面那一栏先空着、窗口晚一点出来（见用它们的地方的 Suspense）。
// 没取到（多半是刚发了新版、旧的文件已经没了，也可能是断网）：在那个位置说一句、给个“刷新”，不让整页白掉。
import { lazy } from "react";
import { IconClose, IconWarn } from "./icons.jsx";

const LOAD = {
  library: () => import("./knowledge.jsx"),
  scheduled: () => import("./scheduled.jsx"),
  settings: () => import("./settings.jsx"),
  search: () => import("./search.jsx"),
};

// 窗口（给了 onClose）：浮在页面上边，能关；页面：占着那一栏
function Gone({ onClose }) {
  const bar = (
    <div className="ws-error m-0 items-center" role="alert">
      <IconWarn size={15} />
      <span>这部分没有打开，可能是刚更新了版本。刷新页面再试。</span>
      <button className="ws-btn ws-error-act phone:min-h-11" onClick={() => location.reload()}>刷新</button>
      {onClose && <button className="ws-icon-btn phone:min-h-11 phone:min-w-11" aria-label="关闭" onClick={onClose}><IconClose size={15} /></button>}
    </div>
  );
  return onClose ? <div className="fixed inset-x-4 top-4 z-[80] mx-auto max-w-md">{bar}</div>
    : <section className="min-w-0 p-6 phone:p-4" aria-label="没有打开">{bar}</section>;
}

const part = (load, pick) => lazy(() => load().then((m) => ({ default: pick(m) }), () => ({ default: Gone })));

export const LibraryPage = part(LOAD.library, (m) => m.LibraryPage);
export const ScheduledPage = part(LOAD.scheduled, (m) => m.ScheduledPage);
export const SettingsDialog = part(LOAD.settings, (m) => m.SettingsDialog);
export const SearchDialog = part(LOAD.search, (m) => m.SearchDialog);
export const PosterApp = part(() => import("./poster.jsx"), (m) => m.default);

// 对话页用的那四块先取好。没取到就算了：用到时再取一次
export const warm = () => Object.values(LOAD).forEach((load) => load().catch(() => {}));
