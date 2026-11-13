// 对话页：左侧对话列表（能搜标题，每条能置顶、改名、导出、删除），右侧对话；头像菜单里的“设置”弹出设置窗口（记忆也在里面），后面的对话不动。
// 页面以服务器上的线程状态为准：对话和来源都从状态里取。执行中的事件（思考和正文的逐字片段、工具步骤）
// 叠在存下来的对话上实时显示，并提示该重新取一次状态了；所以刷新、换个页面打开、或者中途断线重连，看到的都一样。
// 回答时又说的话排在服务器上，这一轮答完或停下就接着发成新的一轮：这一轮的事件流结束时线程又在跑了，页面接着读下一次执行的。
// 这一轮出错时排着的话不发，取回输入框。排着的话也能插进这一轮：放进对话时事件流里来一条 guide，页面把它挪进过程里。
// 模型也会停下来问用户（状态 asking，问题在 state.ask）：这时没有执行在跑，回答或跳过以后同一轮接着跑（见 act.answer）；排着的话留在服务器上，等这一轮答完再发。
// 问着的时候点停止走 dismiss：没有事件流，服务器收尾以后取一次状态；重新回答、改那句话和空闲时一样（服务器先把这一轮收尾）。
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { answerResearchThread, createResearchThread, deleteResearchThread, editResearchThread, followResearchThread, getCredits, getResearchState, guideResearch, listModels, listResearchThreads,
  markThreadRead, pinResearchThread, queueResearch, regenerateResearchThread, renameResearchThread, retryResearchThread, sayResearch, setResearchAsks, stopResearchThread, unguideResearch, unqueueResearch } from "./api.js";
import { localWho } from "./auth.js";
import { setDraft } from "./drafts.js";
import { AFTER_RUN_ERROR, errorText, quotaText } from "./errors.js";
import { LoginDialog, useAuth } from "./login.jsx";
import { saveThinkDefault, thinkDefault, useTheme } from "./prefs.js";
import { Sidebar, withCurrent } from "./sidebar.jsx";
import { MOBILE, NavContext, SPLIT, focusInput, isSearchKey, isSideKey, typeNext, useMedia } from "./nav.jsx";
import { ACTIVE, ASKING, LOCATION, QUEUED, addLive, itemKey, saveFile, threadMarkdown, useNow } from "./research.js";
import { FilePanel } from "./artifact.jsx";
import { FileList, KnowledgePanel, SentPanel } from "./workspace-files.jsx";
import { opener } from "./layers.js";
import { away, useDoneAlert, useTaskAlert } from "./alert.jsx";
import { NOTICES_MAX, Notices, noticeLine } from "./notice.jsx";
import { LibraryPage, ScheduledPage, SearchDialog, SettingsDialog, warm } from "./parts.jsx";
import { useEvent, useLatest } from "./stable.js";
import { Conversation, forgetReading } from "./timeline.jsx";
import { UndoToast, useUndo } from "./undo.jsx";
import { notSendableText, uploadErrorText } from "./upload.js";
// 样式：对话页的变量、基础样式和共用的小件在 tailwind.css，各块的在各自的 *.module.css

// 取回来的列表和手上的是不是一样（一样就不换，左栏不用跟着重新渲染）：空闲时每 20 秒取一次，绝大多数时候什么都没变
const sameList = (a, b) => a.length === b.length && JSON.stringify(a) === JSON.stringify(b);

// 地址：/chat/new 是新对话，/work/new 是新工作任务（同一个新页面，上面的“对话 / 工作”选着工作），/chat/<线程 id> 是某个对话或工作任务；
// 别的地址（/、/chat）都当新对话，进来时改成 /chat/new。
// 线程 id 只有字母、数字、- 和 _（后端校验），地址里不用编码；对不上的 id 后端回 404，页面换成新对话
const NEW_PATH = "/chat/new";
const NEW_WORK_PATH = "/work/new";
const newPath = (work) => (work ? NEW_WORK_PATH : NEW_PATH);
const chatPath = (id) => (id ? `/chat/${id}` : NEW_PATH);
const routeWork = () => /^\/work(\/new)?\/?$/.test(location.pathname);
// 以前的设置页、记忆页地址（/settings、/memory，11-07 改成窗口之前）：打开的话弹出设置窗口的那一页，地址换成对话的
const oldPage = () => location.pathname.match(/^\/(settings|memory)\/?$/)?.[1] || null;
// 占对话那一栏的页面，左栏照旧：资料库（2026-11-10，见 knowledge.jsx）、定时任务（2026-11-13，见 scheduled.jsx）。
// 去这些页时开着的对话不断开（在跑的接着跑），点回来马上接上
const PAGES = { library: "/library", scheduled: "/scheduled" };
const PAGE = 50; // 左栏的对话列表一次取多少段：先取最近的这么多，滚到底再多取这么多

