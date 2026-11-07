// 对话页：左侧对话列表（能搜标题，每条能置顶、改名、导出、删除），右侧对话；头像菜单里的“设置”在右侧换成设置页（/settings），设置页里点“记忆”换成记忆页（/memory）。
// 页面以服务器上的线程状态为准：对话和来源都从状态里取。执行中的事件（思考和正文的逐字片段、工具步骤）
// 叠在存下来的对话上实时显示，并提示该重新取一次状态了；所以刷新、换个页面打开、或者中途断线重连，看到的都一样。
// 回答时又说的话排在服务器上，这一轮答完或停下就接着发成新的一轮：这一轮的事件流结束时线程又在跑了，页面接着读下一次执行的。
// 这一轮出错时排着的话不发，取回输入框。排着的话也能插进这一轮：放进对话时事件流里来一条 guide，页面把它挪进过程里。
import { useEffect, useRef, useState } from "react";
import { createResearchThread, deleteResearchThread, editResearchThread, followResearchThread, getCredits, getResearchState, guideResearch, listModels, listResearchThreads,
  pinResearchThread, queueResearch, regenerateResearchThread, renameResearchThread, retryResearchThread, sayResearch, stopResearchThread, unguideResearch, unqueueResearch } from "./api.js";
import { IconClose, IconDownload, IconEdit, IconMore, IconPanel, IconPinTop, IconPlus, IconSearch, IconTrash } from "./icons.jsx";
import { setDraft } from "./drafts.js";
import { AFTER_RUN_ERROR, errorText, quotaText } from "./errors.js";
import { useLayer } from "./layers.js";
import { CreditsFoot, LoginDialog, UserFoot, useAuth } from "./login.jsx";
import { MemoryPage } from "./memory.jsx";
import { saveThinkDefault, thinkDefault, useTheme } from "./prefs.js";
import { SettingsPage } from "./settings.jsx";
import { ActionMenu, TitleInput } from "./menu.jsx";
import { MOBILE, NavContext, SEARCH_ARIA_KEY, SEARCH_KEY, SIDE_ARIA_KEY, SIDE_KEY, isSearchKey, isSideKey, useMedia } from "./nav.jsx";
import { ACTIVE, QUEUED, addLive, elapsed, itemKey, saveFile, sayWhen, stamp, threadMarkdown, useNow, when } from "./research.js";
import { useDoneAlert } from "./alert.jsx";
import { Conversation, forgetReading } from "./timeline.jsx";
import { UndoToast, useUndo } from "./undo.jsx";
import { uploadErrorText } from "./upload.js";
import "./workspace.css";

// 地址：/chat/new 是新对话，/chat/<线程 id> 是某个对话；别的地址（/、/chat）都当新对话，进来时改成 /chat/new。
// 线程 id 只有字母、数字、- 和 _（后端校验），地址里不用编码；对不上的 id 后端回 404，页面换成新对话
const NEW_PATH = "/chat/new";
const chatPath = (id) => (id ? `/chat/${id}` : NEW_PATH);
// 和对话同级的页：设置、记忆（右侧换成这一页，这时没有选中的对话）
const PAGES = { settings: "/settings", memory: "/memory" };
const pageOfPath = () => Object.keys(PAGES).find((k) => new RegExp(`^${PAGES[k]}/?$`).test(location.pathname)) || null;
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

// 一轮结束时还留在服务器上的话：要放回输入框的那几种
const leftover = (s) => [s.guided_message && "guided", s.queued_message && "queued"].filter(Boolean);