// 搜对话、记忆页用的整份列表：all 是打开它们时取的那一次，loaded 是左栏那份（一直在更新）。两边都有的行按左栏的；
// 取了整份以后才有的（刚新建的）放最前；等着撤销删除的不要
function wholeList(all, loaded, gone) {
  const fresh = new Map(loaded.map((t) => [t.thread_id, t]));
  const had = new Set(all.map((t) => t.thread_id));
  return [...loaded.filter((t) => !had.has(t.thread_id)), ...all.map((t) => fresh.get(t.thread_id) || t)].filter((t) => !gone.has(t.thread_id));
}
const PAGE_TITLES = { library: "资料库", scheduled: "定时任务" };
const routePage = () => Object.keys(PAGES).find((k) => location.pathname.replace(/\/$/, "") === PAGES[k]) || null;
const routeId = () => {
  const m = location.pathname.match(/^\/chat\/([^/]+)\/?$/);
  return m && m[1] !== "new" ? m[1] : null;
};
// 改地址栏：push 进浏览器历史（后退能回来），否则换掉当前这条；已经是这个地址就不动
const navigate = (path, push) => {
  if (location.pathname !== path || location.hash) history[push ? "pushState" : "replaceState"](null, "", path);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 桌面上左栏收起没有：记在本机（取不到、存不下就按展开）
const SIDE_STORE = "lingdraft.side";
const sideOffSaved = () => { try { return localStorage.getItem(SIDE_STORE) === "off"; } catch { return false; } };
const saveSideOff = (off) => { try { if (off) localStorage.setItem(SIDE_STORE, "off"); else localStorage.removeItem(SIDE_STORE); } catch { /* 存不下就只管这一次 */ } };
const fileName = (title) => (title || "").replace(/[\\/:*?"<>|\s]+/g, " ").trim().slice(0, 60) || "对话";

// 一轮结束时还留在服务器上的话：要放回输入框的那几种。停下来问用户的不算结束：排着的话等回答以后接着发，不取回来
const leftover = (s) => (s.status === ASKING ? [] : [s.guided_message && "guided", s.queued_message && "queued"].filter(Boolean));

export default function Workspace() {
  const auth = useAuth(); // 身份：游客还是登录了（见 auth.js）。身份一变整页重载，这里不用跟着改状态
  const [earlyWho] = useState(localWho); // 认人做完之前，欢迎语先按本机记着的名字说（见 auth.localWho）
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginWant, setLoginWant] = useState(null); // 游客点了“登录后可用”里的哪个模型（登录弹窗说登录后就能用它，第 28 条）
  const [threads, setThreads] = useState(null);
  // 左栏的列表先取最近的 PAGE 段，滚到底再多取（见 loadMore）：more 是下面还有没取的，listLimit 是现在取到最近的多少段。
  // 搜对话、记忆页对出处要全的，开着它们时另取一次整份（见下面 all）
  const [more, setMore] = useState(false);
  const listLimit = useRef(PAGE);
  const listSeq = useRef(0); // 取列表的请求按发出的先后编号；listTaken 是已经用上的最新那次：比它早发、晚到的不要（多取以后，早发的那次会把列表变短）
  const listTaken = useRef(0);
  const listIds = useRef(new Set()); // 手上这份列表里有哪些对话
  const growing = useRef(false); // 正在多取
  const [tid, setTid] = useState(routeId);
  const [search, setSearch] = useState(false); // 搜对话的窗口开着没有（见 search.jsx）
  const [settings, setSettings] = useState(oldPage); // 设置窗口开着时是窗口里的哪一类（见 settings.jsx，"settings" 是刚打开）；关着是 null
  // 整份对话列表：搜对话、记忆页对出处要全的（左栏只取了最近的一部分），开着它们时取一次，关上就不留。还在取是 null；没取到就只用左栏那份
  const [all, setAll] = useState(null);
  const wantAll = search || settings === "memory";
  useEffect(() => {
    if (!wantAll) return undefined;
    let on = true;
    listResearchThreads().then((got) => on && setAll(got.threads), () => on && setAll([]));
    return () => {
      on = false;
      setAll(null);
    };
  }, [wantAll]);
  const [page, setPageState] = useState(routePage); // 对话那一栏放的是什么：null 是对话，"library" 是资料库页，"scheduled" 是定时任务页
  const [theme, setTheme, dark] = useTheme(); // 主题（设置窗口里选，存本机）：跟随系统、浅色、深色
  const [st, setSt] = useState(null);
  // 这个页面发起或接上的那次执行：{said, images, sentAt, turnsAtStart, items, stage, phase, title, stopping, hidden, edit}
  // images：这句话带的图（[{key, file, url}]，见 Composer），存下来之前先按本机的显示；docs：带的文件（见 composer.jsx 的 picked），同样先按本机的显示
  // sentAt：这次说的话或改的话按本机时间记的发送时间，存下来的对话回来以前先显示它
  const [live, setLive] = useState(null);
  const [suggesting, setSuggesting] = useState(false); // 这一轮结束了，推荐问题还在起（同一条事件流里还会再来一次状态）
  const [busy, setBusyState] = useState(false);
  const [error, setError] = useState(null);
  const [putBack, setPutBack] = useState(null); // 要放回输入框的话：{thread, text, images?, docs?, rejected?}，每次是个新对象；images、docs 是一起没发出去的图和文件；rejected 是没发出去被拒的（连着被拒不叠加）
  // 能选的模型（/api/models，Auto 在第一个）：没取到是 []，不显示选择，新对话按 Auto
  const [models, setModels] = useState(null);
  const [modelNames, setModelNames] = useState({}); // 选项以外、回答开头可能出现的模型的名字和图标（见 listModels）
  const loadModels = () => listModels().then((r) => { setModels(r.models); setModelNames(r.names); });
  // 新对话选的模型：null 是 Auto；开新对话时还是上次选的，刷新页面回到 Auto。开始的对话以它定下的为准（st.model）
  const [pick, setPick] = useState(null);
  // 深度思考开关：每句话发的时候带上（选的模型能开关才带）。新对话（包括刷新）按设置里的默认（存本机，默认开）；
  // 在一段对话里手动切了只管这段对话，记在 thinkSet 里（这次打开的页面里），回到这段对话还是切过的，开新对话又回到默认（2026-11-07 Sean 定）。
  // 开关只管对话：工作任务一直开着、关不了（2026-11-12 Sean 定，后端也不看，见 agent.py 的 _plan），输入框旁边的开关显示成开、点不动，发的时候不带。
  const [thinkOn, setThinkOn] = useState(thinkDefault); // 设置里的默认
  const thinkOnRef = useRef(thinkOn);
  thinkOnRef.current = thinkOn;
  const [thinking, setThinkingState] = useState(thinkOn);
  const thinkSet = useRef(new Map()); // 对话 id（新对话是 "new"）→ 手动切成的开关
  const setThinking = (v) => {
    thinkSet.current.set(tidRef.current ?? "new", v);
    setThinkingState(v);
  };
  // 对话 / 工作（2026-11-10 Sean 定分开；11-11 照豆包改：新建时选，定下以后整段不变，工作任务不能变回对话）。
  // 工作任务里，计划、调研、写报告这类活模型先用问题卡问清楚再动手；对话不出问题卡。
  // 新页面选着哪个记在这里（左栏两个入口、页面上的大开关都改它，地址跟着是 /chat/new 或 /work/new）；开始以后看对话自己的（见下面的 work）
  const [newWork, setNewWorkState] = useState(routeWork);
  const newWorkRef = useRef(newWork);
  const setNewWork = (v) => {
    newWorkRef.current = v;
    setNewWorkState(v);
  };
  const startedAs = useRef(null); // 这个页面刚发起的那段对话 {id, work}：状态、列表还没取回来时先按它
  const pathOf = (id) => (id ? chatPath(id) : newPath(newWorkRef.current)); // 这一页的地址：新页面按选着的是对话还是工作
  // 额度（/api/credits）：打开页面、每轮结束、打开账号菜单时取；没取到是 null（不显示）
  const [credits, setCredits] = useState(null);
  const refreshCredits = () => getCredits().then(setCredits).catch(() => {});
  const gen = useRef(0); // 每切一次线程加一：旧线程的连接和回调发现对不上，就不再改页面
  const tidRef = useRef(tid);
  const pageRef = useRef(page);
  const setPage = (p) => {
    pageRef.current = p;
    setPageState(p);
  };
  const ctl = useRef(new AbortController()); // 当前线程的连接；切走、或开始新的一次执行时断开（服务器上的执行照常跑）
  const lastEvent = useRef(null); // 收到的最后一个事件 id，断线后凭它接着读
  const busyRef = useRef(false);
  const refetchAt = useRef(0);
  const refetchTimer = useRef(null);
  // 导航（见 nav.jsx）：桌面上左栏可以整栏收起；窄屏左栏是抽屉，drawer 是开着没有
  const mobile = useMedia(MOBILE);
  const [sideOff, setSideOff] = useState(sideOffSaved);
  const [drawer, setDrawer] = useState(false);
  const navFocus = useRef(null); // 收起、展开、关抽屉以后焦点去哪个按钮（按下的那个按钮没了）
  const undo = useUndo(); // 删了能撤销（见 undo.jsx）：对话、记忆共用一条提示
  // 右栏开着的模型写的文件（见 artifact.jsx）：{call}（正在写的那次调用）或 {series, id}（某份文件的某一版）；换对话就关上。
  // 用户看着这一轮生成时，头一次收到某次写文件就自动打开（onEvent 里），判断记在这里，不跟着卡片、右栏的组件换来换去丢掉（Fusion 踩过）；
  // 同一次调用只自动开一次，用户关了就不再开。手机上不自动开：全屏会盖住对话，卡片上看得到在写，点开再看
  const [file, setFile] = useState(null);
  const [fileFocus, setFileFocus] = useState(0); // 用户点开的每次加一：光标挪进右栏（自动打开的不抢光标）
  const autoOpened = useRef(new Set());
  const fileOpener = useRef(null); // 点开右栏的那个按钮：关上以后光标回去
  const mobileRef = useRef(mobile);
  mobileRef.current = mobile;
  // 点开文件、在右栏里换着看（文件列表点开一个、看完回列表）以后光标去哪（2026-11-09 Sean 要的）：分栏时回输入框，看着文件接着说；
  // 右栏盖在对话上（窄桌面、手机）时挪到右栏的标题上，键盘、读屏接着从这里读
  const focusAfterOpen = () => {
    if (matchMedia(SPLIT).matches) {
      setFileFocus(0); // 右栏换成别的（列表、文件）挂上时不拿以前的数去挪光标
      if (!pageRef.current) focusInput(true); // 资料库页没有输入框：光标留在点的那一行
    } else setFileFocus((n) => n + 1);
  };
  const openFile = (target) => {
    fileOpener.current = opener();
    setFile(target);
    focusAfterOpen();
  };
  // 收起右栏（×、Esc、再点一下“文件 N”）：桌面上光标回输入框；手机上回到点开它的那个按钮（见 typeNext）
  const closeFile = () => {
    setFile(null);
    const back = fileOpener.current;
    fileOpener.current = null;
    if (pageRef.current) back?.isConnected && back.focus({ preventScroll: true }); // 资料库页：回到点开它的那一行
    else typeNext(back?.isConnected ? back : null);
  };
  const navFile = (target) => {
    setFile(target);
    focusAfterOpen();
  };
  // 顶栏的“文件 N”：开着文件列表时再点收起，开着别的换成列表
  const toggleFiles = () => (file?.list ? closeFile() : file ? navFile({ list: true }) : openFile({ list: true }));
  const [gone, setGone] = useState(() => new Set()); // 点了删除、还在等撤销的对话：列表里不显示（隔几秒取回的列表里还有它）

  // 定时任务跑完的未读和通知（2026-11-13）：列表每行带 unread（几条没看）和 notice（最近那条：任务名、回答的开头、跑成没有、什么时候）。
  // 页面开着时新到的在右上角弹出来（notices，见 notice.jsx）；人没在看页面时标题、图标提醒，开了通知的发系统通知（见 alert.jsx 的 useTaskAlert）。
  // noticeAt 记着每段对话见过的最近一条的时间：头一次取到列表只记不弹（刷新页面不补弹旧的），之后时间更新的才算新到（晚到的旧列表不会再弹一遍）
  const [notices, setNotices] = useState([]);
  const noticeAt = useRef(null);
  const [taskMark, raiseTask] = useTaskAlert();
  const opening = useRef(0); // 有几次“打开对话”还在取状态（见 restore）：这期间列表说它在跑，也不另外去接
  const idleAt = useRef(0); // 开着的对话最近一次停下来是什么时候：在那之前发出去的列表请求说它在跑的不算（晚到的旧列表）

  const setBusy = (b) => {
    if (busyRef.current && !b) idleAt.current = Date.now();
    busyRef.current = b;
    setBusyState(b);
  };

  // 取回来的列表：除了显示，还看两件事——有没有新到的通知；开着的这段对话是不是在页面不知道的时候跑起来了
  function takeList({ threads: list, more: rest }, sent) {
    setThreads((cur) => (cur && sameList(cur, list) ? cur : list));
    setMore(rest);
    listIds.current = new Set(list.map((t) => t.thread_id));
    const seen = noticeAt.current;
    const next = { ...seen };
    for (const t of list) {
      const at = t.notice?.at;
      if (!at || at <= (next[t.thread_id] || "")) continue;
      next[t.thread_id] = at;
      if (!seen || !t.unread) continue;
      const n = { key: `${t.thread_id}@${at}`, thread_id: t.thread_id, title: t.notice.title || t.title, text: noticeLine(t.notice), ok: t.notice.ok };
      const open = t.thread_id === tidRef.current;
      if (open) freshen(t.thread_id);
      // 正看着的那段对话不弹：结果就在眼前。同一段对话只留最新的一条
      if (!open || pageRef.current) setNotices((ns) => [n, ...ns.filter((x) => x.thread_id !== n.thread_id)].slice(0, NOTICES_MAX));
      raiseTask(n, (id) => select(id, "push", false, true));
    }
    noticeAt.current = next;
    const id = tidRef.current;
    if (id && !busyRef.current && !opening.current && sent > idleAt.current && list.some((t) => t.thread_id === id && ACTIVE.has(t.status))) adopt(id);
  }
  // 取到的那份（没取到、被更晚发的那次抢先了是 null）
  const refreshList = () => {
    const sent = Date.now();
    const seq = (listSeq.current += 1);
    return listResearchThreads({ limit: listLimit.current, current: tidRef.current }).then((got) => {
      if (seq < listTaken.current) return null;
      listTaken.current = seq;
      takeList(got, sent);
      return got;
    }, () => null);
  };
  // 左栏滚到底了：多取一页。多出来的那一页全是本来就带着的（置顶的、有未读的）时接着取，直到多出几行、或者没有了
  const loadMore = async () => {
    if (growing.current) return;
    growing.current = true;
    try {
      for (;;) {
        const had = listIds.current.size;
        listLimit.current += PAGE;
        const got = await refreshList();
        if (!got?.more || got.threads.length > had) break;
      }
    } finally {
      growing.current = false;
    }
  };
  const anyActive = busy || (threads || []).some((t) => ACTIVE.has(t.status));
  const now = useNow(anyActive);

  useEffect(() => {
    refreshList();
    navigate(pageRef.current ? PAGES[pageRef.current] : pathOf(tid), false);
    if (tid) restore(tid, gen.current);
    // 浏览器前进后退、手动改了地址：地址栏已经是了，只换页面（去海报页时这个页面会被换掉，不用管）
    const onPop = () => {
      if (location.hash === "#poster") return;
      const p = oldPage();
      if (p) { // 后退到以前的设置页、记忆页地址：弹出窗口，对话不动
        setSettings(p);
        return navigate(pathOf(tidRef.current), false);
      }
      const at = routePage();
      if (at) return openPage(at, null);
      const id = routeId();
      if (pageRef.current || id !== tidRef.current) select(id, "none", routeWork());
      else if (!id) setNewWork(routeWork()); // 在新页面上前进后退：只是“对话 / 工作”换了
    };
    addEventListener("popstate", onPop);
    return () => { // 切去别的页：断开连接，旧的回调不再改页面
      removeEventListener("popstate", onPop);
      ctl.current.abort();
      ctl.current = new AbortController();
      gen.current += 1;
      clearTimeout(refetchTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const t = setInterval(refreshList, anyActive ? 4000 : 20000);
    return () => clearInterval(t);
    // refreshList 里用到的（takeList、select、restore……）都认 ref，不认哪一次渲染
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyActive]);
  const title = live?.title || st?.title; // 执行中起好的标题跟着事件来，库里一轮跑完才有（之前是用户的原话）
  const done = useDoneAlert(tid, busy || ACTIVE.has(st?.status), st, title); // 用户没在看时答完了（或停下来等回答）：标题前加“（答完了）”，见 alert.jsx
  useEffect(() => {
    const base = page ? `${PAGE_TITLES[page]} · 灵稿` : title ? `${title} · 灵稿` : tid ? "灵稿 · 对话" : newWork ? "灵稿 · 新工作任务" : "灵稿 · 新对话";
    const mark = done || taskMark; // 两样都有时说开着的这段对话的
    document.title = (mark ? `（${mark}）` : "") + base;
  }, [title, tid, done, taskMark, page, newWork]);
  // 开着的这段对话有未读、人正看着（页面在前台，没在定时任务页、资料库页上）：标成已读，左栏的数字消掉。人不在时留着，回来再标
  const openUnread = (!page && tid && threads?.find((t) => t.thread_id === tid)?.unread) || 0;
  useEffect(() => {
    if (!openUnread) return undefined;
    const read = () => {
      if (away()) return;
      setThreads((ts) => ts && ts.map((t) => (t.thread_id === tid ? { ...t, unread: 0, notice: null } : t)));
      markThreadRead(tid).catch(() => {}); // 没标上：下次取回的列表里数字还在，到时再标
    };
    read();
    document.addEventListener("visibilitychange", read);
    addEventListener("focus", read);
    return () => {
      document.removeEventListener("visibilitychange", read);
      removeEventListener("focus", read);
    };
  }, [openUnread, tid, page]);
  // 打开了哪段对话，它的通知就不用留着了
  useEffect(() => {
    if (tid && !page) setNotices((ns) => (ns.some((n) => n.thread_id === tid) ? ns.filter((n) => n.thread_id !== tid) : ns));
  }, [tid, page]);

  // 深色（批次 G 第 16 条；设置窗口里能选跟随系统、浅色、深色）：深色时浏览器顶栏跟着深；只在对话页加，海报页还是浅色（index.html 里那条白的）
  useEffect(() => {
    if (!dark) return undefined;
    const m = Object.assign(document.createElement("meta"), { name: "theme-color", content: "#1b1f24" });
    document.head.prepend(m); // 好几条都对得上时浏览器用最前面那条
    return () => m.remove();
  }, [dark]);
  useEffect(() => { loadModels().catch(() => setModels([])); }, []);
  // 资料库页、定时任务页、设置、搜对话是用到才取的（见 parts.jsx）：页面空下来先取好，点开时不用等
  useEffect(() => {
    const idle = window.requestIdleCallback ? requestIdleCallback(warm, { timeout: 4000 }) : setTimeout(warm, 2000); // Safari 没有 requestIdleCallback
    return () => (window.cancelIdleCallback ? cancelIdleCallback(idle) : clearTimeout(idle));
  }, []);
  // 身份定了取一次额度；每轮结束（busy 回到 false）再取：这一轮花的已经记上了
  useEffect(() => { if (auth.ready && !busy) refreshCredits(); }, [auth.ready, busy]);
  // 额度到点重置：最早的那个重置时间过了一两秒再取一次，左栏圆环和额度面板马上变成新的（原来要刷新页面才变）。
  // 标签页在后台时浏览器会拖着定时器，切回来时已经过了点也补取一次；这里的钟比服务器快、取回来还没重置的，隔 5 秒再取
  useEffect(() => {
    const ats = (credits?.windows || []).map((w) => Date.parse(w.resets_at)).filter((t) => !Number.isNaN(t));
    if (!ats.length) return undefined;
    const at = Math.min(...ats);
    const wait = at - Date.now();
    const timer = setTimeout(refreshCredits, wait > 0 ? Math.min(wait + 1500, 2 ** 31 - 1) : 5000);
    const back = () => { if (!document.hidden && Date.now() >= at) refreshCredits(); };
    document.addEventListener("visibilitychange", back);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", back);
    };
  }, [credits]);
  // 领不到游客身份（同一网络今天领得太多，2026-11-06 Sean 定每网络每天 5 个）：弹登录框，说清为什么、登录后会怎样（第 26 条）；
  // 没有登录功能的部署只能说明天再来
  useEffect(() => {
    if (!auth.crowded) return;
    if (auth.login) setLoginOpen(true);
    else setError(`你所在的网络今天新来的游客太多了（防刷，同一网络每天最多 ${auth.crowded.per_ip || 5} 个游客身份），明天再来就能接着用。`);
  }, [auth.crowded, auth.login]);
  // 点了锁着的模型去登录的：登录回来（整页重载过）直接选好它
  useEffect(() => {
    if (!models?.length || !auth.me) return;
    const id = takeWant();
    if (id && models.some((m) => m.id === id && !m.locked)) setPick(id);
  }, [models, auth.me]);
  const picked = models?.some((m) => m.id === pick && !m.locked) ? pick : models?.[0]?.id; // 选的不在列表里了（刚下架）、锁上了也按 Auto
  // 输入框旁边显示的模型：新对话是选的；开始的对话是它定下的（状态还没取回来时，刚发出去的按选的，打开的老对话先不显示）
  const current = !tid ? picked : st?.thread_id === tid && st.model ? st.model : live ? picked : null;
  // 这一页是对话还是工作：新页面按选着的；开始了的按对话自己的（状态里的 work），状态还没取回来先按列表里那一行、刚发起的按发的时候选的
  const work = !tid ? newWork : st?.thread_id === tid ? !!st.work
    : threads?.find((t) => t.thread_id === tid)?.work ?? (startedAs.current?.id === tid && startedAs.current.work);
  // 这句话带不带思考开关：按输入框旁边显示的那个模型（能开关才带，不带的按模型的默认）；工作任务不带（一直开）
  const thinkOpts = () => (!work && models?.find((m) => m.id === current)?.thinking === "toggle" ? { thinking } : {});
  // 新页面上换“对话 / 工作”：地址跟着换（不进浏览器历史），写着的字、选的模型都不动
  const pickWork = (v) => {
    setNewWork(v);
    if (!pageRef.current && !tidRef.current) navigate(newPath(v), false);
  };

  // ---- 切换线程 ----

  // how：push 记进浏览器历史（点左栏的对话、新对话）；replace 换掉当前这条（当前对话删了、打不开）；none 地址栏已经是了。
  // asWork：开的是新工作任务（id 是 null 时才看；左栏的“新工作任务”、地址是 /work/new），不然是新对话。
  // 在资料库、定时任务页时：回到对话那一栏（右栏看的资料库文件关上）；点的就是刚才开着的那个对话（包括还没发的新对话），原样放回来。
  // fresh：就算是刚才开着的那个也整个重新来（定时任务页点了运行，那段对话多了一轮在跑；那一页底下说了一句话，要开一段新的）
  const select = (id, how = "push", asWork = false, fresh = false) => {
    if (!id) setNewWork(asWork);
    const path = id ? chatPath(id) : newPath(asWork);
    if (pageRef.current) {
      setPage(null);
      setFile(null);
      if (id === tidRef.current && !fresh) return how !== "none" && navigate(path, how === "push");
    }
    if (id === tidRef.current && id && !fresh) return;
    if (!id) thinkSet.current.delete("new"); // 新对话：深度思考回到默认
    setThinkingState(thinkSet.current.get(id ?? "new") ?? thinkOnRef.current);
    ctl.current.abort();
    ctl.current = new AbortController();
    gen.current += 1;
    tidRef.current = id;
    lastEvent.current = null;
    clearTimeout(refetchTimer.current);
    refetchAt.current = 0;
    setTid(id);
    setSt(null);
    setLive(null);
    setSuggesting(false);
    setError(null);
    setBusy(false);
    setFile(null);
    autoOpened.current.clear();
    if (how !== "none") navigate(path, how === "push");
    if (id) restore(id, gen.current);
    if (id && !listIds.current.has(id)) refreshList(); // 开的是更早的、左栏还没取到的那段：取一次，列表会带上开着的这段
  };

  // 设置里改了深度思考的默认：之后的新对话按它；正开着一个还没发、也没手动切过的新对话，这个也跟着变（关了窗口看到的就是新默认）
  const changeThinkDefault = (v) => {
    saveThinkDefault(v);
    setThinkOn(v);
    if (tidRef.current === null && !busyRef.current && !thinkSet.current.has("new")) setThinkingState(v);
  };

  // 打开一个线程：取状态；服务器上还在跑，就接上这次执行的事件流（从这次执行的头一个事件补发）。
  // quiet：不是用户打开的（见 adopt），取不到就算了，不报错
  async function restore(id, g, quiet = false) {
    try {
      let s;
      opening.current += 1;
      try {
        s = await getResearchState(id);
      } finally {
        opening.current -= 1; // 和下面接上去（follow 里记成在跑）之间没有空当：列表这时回来不会再接一次
      }
      if (g !== gen.current) return;
      setSt(s);
      if (ACTIVE.has(s.status)) {
        setLive({ items: [], turnsAtStart: s.turns.length, stage: s.status === "queued" ? QUEUED : null });
        await follow(id, g, []);
      } else {
        takeBack(id, leftover(s)); // 没在跑却还排着话：上次那一轮出错了、或服务重启时没跑完
        if (s.suggesting) awaitSuggestions(id, g);
      }
    } catch (e) {
      if (g !== gen.current || quiet) return;
      if (e.status === 404) select(null, "replace"); // 线程确实不在了才换成新对话；网络抖动时地址留着，刷新还能再试
      setError(errorText("没能打开这个对话", e, e.status === 404 ? null : "刷新页面再试一次"));
    }
  }

  // 开着的这段对话没在跑，服务器上却跑起来了（定时任务到点了，或者在别的页面、别的设备上发的；见 takeList）：接上去，边跑边出。
  // 没接上的话下次取列表再来
  function adopt(id) {
    ctl.current.abort(); // 上一轮的连接可能还开着在等推荐问题：不要了
    ctl.current = new AbortController();
    lastEvent.current = null;
    setSuggesting(false);
    restore(id, gen.current, true);
  }

  // 开着的这段对话来了通知而页面可能没跟上（定时任务那一轮没来得及接上就跑完了）：取一次状态。
  // 正在跑、正在接的不动：它们结束时以最终状态为准
  function freshen(id) {
    const g = gen.current;
    if (busyRef.current || opening.current) return;
    getResearchState(id).then((s) => {
      if (g === gen.current && id === tidRef.current && !busyRef.current && !ACTIVE.has(s.status)) setSt(s);
    }).catch(() => {});
  }

  // 打开时这一轮已经结束、推荐问题还在起（刷新、切回来）：接上事件流等着，服务器从现在接着发（不从头补发整轮），起好了会再来一次状态。
  // 流结束了再取一次状态为准（起好的那次状态正好在接上之前发出去了、或者没起成）；又说了话（连接被换掉）就不等了
  async function awaitSuggestions(id, g) {
    setSuggesting(true);
    const sig = ctl.current.signal;
    const show = (s) => { if (g === gen.current && !sig.aborted && !ACTIVE.has(s.status)) setSt(s); };
    try {
      await followResearchThread(id, null, (ev, data) => { if (ev === "state") show(data); }, sig);
      show(await getResearchState(id));
    } catch { /* 下次打开再看 */ }
    if (g === gen.current && !sig.aborted) setSuggesting(false);
  }

  // 接上服务器上这次执行：补发断线期间漏掉的事件，再接着收；连不上就隔一会儿重连。
  // r 是这次执行在页面上的记号（见 run）；从打开线程接上的，用当前连接新开一个
  async function follow(id, g, errs, r = { done: false, sig: ctl.current.signal }) {
    setBusy(true);
    for (let fails = 0; ;) {
      try {
        await followResearchThread(id, lastEvent.current, onEvent(g, errs, r), r.sig);
        if (g !== gen.current) return;
        if (r.done) return setSuggesting(false); // 流收完了：推荐问题要么已经到了，要么不会来了
        fails = 0; // 连得上，只是流又断了、或者执行刚开始：接着读
      } catch (e) {
        if (e.aborted || g !== gen.current) return;
        if (r.done) return setSuggesting(false); // 等推荐问题时断了：不再接，下次打开线程能看到
        if (e.status === 404 || ++fails > 30) { // 线程不在了，或者几分钟都连不上；后端重启时代理回的 5xx 也按断线重连
          setError(errorText("没能取回这一轮的进度", e, e.status === 404 ? null : "刷新页面看看这一轮现在的样子"));
          setBusy(false);
          setLive(null);
          return;
        }
        setLive((l) => ({ items: [], ...l, stage: "连接断了，正在重连…" }));
      }
      await sleep(fails ? Math.min(1000 * 2 ** (fails - 1), 10000) : 500);
      if (r.sig.aborted) return;
    }
  }

  // 这一轮结束了：以这时的状态为准（推荐问题起好了会再来一次）。执行中出过错但线程没算失败，把错误告诉用户
  function finish(g, s, errs) {
    if (g !== gen.current) return;
    clearTimeout(refetchTimer.current);
    refetchAt.current = 0;
    setSt(s);
    setBusy(false);
    setLive(null);
    setSuggesting(false);
    if (errs.length && s.status !== "failed") setError(AFTER_RUN_ERROR); // 没算失败的出错：答完以后收尾时（见 errors.js）
    takeBack(s.thread_id, leftover(s)); // 这一轮出错，排着的话没发
    refreshList();
  }

  // 排着的话取回输入框：撤回的，或者这一轮出错没发的。服务器上取一次就没了：已经发出去了就什么也不放。
  // kinds 是取哪几种：queued（排着、答完就发的）、guided（等着插进这一轮的）；都取时等着插的在前，合成一句放回去
  async function takeBack(id, kinds = ["queued"]) {
    if (!kinds.length) return;
    try {
      const texts = [];
      for (const k of ["guided", "queued"].filter((k) => kinds.includes(k))) {
        texts.push(await (k === "guided" ? unguideResearch : unqueueResearch)(id));
        if (id === tidRef.current) setSt((s) => (s ? { ...s, [`${k}_message`]: null } : s));
      }
      const text = texts.filter(Boolean).join("\n");
      if (text) setPutBack({ thread: id, text });
    } catch (e) {
      if (id === tidRef.current) setError(errorText("排着的话没能取回输入框", e, "刷新页面看看它还在不在"));
    }
  }

  // 执行中重新取一次状态：用户的话存下来以后、开始跑的时候（开始的时间，计时用）。这一轮的内容一轮跑完才存，执行中都跟着事件来。
  // 几个事件挨着来时只取一次，最早要取的那次为准
  function refetch(g, ms) {
    const due = Date.now() + ms;
    if (refetchAt.current && refetchAt.current <= due) return;
    clearTimeout(refetchTimer.current);
    refetchAt.current = due;
    refetchTimer.current = setTimeout(async () => {
      refetchAt.current = 0;
      try {
        const s = await getResearchState(tidRef.current);
        if (g === gen.current && busyRef.current) setSt(s); // 执行已经结束的话，以 state 事件给的最终状态为准
      } catch { /* 下一个事件再取 */ }
    }, ms);
  }

  const onEvent = (g, errs, r) => (ev, data, id) => {
    if (g !== gen.current || r.sig.aborted) return;
    if (id) {
      lastEvent.current = id;
      const run = id.slice(0, id.lastIndexOf("-")); // 事件 id 是“执行-序号”
      if (r.run && r.run !== run) { // 接到了下一次执行（排着的话发出去了）：上一次的实时内容不要了
        setLive({ items: [], stage: "正在准备…" });
        errs.length = 0;
      }
      r.run = run;
    }
    if (ev === "thread") {
      if (tidRef.current !== data.thread_id) {
        tidRef.current = data.thread_id;
        setTid(data.thread_id);
        if (thinkSet.current.has("new")) { // 新对话里切过深度思考：有了 id 以后记在这段对话名下
          thinkSet.current.set(data.thread_id, thinkSet.current.get("new"));
          thinkSet.current.delete("new");
        }
        startedAs.current = { id: data.thread_id, work: newWorkRef.current };
        if (!pageRef.current) navigate(chatPath(data.thread_id), false); // 新对话有了 id：换掉 /chat/new，后退不回到空白的新对话（去了资料库的不动地址）
      }
      setLive((l) => (l ? { ...l, stage: data.queued ? QUEUED : l.stage } : l));
      refreshList();
      refetch(g, 0); // 用户的话已经存下来了
    } else if (ev === "started") {
      setLive((l) => (l ? { ...l, stage: null } : l));
      refreshList();
      refetch(g, 0);
    } else if (ev === "title") {
      setLive((l) => (l ? { ...l, title: data.title } : l));
      refreshList();
    } else if (ev === "delta" || ev === "step" || ev === "restart" || ev === "model" || ev === "ask" || ev === "writing") {
      setLive((l) => addLive(l && { ...l, stage: null }, ev, data));
    } else if (ev === "draft") { // 模型正在写文件：攒着（见 addLive），头一次收到这次调用时在右栏打开（见上面 file）
      setLive((l) => addLive(l && { ...l, stage: null }, ev, data));
      if (data.name && !autoOpened.current.has(data.id)) {
        autoOpened.current.add(data.id);
        if (!mobileRef.current) {
          setFile({ call: data.id });
          setFileFocus(0); // 不抢光标：用户可能正在输入框里打字
        }
      }
    } else if (ev === "guide") { // 等着插的话放进这一轮了：从下面挪进过程里。之后又插了一句的话以服务器为准
      setLive((l) => addLive(l, ev, data));
      setSt((s) => (s?.guided_message?.text === data.text ? { ...s, guided_message: null } : s));
      refetch(g, 0);
    } else if (ev === "phase") {
      if (data.phase === "suggest") setSuggesting(true); // 这一轮已经结束，live 已经清掉了
      else setLive((l) => addLive(l && { ...l, stage: null }, ev, data));
    } else if (ev === "state") {
      // 这一轮结束了就按结束显示，不等连接关上：推荐问题起好了还会再来一次状态
      if (!ACTIVE.has(data.status)) {
        r.done = true;
        finish(g, data, errs);
      } else { // 这次执行结束时线程又在跑了：排着的话已经发成新的一轮。先按这时的状态显示，这条流结束后接着读下一次执行（见 follow）
        clearTimeout(refetchTimer.current);
        refetchAt.current = 0;
        setSt(data);
        setLive({ items: [], stage: data.status === "queued" ? QUEUED : "正在准备…" });
        refreshList();
      }
    } else if (ev === "error") {
      errs.push(data.message);
    }
  };

  // 跑一次（新建、接着说、重新回答、改最后一句或重试）：边收事件边显示，停下来以后以最终状态为准。
  // said：用户这次说的话，存下来之前先在对话里显示；extra：这次执行的其他显示信息（这句话带的图、重新回答时要藏起来的旧条目、改过的那句话）
  async function run(fn, said, extra = {}) {
    const g = gen.current;
    const before = st?.turns?.length || 0; // 发之前对话里有几轮：服务出错时用来看这句话存没存下
    ctl.current.abort(); // 上一轮的连接可能还开着在等推荐问题：又说了话，那些推荐问题不要了
    ctl.current = new AbortController();
    const r = { done: false, sig: ctl.current.signal }; // done：收到了这一轮结束的状态，之后连接断了也不用再接
    setBusy(true);
    setError(null);
    setSuggesting(false);
    setLive({ items: [], said, sentAt: said || extra.edit ? new Date().toISOString() : undefined, turnsAtStart: st?.turns?.length || 0, stage: "正在准备…", ...extra });
    const errs = [];
    try {
      await fn(onEvent(g, errs, r), r.sig);
      if (g !== gen.current) return;
      if (r.done) return setSuggesting(false); // 流收完了：推荐问题要么已经到了，要么不会来了
      if (tidRef.current) return await follow(tidRef.current, g, errs, r); // 流没收完就断了
      setBusy(false);
    } catch (e) {
      if (e.aborted || g !== gen.current) return;
      if (r.done) return setSuggesting(false); // 等推荐问题时断了：不再接，下次打开线程能看到
      const images = extra.images?.length ? extra.images : undefined;
      const docs = extra.docs?.length ? extra.docs : undefined;
      const back = images && docs ? "话、图和文件都放回输入框了" : docs ? "话和文件都放回输入框了" : "话和图都放回输入框了";
      // 带的图没收（超过 4 MB、格式不对、传不上去，见后端 api.main 的 _upload）：这句话没发出去，对话里什么都没多，话和图都放回输入框
      if (images && [413, 415, 502].includes(e.status)) {
        setPutBack({ thread: tidRef.current || "new", text: said, images, docs, rejected: true });
        setError(`${uploadErrorText(e.status)}，${back}`);
        setBusy(false);
        setLive(null);
        return;
      }
      // 带的文件不能发（还在解析、读不了、过期清掉了，见后端 files.service 的 NotSendable）：同上，话和文件放回输入框，读不了、过期的在那里标出来
      if (docs && e.status === 400 && e.detail?.code === "files_not_sendable") {
        setPutBack({ thread: tidRef.current || "new", text: said, images, docs, rejected: true });
        setError(`这句话没发出去：${notSendableText(e.detail.reason)}，${back}。`);
        setBusy(false);
        setLive(null);
        return;
      }
      // 额度用完了（429）：话放回输入框，提示里带上什么时候重置，游客还给登录入口；服务器上没有执行要去接
      if (e.status === 429 && e.detail?.code === "credits_exhausted") {
        const text = said ?? extra.edit;
        if (text) setPutBack({ thread: tidRef.current || "new", text, images, docs, rejected: true });
        setError({ text: quotaText(e.detail) + (text ? "刚才那句已经放回输入框。" : ""), login: !!e.detail.guest, quota: true });
        refreshCredits();
        setBusy(false);
        setLive(null);
        return;
      }
      // 新对话被拒（比如选的模型刚下架）：话放回输入框，重新取能选的模型
      if (!tidRef.current && e.status === 400 && said) {
        setPutBack({ thread: "new", text: said, images, docs, rejected: true });
        setError("这句话没发出去，已经放回输入框：选的模型现在用不了，换一个再发。");
        loadModels().catch(() => {});
        setBusy(false);
        setLive(null);
        return;
      }
      // 服务出错（5xx）：话（和图）多半没存下。先取一次状态：存下了（多了一轮、改的那句已经换上、正在跑）就按服务器的接着显示；
      // 没存下就和图被拒一样，话和图都放回输入框（待确认第 19 条）
      const text = said ?? extra.edit;
      if (e.status >= 500 && text) {
        const s = tidRef.current ? await getResearchState(tidRef.current).catch(() => null) : null;
        if (g !== gen.current) return;
        const kept = s && (ACTIVE.has(s.status) || s.turns.length > before || (extra.edit != null && s.turns.at(-1)?.user === extra.edit));
        if (kept) {
          setSt(s);
          if (ACTIVE.has(s.status)) {
            setLive((l) => ({ items: [], ...l, said: null, edit: undefined, sentAt: undefined, stage: "服务器还在处理，等它完成" }));
            return await follow(tidRef.current, g, errs, r);
          }
        } else {
          setPutBack({ thread: tidRef.current || "new", text, images, docs, rejected: true });
          setError(errorText(images || docs ? `这句话没发出去，${back}` : "这句话没发出去，已经放回输入框", e));
        }
        setBusy(false);
        setLive(null);
        return;
      }
      // 话被拒（400）、正在跑或上一轮没跑完（409）、连接断了（没有状态码）——都回服务器接上当前进度
      const recoverable = tidRef.current && (!e.status || e.status === 400 || e.status === 409);
      // 被拒的话（说的、改的）服务器没存下来：不再显示在对话里，放回输入框，和排队没排上一样。
      // 回答问题时写的话（extra.answer）没交上也放回去，服务出错（5xx）时一样
      const lost = e.status ? (recoverable ? said ?? extra.edit ?? extra.answer : extra.answer) || null : null;
      if (lost) setPutBack({ thread: tidRef.current, text: lost, images, docs });
      if (e.status || !recoverable) {
        const what = lost ? (extra.answer ? "回答没交上，已经放回输入框" : "这句话没发出去，已经放回输入框") : extra.what || (said ? "这句话没发出去" : "这一步没做成");
        // 回答时的 409：这个问题在别处答过了，或者这一轮已经停了；下面接上服务器，页面跟着变成现在的样子
        if (!(extra.quiet && e.status === 409)) setError(extra.asked && e.status === 409 ? `${what}：这个问题已经不用回答了。` : errorText(what, e));
      }
      if (recoverable) {
        const rejected = e.status ? { said: null, edit: undefined, sentAt: undefined } : {};
        setLive((l) => ({ items: [], ...l, ...rejected, stage: e.status ? "服务器还在处理，等它完成" : "连接断了，正在重连…" }));
        return await follow(tidRef.current, g, errs, r);
      }
      setBusy(false);
      setLive(null);
    }
  }

  // 问着的时候点停止：不回答了，这一轮到此为止（见后端 research_runner._adismiss）。这时没有执行在跑，也没有事件流：
  // 服务器收尾以后这里取一次状态。排着的话这时接着发成新的一轮（取到的状态是在跑），接上那次执行；
  // 409 是这个问题刚好在别处答了、或者这一轮已经停了，同样按取到的状态来
  async function dismiss(id, g) {
    ctl.current.abort();
    ctl.current = new AbortController();
    setBusy(true);
    setError(null);
    setLive({ items: [], stopping: true });
    try {
      await stopResearchThread(id);
    } catch (e) {
      if (g !== gen.current) return;
      if (e.status !== 409) {
        setBusy(false);
        setLive(null);
        setError(errorText("没停下来", e, "再点一次停止"));
        return;
      }
    }
    try {
      const s = await getResearchState(id);
      if (g !== gen.current) return;
      if (!ACTIVE.has(s.status)) return finish(g, s, []);
      lastEvent.current = null; // 新的一次执行：从它的头一个事件收
      setSt(s);
      setLive({ items: [], turnsAtStart: s.turns.length, stage: s.status === "queued" ? QUEUED : null });
      await follow(id, g, []);
    } catch (e) {
      if (g !== gen.current) return;
      setBusy(false);
      setLive(null);
      setError(errorText("没能取回这一轮现在的样子", e, "刷新页面看看"));
    }
  }

  // 这句话带的图（File）和文件（编号）；没带文件不写 files
  const attached = (images, docs) => ({ images: images.map((i) => i.file), ...(docs.length ? { files: docs.map((d) => d.id) } : {}) });
  // act、manage 交给每一轮、输入框、左栏，它们记着渲染结果（memo）：这里给的是不变的一份，调到的总是这一次渲染的（见 stable.js）
  const act = useLatest({
    // 新对话带上选的模型（没取到列表就不带，按 Auto）、是不是工作任务（新页面上选着“工作”，定下就不变）；
    // 之后每句话带上深度思考开关（对话定下的模型能开关才带）。
    // images：这句话带的图（[{key, file, url}]，见 Composer）；docs：带的文件（解析好了的，见 composer.jsx 的 picked），发编号。排着的话都不带
    start: (text, images = [], docs = []) => {
      const opts = { model: picked, ...thinkOpts(), ...(newWorkRef.current ? { work: true } : {}), ...attached(images, docs) };
      return run((cb, sig) => createResearchThread(text, opts, cb, sig), text, { images, docs });
    },
    say: (text, images = [], docs = []) => {
      const id = tidRef.current;
      const opts = { ...thinkOpts(), ...attached(images, docs) };
      return run((cb, sig) => sayResearch(id, text, opts, cb, sig), text, { images, docs });
    },
    retry: () => run((cb, sig) => retryResearchThread(tidRef.current, cb, sig), null, { what: "没能重试" }),
    // 回答模型停下来问的问题（st.ask），或者跳过（skip）：同一轮接着跑。回答先按页面手上的放进过程里，服务器的那条到了换掉它。
    // mute 是“别再问了”：这个问题跳过，这段对话之后也不再出问题卡（st.asks_off，恢复见 manage.askAgain）。
    // typed：回答是输入框里写的，没交上时放回输入框（点选项的不放）
    // 等着的是要位置的（ask.kind 是 location，见 timeline.jsx 的 LocateCard）：location 是浏览器给的位置，unavailable 是取不到，skip 是不给，
    // text 是没给位置、直接说了个地方（这一条先放进过程里）。auto：不是用户点的（页面自己取了交的）——别处已经交过了（409）就不提示
    answer: ({ text = "", skip = false, mute = false, typed = false, location = null, unavailable = false, auto = false } = {}) => {
      const id = tidRef.current;
      const ask = st?.ask;
      if (!id || !ask) return undefined;
      if (ask.kind === LOCATION) {
        const said = !location && text ? text : "";
        const body = location ? { id: ask.id, location } : said ? { id: ask.id, text: said } : unavailable ? { id: ask.id, unavailable: true } : { id: ask.id, skip: true };
        const mine = said ? [{ type: "ask", kind: LOCATION, id: `${ask.call_id}:where`, question: "", options: [], pending: false, answer: said, skipped: false, muted: false }] : [];
        return run((cb, sig) => answerResearchThread(id, body, cb, sig), null,
          { items: mine, asked: true, quiet: auto, what: said ? "回答没交上" : "位置没交上", answer: typed && said ? said : undefined });
      }
      const skipped = skip || mute;
      const mine = { type: "ask", id: ask.call_id, question: ask.question, options: ask.options, pending: false, answer: skipped ? "" : text, skipped, muted: mute };
      const body = mute ? { id: ask.id, mute: true } : skip ? { id: ask.id, skip: true } : { id: ask.id, text };
      return run((cb, sig) => answerResearchThread(id, body, cb, sig), null,
        { items: [mine], asked: true, what: "回答没交上", answer: typed && !skipped ? text : undefined });
    },
    // 最后一轮的旧回答先藏起来，新回答在同一轮里接着出现
    regenerate: () => {
      const hidden = new Set((st?.turns?.at(-1)?.items || []).map(itemKey));
      const opts = thinkOpts();
      return run((cb, sig) => regenerateResearchThread(tidRef.current, opts, cb, sig), null, { hidden, what: "没能重新回答" });
    },
    // 改最后一句：同样藏起旧回答，改过的话先显示出来
    edit: (text) => {
      const hidden = new Set((st?.turns?.at(-1)?.items || []).map(itemKey));
      const opts = thinkOpts();
      return run((cb, sig) => editResearchThread(tidRef.current, text, opts, cb, sig), null, { hidden, edit: text });
    },
    // 正在回答时又说的话：排在服务器上，这一轮答完就发（又说的接在后面，合成一句）。
    // 话到的时候这一轮刚好答完了，服务器直接发了出去：接上那次执行（流还连着的那次执行可能已经以空闲收尾，不能指望它）
    queue: async (text) => {
      const id = tidRef.current;
      const g = gen.current;
      try {
        const q = await queueResearch(id, text, thinkOpts());
        if (g !== gen.current) return;
        if (q) setSt((s) => (s ? { ...s, queued_message: q } : s));
        else run((cb, sig) => followResearchThread(id, null, cb, sig), null);
      } catch (e) {
        if (g !== gen.current) return;
        setPutBack({ thread: id, text, rejected: true }); // 没排上：话放回输入框
        setError(errorText("这句话没能排上，已经放回输入框", e, "可以再发一次"));
      }
    },
    unqueue: () => takeBack(tidRef.current),
    // 排着的话不等这一轮答完，插进这一轮。服务器说已经没在跑了：排着的话已经发出去了（或这一轮出错，结束时会放回输入框）
    guide: async () => {
      const id = tidRef.current;
      const g = gen.current;
      try {
        const gd = await guideResearch(id);
        if (g === gen.current) setSt((s) => (s ? { ...s, queued_message: null, guided_message: gd } : s));
      } catch (e) {
        if (g !== gen.current) return;
        // 409：这一轮刚好结束了，排着的话已经发出去，或者这一轮出错、放回了输入框
        setError(e.status === 409 ? "没能插进这一轮：这一轮刚好结束了，排着的话会接着发出去，或者放回输入框。" : errorText("没能插进这一轮", e, "它还排着，这一轮答完就发"));
        refetch(g, 0); // 这句话现在是排着还是等着插，以服务器为准
      }
    },
    unguide: () => takeBack(tidRef.current, ["guided"]),
    withdraw: (kinds) => takeBack(tidRef.current, kinds), // 输入框里按 ↑：没发出去的都取回来
    // 只是提出停下：服务器在下一个字或下一步之前停，最终状态照常从这次执行的事件流里来
    stop: async () => {
      const g = gen.current;
      if (!tidRef.current) return; // 新对话刚发出去，服务器还没给线程 id：这时还没开始跑
      if (st?.status === ASKING && !busyRef.current) return dismiss(tidRef.current, g);
      setLive((l) => (l ? { ...l, stopping: true } : l));
      try {
        await stopResearchThread(tidRef.current);
      } catch (e) {
        if (g !== gen.current || e.status === 409) return; // 409：这一轮刚好已经跑完
        setLive((l) => (l ? { ...l, stopping: false } : l));
        setError(errorText("没停下来", e, "再点一次停止"));
      }
    },
  });

  // 左栏、顶栏菜单：置顶、改名、导出、删除。出错就在对话区提示
  const unhide = (id) => setGone((g) => {
    const n = new Set(g);
    n.delete(id);
    return n;
  });
  const manage = useLatest({
    rename: async (id, title) => {
      try {
        const row = await renameResearchThread(id, title);
        setThreads((ts) => ts && ts.map((t) => (t.thread_id === id ? { ...t, ...row } : t)));
        if (id === tidRef.current) setSt((s) => (s ? { ...s, title: row.title } : s));
      } catch (e) {
        setError(errorText("标题没改成", e));
      }
    },
    // 恢复提问：这段对话在问题卡上点过“别再问了”（st.asks_off），改回来，之后 Work 下发的话又会先问
    askAgain: async (id) => {
      try {
        const s = await setResearchAsks(id, true);
        if (id === tidRef.current) setSt((cur) => (cur ? { ...cur, asks_off: s.asks_off } : cur));
      } catch (e) {
        setError(errorText("没恢复成", e));
      }
    },
    exportMd: async (id) => {
      try {
        const s = id === tidRef.current && st ? st : await getResearchState(id);
        saveFile(`${fileName(s.title)}.md`, threadMarkdown(s));
      } catch (e) {
        setError(errorText("没导出成", e));
      }
    },
    // 删除（第 44 条）：不再先确认，马上从列表里拿掉，5 秒内能撤销，过了才真删（见 undo.jsx）。
    // 删的是开着的这个：先换成新对话；撤销时还停在那个新对话上、没动过，就回到它（阅读位置也回来，见 timeline.jsx）
    remove: (id) => {
      const open = id === tidRef.current;
      const name = (open && (live?.title || st?.title)) || threads?.find((t) => t.thread_id === id)?.title || "这段对话";
      setGone((g) => new Set(g).add(id));
      if (open) select(null, "replace");
      undo.push({
        kind: "thread",
        text: `已删除“${name}”`,
        commit: async ({ keepalive }) => {
          const forget = () => {
            setDraft(id, ""); // 对话没了，没发出去的字、看到哪儿了也不留
            forgetReading(id);
          };
          if (keepalive) forget(); // 页面正在关：等不到回来
          try {
            await deleteResearchThread(id, { keepalive });
            forget();
            setThreads((ts) => ts && ts.filter((t) => t.thread_id !== id));
            setAll((ts) => ts && ts.filter((t) => t.thread_id !== id));
          } catch (e) {
            setError(errorText("没删成", e));
          }
          unhide(id);
          refreshList();
        },
        undo: () => {
          unhide(id);
          if (open && tidRef.current === null && !busyRef.current) select(id);
        },
      });
    },
    // 置顶、取消（第 13 条）：存在后端。先改页面，没改成再改回来
    pin: async (id, on) => {
      const was = threads?.find((t) => t.thread_id === id)?.pinned_at ?? null;
      const put = (v) => setThreads((ts) => ts && ts.map((t) => (t.thread_id === id ? { ...t, pinned_at: v } : t)));
      put(on ? was || new Date().toISOString() : null);
      try {
        const row = await pinResearchThread(id, on);
        if (row) setThreads((ts) => ts && ts.map((t) => (t.thread_id === id ? { ...t, ...row } : t)));
      } catch (e) {
        put(was);
        setError(errorText(on ? "没置顶成" : "没取消置顶", e));
      }
    },
  });

  // 执行中这一轮的来源跟着事件来（库里一轮跑完才有），叠在已存的来源上：正文里的引用据此显示成编号
  // 来源没变就还是同一个对象：每来一批字都换一个的话，以前的每一轮都得跟着重新渲染
  const liveSources = live?.sources;
  const shown = useMemo(() => (st && liveSources ? { ...st, sources: { ...st.sources, ...liveSources } } : st), [st, liveSources]);

  // 回答开头要写名字的模型（每轮记下的、正在答的这一轮用的），能选的模型和名字表里都没有的：多半是页面打开以后才加的免费额度，
  // 重新取一次；每个只取一次，取回来还没有的就是真不再提供了，照写“已不再提供的模型”（见 research.js 的 modelName）
  const namesAsked = useRef(new Set());
  const shownRefs = [...(st?.turns || []).map((t) => t.record?.model?.ref), live?.model, st?.model_ref].filter(Boolean).join(" ");
  useEffect(() => {
    if (!models?.length) return;
    const unknown = shownRefs.split(" ").filter((r) => r && !modelNames[r] && !models.slice(1).some((m) => m.model === r) && !namesAsked.current.has(r));
    if (!unknown.length) return;
    unknown.forEach((r) => namesAsked.current.add(r));
    loadModels().catch(() => {});
  }, [shownRefs, models, modelNames]);

  // 游客开登录弹窗（左栏、额度、设置窗口、报错里的登录，点“登录后可用”的模型）。点模型进来的带着那个模型；从按钮直接进来的参数是点击事件，不算
  const askLogin = useEvent((want) => {
    const m = want?.id && want.locked ? want : null;
    setLoginWant(m);
    keepWant(m?.id);
    setLoginOpen(true);
  });
  const onLogin = auth.login && !auth.me ? askLogin : null;
  const closeLogin = () => { // 没登录就关了：不用再记着要哪个模型
    setLoginOpen(false);
    keepWant(null);
  };

  // ---- 导航：收起左栏、抽屉 ----
  // 收起、展开：按下的按钮跟着没了，焦点挪到对面那个按钮上（顶栏的展开钮 / 左栏的收起钮）
  const toggleSide = (e) => {
    if (e?.currentTarget) navFocus.current = sideOff ? "collapse" : "expand";
    setSideOff((off) => {
      saveSideOff(!off);
      return !off;
    });
  };
  const closeDrawer = () => {
    if (document.activeElement?.closest?.("#ws-side")) navFocus.current = "drawer"; // 焦点在抽屉里：关上以后回到抽屉钮
    setDrawer(false);
  };
  const openSearch = () => { setDrawer(false); setSearch(true); };
  // 去资料库、定时任务页（左栏那一行、前进后退）：对话那一栏换成它，右栏开着的这段对话的文件关上。push 是 null 时地址栏已经是了
  const openPage = (name, push = true) => {
    if (pageRef.current === name) return;
    setPage(name);
    setFile(null);
    if (push !== null) navigate(PAGES[name], push);
  };
  // 定时任务页底下说的那一句话：开一段新对话发出去（由模型建任务）。等页面换成新对话的那一拍再发：那时手上才是新对话的状态
  const [asked, setAsked] = useState(null);
  const askNew = (text) => {
    select(null, "push", false, true);
    setAsked({ text });
  };
  useEffect(() => {
    if (!asked) return;
    setAsked(null);
    act.start(asked.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asked]);
  // openKnowledge(f, at)：在右栏看资料库里的文件（f：{id, name?, type?…}），at 是翻到第几段（点回答里引用的资料库来源）
  const navDo = useLatest({ toggleSide, openDrawer: () => setDrawer(true), closeDrawer, newChat: () => select(null), search: openSearch,
    openKnowledge: (f, at = null) => openFile({ kb: f, at }) });
  const unread = (threads || []).reduce((n, t) => n + (gone.has(t.thread_id) ? 0 : t.unread || 0), 0); // 左栏里一共几条未读（见 nav.jsx 的 HeadBar）
  // 从 NavContext 取它的组件到处都是（顶栏、来源角标、弹层……）：这几样没变就是同一个对象，不然每次渲染它们都跟着来一遍
  const nav = useMemo(() => ({ mobile, collapsed: sideOff, drawer, ...navDo, unread }), [mobile, sideOff, drawer, navDo, unread]);
  useEffect(() => {
    const to = navFocus.current;
    navFocus.current = null;
    if (to) document.querySelector(`[data-nav="${to}"]`)?.focus();
  }, [sideOff, drawer]);
  useEffect(() => { if (!mobile) setDrawer(false); }, [mobile]); // 窗口拉宽了：抽屉不再是抽屉
  // 快捷键：桌面上收起、展开左栏，窄屏开、关抽屉；⌘K 打开搜对话的窗口（开着就把框里的字选上）。别的弹窗开着时 ⌘K 不动
  const sideKey = useRef(null);
  sideKey.current = () => (mobile ? (drawer ? closeDrawer() : setDrawer(true)) : toggleSide());
  const searchKey = useRef(null);
  searchKey.current = openSearch;
  useEffect(() => {
    const key = (e) => {
      if (e.repeat) return;
      if (isSearchKey(e)) {
        const open = document.querySelector("[data-find]");
        if (open) {
          e.preventDefault();
          open.focus();
          open.select();
          return;
        }
        if (document.querySelector('[aria-modal="true"]:not(#ws-side)')) return;
        e.preventDefault();
        searchKey.current();
        return;
      }
      if (!isSideKey(e)) return;
      e.preventDefault();
      sideKey.current();
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, []);
  // 在抽屉里点了一条对话、新对话、定时任务、资料库、设置、登录：换过去（弹窗打开），抽屉收起
  const fromSide = (fn) => (...args) => {
    setDrawer(false);
    return fn(...args);
  };
  // 左栏的回调（左栏记着渲染结果，同 act）
  const side = useLatest({ select: fromSide(select), settings: fromSide(() => setSettings("settings")), page: fromSide(openPage), login: fromSide(() => onLogin?.()),
    more: loadMore, search: openSearch, credits: refreshCredits });
  const listedTitle = tid ? threads?.find((t) => t.thread_id === tid)?.title : null; // 打开中还没取回状态时，顶栏先用列表里的标题
  const shownThreads = useMemo(() => threads && (gone.size ? threads.filter((t) => !gone.has(t.thread_id)) : threads), [threads, gone]); // 等着撤销的不显示
  const startedHere = !!tid && startedAs.current?.id === tid; // 开着的这段是这个页面刚发起的：列表里还没有它时左栏先补一行（见 withCurrent）
  // 开着的这段对话页面手上的样子，左栏和搜对话的窗口拿它盖住列表里那一行（见 withCurrent）。只挑用到的几样：整个 st、live 给过去的话每来一批字左栏都要重新渲染
  const status = st?.status;
  const runningSince = st?.running_since;
  const saidNow = live?.said;
  const cur = useMemo(() => ({ title, status, running_since: runningSince, said: saidNow, busy }), [title, status, runningSince, saidNow, busy]);
  // 交给对话那一栏的几样：同上，没变就是同一个对象（输入框、每一轮按它们记着渲染结果）
  const fileDo = useLatest({ open: openFile, toggle: toggleFiles });
  const files = useMemo(() => ({ open: fileDo.open, current: file, toggle: fileDo.toggle }), [fileDo, file]);
  const pickDo = useLatest({ thinking: setThinking, work: pickWork });
  const locked = !!(tid || live);
  const picker = useMemo(() => ({ current, locked, onPick: setPick, thinking, onThinking: pickDo.thinking, thinkAlways: !!work, onLogin }),
    [current, locked, thinking, pickDo, work, onLogin]);
  const mode = useMemo(() => ({ on: work, onChange: pickDo.work }), [work, pickDo]);
  // 搜对话、记忆页要的整份列表。左栏那份一直在更新，两边都有的行按左栏的；整份还没取回来（all 是 null）时先只有左栏那些：
  // 搜对话的窗口一打开就有最近的，搜不到的先不说没有（partial）；记忆页的出处等取全了再对，免得把更早的对话说成删了
  const allThreads = shownThreads && wholeList(all || [], shownThreads, gone);

  return (
    <NavContext.Provider value={nav}>
      <div className={"ws" + (sideOff ? " side-off" : "") + (mobile && drawer ? " drawer" : "") + (file && !mobile ? " with-file" : "")} data-theme={dark ? "dark" : "light"}>
        <Sidebar threads={shownThreads} more={more} onMore={side.more} current={tid} started={startedHere} cur={cur} work={work} now={now} href={chatPath} newHref={newPath} onSelect={side.select} manage={manage}
          onSettings={side.settings} onPage={auth.login || auth.me ? side.page : null} page={page} onSearch={side.search} auth={auth} onLogin={side.login} credits={credits} onCredits={side.credits} nav={nav} />
        {page === "library" ? (
          <Suspense fallback={<section className="min-w-0" aria-busy="true" />}>
            <LibraryPage auth={auth} onLogin={onLogin} undo={undo} onOpenFile={(f) => openFile({ kb: f })} openFileId={file?.kb?.id} />
          </Suspense>
        ) : page === "scheduled" ? (
          <Suspense fallback={<section className="min-w-0" aria-busy="true" />}>
            <ScheduledPage auth={auth} onLogin={onLogin} undo={undo} models={models} onOpen={(id, fresh) => select(id, "push", false, fresh)} onAsk={askNew} />
          </Suspense>
        ) : <Conversation st={shown} live={live} busy={busy} suggesting={suggesting} error={error} now={now} act={act} tid={tid} putBack={putBack} listedTitle={listedTitle}
          who={auth.ready ? (auth.me ? auth.me.name || "" : null) : earlyWho}
          models={models} modelNames={modelNames} credits={credits} manage={manage}
          picker={picker} work={mode} files={files} />}
        {file && (file.list ? <FileList tid={tid} st={shown} live={live} title={live?.title || shown?.title || listedTitle} onNav={navFile} onClose={closeFile} focus={fileFocus} />
          : file.sent ? <SentPanel tid={tid} f={file.sent} back={file.back} onNav={navFile} onClose={closeFile} focus={fileFocus}
              canExport={!!auth.me} onLogin={onLogin} />
            : file.kb ? <KnowledgePanel f={file.kb} at={file.at} onClose={closeFile} focus={fileFocus} />
            : <FilePanel tid={tid} st={shown} live={live} target={file} onTarget={setFile} onNav={navFile} onClose={closeFile} focus={fileFocus}
              canExport={!!auth.me} onLogin={onLogin} />)}
        {search && (
          <Suspense fallback={null}>
            <SearchDialog threads={allThreads && withCurrent(allThreads, tid, cur, work, startedHere)} partial={!all} now={now} href={chatPath}
              onOpen={(id) => { setSearch(false); select(id); }} onClose={() => setSearch(false)} />
          </Suspense>
        )}
        {settings && (
          <Suspense fallback={null}>
            <SettingsDialog view={settings} onView={setSettings} onClose={() => setSettings(null)} mobile={mobile} auth={auth}
              onLogin={onLogin && (() => { setSettings(null); onLogin(); })}
              theme={theme} onTheme={setTheme} think={thinkOn} onThink={changeThinkDefault}
              undo={undo} threads={all ? allThreads : null} onOpen={(id) => { setSettings(null); select(id); }} />
          </Suspense>
        )}
        <LoginDialog open={loginOpen} crowded={auth.crowded} want={loginWant} credits={credits} onClose={closeLogin} />
        <UndoToast undo={undo} />
        <Notices items={notices} href={chatPath} onClose={(key) => setNotices((ns) => ns.filter((n) => n.key !== key))}
          onOpen={(n) => { setNotices((ns) => ns.filter((x) => x.key !== n.key)); select(n.thread_id, "push", false, true); }} />
      </div>
    </NavContext.Provider>
  );
}

// 游客点了锁着的模型去登录：记下是哪个（OAuth 要跳出去再回来、邮箱登录完整页重载，所以记在 sessionStorage），登录回来取一次就删
const WANT_KEY = "lingdraft.want-model";
function keepWant(id) {
  try { if (id) sessionStorage.setItem(WANT_KEY, id); else sessionStorage.removeItem(WANT_KEY); } catch { /* 存不了就算了：回来按 Auto */ }
}
function takeWant() {
  try {
    const id = sessionStorage.getItem(WANT_KEY);
    sessionStorage.removeItem(WANT_KEY);
    return id;
  } catch { return null; }
}