export default function Workspace() {
  const auth = useAuth(); // 身份：游客还是登录了（见 auth.js）。身份一变整页重载，这里不用跟着改状态
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginWant, setLoginWant] = useState(null); // 游客点了“登录后可用”里的哪个模型（登录弹窗说登录后就能用它，第 28 条）
  const [threads, setThreads] = useState(null);
  const [tid, setTid] = useState(routeId);
  const [page, setPageState] = useState(pageOfPath); // 右侧是哪一页："settings"、"memory"，对话是 null
  const pageRef = useRef(page);
  const setPage = (p) => {
    pageRef.current = p;
    setPageState(p);
  };
  const [theme, setTheme, dark] = useTheme(); // 主题（设置页，存本机）：跟随系统、浅色、深色
  const [st, setSt] = useState(null);
  // 这个页面发起或接上的那次执行：{said, image, sentAt, turnsAtStart, items, stage, phase, title, stopping, hidden, edit}
  // image：这句话带的图（{file, url}），存下来之前先按本机的显示
  // sentAt：这次说的话或改的话按本机时间记的发送时间，存下来的对话回来以前先显示它
  const [live, setLive] = useState(null);
  const [suggesting, setSuggesting] = useState(false); // 这一轮结束了，推荐问题还在起（同一条事件流里还会再来一次状态）
  const [busy, setBusyState] = useState(false);
  const [error, setError] = useState(null);
  const [putBack, setPutBack] = useState(null); // 要放回输入框的话：{thread, text, image?, rejected?}，每次是个新对象；image 是一起没发出去的图；rejected 是没发出去被拒的（连着被拒不叠加）
  // 能选的模型（/api/models，Auto 在第一个）：没取到是 []，不显示选择，新对话按 Auto
  const [models, setModels] = useState(null);
  // 新对话选的模型：null 是 Auto；开新对话时还是上次选的，刷新页面回到 Auto。开始的对话以它定下的为准（st.model）
  const [pick, setPick] = useState(null);
  // 深度思考开关：每句话发的时候带上（选的模型能开关才带）。新对话（包括刷新）按设置页的默认（存本机，默认开）；
  // 在一段对话里手动切了只管这段对话，记在 thinkSet 里（这次打开的页面里），回到这段对话还是切过的，开新对话又回到默认（2026-11-07 Sean 定）
  const [thinkOn, setThinkOn] = useState(thinkDefault); // 设置页里的默认
  const thinkOnRef = useRef(thinkOn);
  thinkOnRef.current = thinkOn;
  const [thinking, setThinkingState] = useState(thinkOn);
  const thinkSet = useRef(new Map()); // 对话 id（新对话是 "new"）→ 手动切成的开关
  const setThinking = (v) => {
    thinkSet.current.set(tidRef.current ?? "new", v);
    setThinkingState(v);
  };
  // 额度（/api/credits）：打开页面、每轮结束、打开账号菜单时取；没取到是 null（不显示）
  const [credits, setCredits] = useState(null);
  const refreshCredits = () => getCredits().then(setCredits).catch(() => {});
  const gen = useRef(0); // 每切一次线程加一：旧线程的连接和回调发现对不上，就不再改页面
  const tidRef = useRef(tid);
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
  const [gone, setGone] = useState(() => new Set()); // 点了删除、还在等撤销的对话：列表里不显示（隔几秒取回的列表里还有它）

  const setBusy = (b) => {
    busyRef.current = b;
    setBusyState(b);
  };

  const refreshList = () => listResearchThreads().then(setThreads).catch(() => {});
  const anyActive = busy || (threads || []).some((t) => ACTIVE.has(t.status));
  const now = useNow(anyActive);

  useEffect(() => {
    refreshList();
    if (!page) navigate(chatPath(tid), false);
    if (tid) restore(tid, gen.current);
    // 浏览器前进后退、手动改了地址：地址栏已经是了，只换页面（去海报页时这个页面会被换掉，不用管）
    const onPop = () => {
      if (location.hash === "#poster") return;
      const p = pageOfPath();
      if (p) return openPage(p, "none");
      if (pageRef.current || routeId() !== tidRef.current) select(routeId(), "none");
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
  }, [anyActive]);
  const title = live?.title || st?.title; // 执行中起好的标题跟着事件来，库里一轮跑完才有（之前是用户的原话）
  const done = useDoneAlert(tid, busy || ACTIVE.has(st?.status), st, title); // 用户没在看时答完了：标题前加“（答完了）”，见 alert.jsx
  useEffect(() => {
    const base = page === "memory" ? "灵稿 · 记忆" : page === "settings" ? "灵稿 · 设置" : title ? `${title} · 灵稿` : tid ? "灵稿 · 对话" : "灵稿 · 新对话";
    document.title = (done ? `（${done}）` : "") + base;
  }, [title, tid, page, done]);

  // 深色（批次 G 第 16 条；设置页能选跟随系统、浅色、深色）：深色时浏览器顶栏跟着深；只在对话页加，海报页还是浅色（index.html 里那条白的）
  useEffect(() => {
    if (!dark) return undefined;
    const m = Object.assign(document.createElement("meta"), { name: "theme-color", content: "#1b1f24" });
    document.head.prepend(m); // 好几条都对得上时浏览器用最前面那条
    return () => m.remove();
  }, [dark]);
  useEffect(() => { listModels().then(setModels).catch(() => setModels([])); }, []);
  // 身份定了取一次额度；每轮结束（busy 回到 false）再取：这一轮花的已经记上了
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (auth.ready && !busy) refreshCredits(); }, [auth.ready, busy]);
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
  // 这句话带不带思考开关：按输入框旁边显示的那个模型（能开关才带，不带的按模型的默认）
  const thinkOpts = () => (models?.find((m) => m.id === current)?.thinking === "toggle" ? { thinking } : {});

  // ---- 切换线程 ----

  // how：push 记进浏览器历史（点左栏的对话、新对话）；replace 换掉当前这条（当前对话删了、打不开）；none 地址栏已经是了
  const select = (id, how = "push") => {
    if (id === tidRef.current && id && !pageRef.current) return;
    setPage(null);
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
    if (how !== "none") navigate(chatPath(id), how === "push");
    if (id) restore(id, gen.current);
  };

  // 打开设置页、记忆页：先像点了新对话那样放下当前对话（连接断开，服务器上的执行照常跑），再换成那一页
  const openPage = (p, how = "push") => {
    if (pageRef.current === p) return;
    if (!pageRef.current) select(null, "none");
    setPage(p);
    if (how !== "none") navigate(PAGES[p], how === "push");
  };

  // 打开一个线程：取状态；服务器上还在跑，就接上这次执行的事件流（从这次执行的头一个事件补发）
  async function restore(id, g) {
    try {
      const s = await getResearchState(id);
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
      if (g !== gen.current) return;
      if (e.status === 404) select(null, "replace"); // 线程确实不在了才换成新对话；网络抖动时地址留着，刷新还能再试
      setError(errorText("没能打开这个对话", e, e.status === 404 ? null : "刷新页面再试一次"));
    }
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
        navigate(chatPath(data.thread_id), false); // 新对话有了 id：换掉 /chat/new，后退不回到空白的新对话
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
    } else if (ev === "delta" || ev === "step" || ev === "restart") {
      setLive((l) => addLive(l && { ...l, stage: null }, ev, data));
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
      const image = extra.image;
      // 带的图没收（超过 4 MB、格式不对、传不上去，见后端 api.main 的 _upload）：这句话没发出去，对话里什么都没多，话和图都放回输入框
      if (image && [413, 415, 502].includes(e.status)) {
        setPutBack({ thread: tidRef.current || "new", text: said, image, rejected: true });
        setError(`${uploadErrorText(e.status)}，话和图都放回输入框了`);
        setBusy(false);
        setLive(null);
        return;
      }
      // 额度用完了（429）：话放回输入框，提示里带上什么时候重置，游客还给登录入口；服务器上没有执行要去接
      if (e.status === 429 && e.detail?.code === "credits_exhausted") {
        const text = said ?? extra.edit;
        if (text) setPutBack({ thread: tidRef.current || "new", text, image, rejected: true });
        setError({ text: quotaText(e.detail) + (text ? "刚才那句已经放回输入框。" : ""), login: !!e.detail.guest, quota: true });
        refreshCredits();
        setBusy(false);
        setLive(null);
        return;
      }
      // 新对话被拒（比如选的模型刚下架）：话放回输入框，重新取能选的模型
      if (!tidRef.current && e.status === 400 && said) {
        setPutBack({ thread: "new", text: said, image, rejected: true });
        setError("这句话没发出去，已经放回输入框：选的模型现在用不了，换一个再发。");
        listModels().then(setModels).catch(() => {});
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
          setPutBack({ thread: tidRef.current || "new", text, image, rejected: true });
          setError(errorText(image ? "这句话没发出去，话和图都放回输入框了" : "这句话没发出去，已经放回输入框", e));
        }
        setBusy(false);
        setLive(null);
        return;
      }
      // 话被拒（400）、正在跑或上一轮没跑完（409）、连接断了（没有状态码）——都回服务器接上当前进度
      const recoverable = tidRef.current && (!e.status || e.status === 400 || e.status === 409);
      // 被拒的话（说的、改的）服务器没存下来：不再显示在对话里，放回输入框，和排队没排上一样
      const lost = recoverable && e.status ? said ?? extra.edit : null;
      if (lost) setPutBack({ thread: tidRef.current, text: lost, image });
      if (e.status || !recoverable) setError(errorText(lost ? "这句话没发出去，已经放回输入框" : extra.what || (said ? "这句话没发出去" : "这一步没做成"), e));
      if (recoverable) {
        const rejected = e.status ? { said: null, edit: undefined, sentAt: undefined } : {};
        setLive((l) => ({ items: [], ...l, ...rejected, stage: e.status ? "服务器还在处理，等它完成" : "连接断了，正在重连…" }));
        return await follow(tidRef.current, g, errs, r);
      }
      setBusy(false);
      setLive(null);
    }
  }

  const act = {
    // 新对话带上选的模型（没取到列表就不带，按 Auto）；之后每句话带上深度思考开关（对话定下的模型能开关才带）。
    // image：这句话带的图（{file, url}，见 Composer），排着的话不带图
    start: (text, image) => run((cb, sig) => createResearchThread(text, { model: picked, ...thinkOpts(), image: image?.file }, cb, sig), text, { image }),
    say: (text, image) => {
      const id = tidRef.current;
      const opts = { ...thinkOpts(), image: image?.file };
      return run((cb, sig) => sayResearch(id, text, opts, cb, sig), text, { image });
    },
    retry: () => run((cb, sig) => retryResearchThread(tidRef.current, cb, sig), null, { what: "没能重试" }),
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
      setLive((l) => (l ? { ...l, stopping: true } : l));
      try {
        await stopResearchThread(tidRef.current);
      } catch (e) {
        if (g !== gen.current || e.status === 409) return; // 409：这一轮刚好已经跑完
        setLive((l) => (l ? { ...l, stopping: false } : l));
        setError(errorText("没停下来", e, "再点一次停止"));
      }
    },
  };

  // 左栏、顶栏菜单：置顶、改名、导出、删除。出错就在对话区提示
  const unhide = (id) => setGone((g) => {
    const n = new Set(g);
    n.delete(id);
    return n;
  });
  const manage = {
    rename: async (id, title) => {
      try {
        const row = await renameResearchThread(id, title);
        setThreads((ts) => ts && ts.map((t) => (t.thread_id === id ? { ...t, ...row } : t)));
        if (id === tidRef.current) setSt((s) => (s ? { ...s, title: row.title } : s));
      } catch (e) {
        setError(errorText("标题没改成", e));
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
          } catch (e) {
            setError(errorText("没删成", e));
          }
          unhide(id);
          refreshList();
        },
        undo: () => {
          unhide(id);
          if (open && tidRef.current === null && !pageRef.current && !busyRef.current) select(id);
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
  };

  // 执行中这一轮的来源跟着事件来（库里一轮跑完才有），叠在已存的来源上：正文里的引用据此显示成编号
  const shown = st && live?.sources ? { ...st, sources: { ...st.sources, ...live.sources } } : st;

  // 游客开登录弹窗（左栏、额度、设置页和记忆页、报错里的登录，点“登录后可用”的模型）。点模型进来的带着那个模型；从按钮直接进来的参数是点击事件，不算
  const onLogin = auth.login && !auth.me ? (want) => {
    const m = want?.id && want.locked ? want : null;
    setLoginWant(m);
    keepWant(m?.id);
    setLoginOpen(true);
  } : null;
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
    if (document.activeElement?.closest?.(".ws-side")) navFocus.current = "drawer"; // 焦点在抽屉里：关上以后回到抽屉钮
    setDrawer(false);
  };
  const nav = { mobile, collapsed: sideOff, drawer, toggleSide, openDrawer: () => setDrawer(true), closeDrawer, newChat: () => select(null) };
  useEffect(() => {
    const to = navFocus.current;
    navFocus.current = null;
    if (to) document.querySelector(`[data-nav="${to}"]`)?.focus();
  }, [sideOff, drawer]);
  useEffect(() => { if (!mobile) setDrawer(false); }, [mobile]); // 窗口拉宽了：抽屉不再是抽屉
  // 快捷键：桌面上收起、展开左栏，窄屏开、关抽屉；⌘K 到左栏的搜索框（收起着先展开，窄屏先开抽屉）。别的弹窗开着时 ⌘K 不动
  const sideKey = useRef(null);
  sideKey.current = () => (mobile ? (drawer ? closeDrawer() : setDrawer(true)) : toggleSide());
  const searchKey = useRef(null);
  searchKey.current = () => {
    if (mobile && !drawer) {
      navFocus.current = "search";
      setDrawer(true);
    } else if (!mobile && sideOff) {
      navFocus.current = "search";
      saveSideOff(false);
      setSideOff(false);
    } else {
      const box = document.querySelector('[data-nav="search"]');
      box?.focus();
      box?.select();
    }
  };
  useEffect(() => {
    const key = (e) => {
      if (e.repeat) return;
      if (isSearchKey(e)) {
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
  // 在抽屉里点了一条对话、新对话、设置、登录：换过去（登录弹窗打开），抽屉收起
  const fromSide = (fn) => (...args) => {
    setDrawer(false);
    return fn(...args);
  };
  const listedTitle = tid ? threads?.find((t) => t.thread_id === tid)?.title : null; // 打开中还没取回状态时，顶栏先用列表里的标题
  const shownThreads = threads && (gone.size ? threads.filter((t) => !gone.has(t.thread_id)) : threads); // 等着撤销的不显示

  return (
    <NavContext.Provider value={nav}>
      <div className={"ws" + (sideOff ? " side-off" : "") + (mobile && drawer ? " drawer" : "")} data-theme={dark ? "dark" : "light"}>
        {mobile && drawer && <div className="ws-scrim" onClick={closeDrawer} aria-hidden="true" />}
        <Sidebar threads={shownThreads} current={tid} st={st} live={live} busy={busy} now={now} onSelect={fromSide(select)} manage={manage}
          page={page} onSettings={fromSide(() => openPage("settings"))} auth={auth} onLogin={fromSide(() => onLogin?.())} credits={credits} onCredits={refreshCredits} nav={nav} />
        {page === "memory" ? <MemoryPage auth={auth} onLogin={onLogin} undo={undo} threads={shownThreads} onOpen={(id) => select(id)} onBack={() => openPage("settings")} />
          : page === "settings" ? <SettingsPage auth={auth} onLogin={onLogin} theme={theme} onTheme={setTheme} think={thinkOn}
            onThink={(v) => { saveThinkDefault(v); setThinkOn(v); }} onMemory={() => openPage("memory")} />
          : <Conversation st={shown} live={live} busy={busy} suggesting={suggesting} error={error} now={now} act={act} tid={tid} putBack={putBack} listedTitle={listedTitle}
            models={models} credits={credits} manage={manage}
            picker={{ current, locked: !!(tid || live), onPick: setPick, thinking, onThinking: setThinking, onLogin }} />}
        <LoginDialog open={loginOpen} crowded={auth.crowded} want={loginWant} credits={credits} onClose={closeLogin} />
        <UndoToast undo={undo} />
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

// ---------------- 左栏：对话列表 ----------------

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

function rowSub(t, now) {
  if (t.status === "queued") return "排队中";
  if (t.status === "running") return t.running_since ? `进行中，已用 ${elapsed(t.running_since, now)}` : "进行中";
  if (t.status === "failed") return "没有完成，可以重试";
  return "";
}

// 搜到的那几个字标出来（只搜标题，不分大小写）
function Hit({ text, q }) {
  const i = q ? text.toLowerCase().indexOf(q) : -1;
  if (i < 0) return text;
  return <>{text.slice(0, i)}<mark>{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>;
}

// 窄屏时这一栏是抽屉（nav.drawer 开着）：打开时焦点进来，Tab 在里面转，Esc、点外面、右上角的关闭都能关上。
// 抽屉里的菜单、改名框开着、搜索框里有字时，Esc 先归它们
const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])';

function Sidebar({ threads, current, st, live, busy, now, onSelect, manage, page, onSettings, auth, onLogin, credits, onCredits, nav }) {
  const [menu, setMenu] = useState(null); // 打开菜单的那一行：{id, anchor: 那一行的“⋯”}
  const [renaming, setRenaming] = useState(null); // 正在改名的线程 id
  const [query, setQuery] = useState(""); // 搜索框里的字
  const box = useRef(null);
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
  // 列表每隔几秒取一次；当前这个线程以页面手上的状态为准，刚新建、列表里还没有的先补一行
  let rows = threads || [];
  if (current) {
    const mine = rows.find((t) => t.thread_id === current);
    const title = live?.title || st?.title;
    const patch = { ...(busy ? { status: "running", running_since: st?.running_since || mine?.running_since } : st ? { status: st.status } : {}),
      ...(title ? { title } : {}) };
    rows = mine ? rows.map((t) => (t.thread_id === current ? { ...t, ...patch } : t))
      : [{ thread_id: current, title: live?.said || "新对话", status: "running", ...patch }, ...rows];
  }
  // 标题一样的几条（比如两个“广州今日天气”）：下面多一行日期和钟点，好分清
  const seen = {};
  for (const t of rows) seen[t.title] = (seen[t.title] || 0) + 1;
  const q = query.trim().toLowerCase();
  const hits = q ? rows.filter((t) => (t.title || "").toLowerCase().includes(q)).sort(newest(now)) : null;
  const groups = GROUPS.map(([key, name]) => {
    const items = rows.filter((t) => groupOf(t, now) === key);
    items.sort(key === "pinned" ? (a, b) => Date.parse(b.pinned_at) - Date.parse(a.pinned_at) : newest(now));
    return [key, name, items];
  }).filter(([, , items]) => items.length);

  // 删掉一行以后焦点放到下一行（没有就上一行、再没有就“新对话”）：删的按钮跟着那一行没了，焦点不掉到页面上
  const neighbor = (id) => {
    const links = [...box.current.querySelectorAll(".ws-groups .ws-task-link")];
    const i = links.findIndex((a) => a.dataset.id === id);
    return links[i + 1] || links[i - 1] || box.current.querySelector(".ws-new");
  };
  const focusRow = (id) => box.current?.querySelector(`.ws-task-link[data-id="${CSS.escape(id)}"]`)?.focus();
  const menuRow = menu && rows.find((t) => t.thread_id === menu.id); // 菜单开着时那一行变了（开始回答了）按新的
  const sz = nav.mobile ? 18 : 14;

  const row = (t, group) => {
    if (renaming === t.thread_id) {
      return (
        <div key={t.thread_id} className="ws-task renaming">
          <TitleInput value={t.title} onDone={(title, how) => {
            setRenaming(null);
            if (title && title !== t.title) manage.rename(t.thread_id, title);
            if (how !== "blur") requestAnimationFrame(() => focusRow(t.thread_id)); // 回车、Esc：回到这一行；点了别处就在别处
          }} />
        </div>
      );
    }
    const sel = t.thread_id === current;
    const open = menu?.id === t.thread_id;
    const status = ACTIVE.has(t.status) ? "active" : t.status === "failed" ? "failed" : "idle";
    const twin = seen[t.title] > 1 && stamp(t.updated_at, now);
    const sub = rowSub(t, now) || (twin ? sayWhen(twin) : "");
    return (
      <div key={t.thread_id} className={"ws-task" + (sel ? " sel" : "") + (open ? " menu" : "")}>
        <a href={chatPath(t.thread_id)} data-id={t.thread_id} className="ws-task-link" aria-current={sel ? "page" : undefined}
          onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onSelect(t.thread_id); }}>
          <span className={"dot " + status} />
          <span className="body">
            <span className="title" title={t.title}><Hit text={t.title} q={q} /></span>
            {sub && <span className="sub">{sub}</span>}
          </span>
          <span className="time">{when(t.updated_at, now, group)}</span>
        </a>
        <button className="more" aria-label={`“${t.title}”的更多操作`} aria-haspopup={nav.mobile ? "dialog" : "menu"} aria-expanded={open}
          onClick={(e) => setMenu(open ? null : { id: t.thread_id, anchor: e.currentTarget })}>
          <IconMore size={15} />
        </button>
      </div>
    );
  };

  return (
    <aside ref={box} id="ws-side" className="ws-side" onKeyDown={onKeyDown}
      {...(modal ? { role: "dialog", "aria-modal": true, "aria-label": "对话列表" } : {})}>
      <div className="ws-brand">
        <span className="logo">灵</span>
        <span className="name">灵稿</span>
        <span className="tag">对话</span>
        {nav.mobile
          ? <button className="ws-icon-btn ws-side-toggle" data-nav="close" aria-label="关闭对话列表" onClick={nav.closeDrawer}><IconClose size={18} /></button>
          : <button className="ws-icon-btn ws-side-toggle" data-nav="collapse" aria-label="收起侧栏" title={`收起侧栏（${SIDE_KEY}）`} aria-keyshortcuts={SIDE_ARIA_KEY}
            onClick={nav.toggleSide}><IconPanel size={18} /></button>}
      </div>
      <div className="ws-side-top">
        <button className="ws-new" onClick={() => onSelect(null)} aria-current={!current && !page ? "page" : undefined}>
          <IconPlus size={15} /> 新对话
        </button>
      </div>
      {/* 搜索（第 13 条、M1）：边打边筛，只搜标题；Esc 清空，↓ 到第一条，回车打开第一条 */}
      <div className="ws-search" role="search"
        onMouseDown={(e) => { if (!e.target.closest("input, button")) { e.preventDefault(); e.currentTarget.querySelector("input").focus(); } }}>
        <IconSearch size={15} />
        <input type="search" data-nav="search" data-esc={query ? "" : undefined} value={query} placeholder="搜索对话标题" aria-label="搜索对话标题" aria-keyshortcuts={SEARCH_ARIA_KEY}
          enterKeyHint="search" autoComplete="off" onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return;
            if (e.key === "Escape" && query) {
              e.preventDefault();
              e.stopPropagation();
              setQuery("");
            } else if (e.key === "ArrowDown") {
              const a = box.current.querySelector(".ws-groups .ws-task-link");
              if (a) { e.preventDefault(); a.focus(); }
            } else if (e.key === "Enter" && hits?.length) {
              e.preventDefault();
              onSelect(hits[0].thread_id);
            }
          }} />
        {query ? (
          <button className="ws-search-clear" aria-label="清空搜索" onClick={(e) => { setQuery(""); e.currentTarget.parentElement.querySelector("input").focus(); }}>
            <IconClose size={14} />
          </button>
        ) : !nav.mobile && <kbd className="ws-kbd" aria-hidden="true">{SEARCH_KEY}</kbd>}
      </div>
      <nav className="ws-groups" aria-label="对话列表">
        {threads === null && (
          <div className="ws-side-skel" role="status" aria-label="正在取对话列表">
            {[78, 62, 70, 54, 66].map((w) => <span key={w} className="ws-skel ws-skel-row" style={{ width: `${w}%` }} />)}
          </div>
        )}
        {threads !== null && !hits && rows.length === 0 && <div className="ws-side-empty">还没有对话</div>}
        {hits ? (hits.length ? (
          <div className="ws-group" role="group" aria-label="搜索结果">
            <div className="ws-group-name">标题里有“{query.trim()}”<span>{hits.length}</span></div>
            {hits.map((t) => row(t))}
          </div>
        ) : threads !== null && <div className="ws-side-empty">没有标题里有“{query.trim()}”的对话。只搜标题，不搜对话内容。</div>)
          : groups.map(([key, name, items]) => (
            <div className="ws-group" key={key} role="group" aria-label={name}>
              <div className="ws-group-name" aria-hidden="true">{name}<span>{items.length}</span></div>
              {items.map((t) => row(t, key))}
            </div>
          ))}
      </nav>
      <div className="ws-sr" role="status">{hits ? (hits.length ? `找到 ${hits.length} 段对话` : "没有找到") : ""}</div>
      <div className="ws-foot">
        <CreditsFoot credits={credits} guest={!auth.me} onLogin={auth.login ? onLogin : null} onOpen={onCredits} mobile={nav.mobile} />
        <span className="ws-foot-user"><UserFoot auth={auth} onLogin={onLogin} onSettings={onSettings} mobile={nav.mobile} /></span>
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
