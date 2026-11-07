// 对话。一轮是用户的一句话加上模型这一轮做的事：先是过程（思考、搜索、读页按发生的先后），然后是天气卡片、画的图，再是正文和这条回答用到的来源；
// 最后一轮下面是推荐问题（起的时候先说一声）。回答时又说的话排着（最多一句），贴在输入框上沿，这一轮答完就发，能撤回到输入框；
// 也能插进这一轮：模型做完手上这一步就看到它，插进去以后排在过程里。过程正在跑时展开，跑完不自动收起（用户自己收）；重新打开的对话里收成一行
// “已完成 25 秒 · 搜了 6 次 · 读了 4 页”，点开看每一步。收起的内容还留在页面里，浏览器的查找搜得到（见 Fold）。
// 每一轮是一个 section（.ws-turn），回答开头有“灵稿”的标识；三轮起右边（宽屏）或顶栏（窄屏）有轮次目录，见 TurnIndex。
// 每句话和每条回答下面有时间，都能复制；最后一句在空闲时能改，改完按新的话重新回答。这些一直显示，之前各轮的淡一档。
// 存下来的对话为准，正在跑的这一轮叠上实时片段。
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { listInspirations } from "./api.js";
import { getDraft, setDraft } from "./drafts.js";
import { failureText } from "./errors.js";
import { IconArrowDown, IconArrowUp, IconBook, IconBulb, IconCheck, IconChevron, IconClose, IconCloudSun, IconCopy, IconDownload, IconEdit, IconFork, IconImage, IconInsert, IconLock, IconLoop, IconMore, IconPage, IconPin, IconPlane, IconPrint, IconRetry, IconRoute, IconSearch, IconStop, IconTicket, IconTrain, IconTrash, IconUndo, IconWarn, Spinner } from "./icons.jsx";
import { ImageCards, Viewer, drawn, imageUrl, imagesOf } from "./images.jsx";
import { MODES, MapCard, mapsOf } from "./maps.jsx";
import { ItineraryCard, TravelCard, itinerariesOf, travelOf } from "./travel.jsx";
import { useLayer } from "./layers.js";
import { Markdown } from "./markdown.jsx";
import { ActionMenu } from "./menu.jsx";
import { HeadBar, NavContext, byKeyboard, touch } from "./nav.jsx";
import { ACTIVE, QUEUED, answerText, citeNumbers, copyText, creditsLeft, creditsLine, creditsText, cutPartialCite, domain, elapsed, loginLine, mergeItems, modelName, splitTurn, stamp, tidyTyping } from "./research.js";
import { AnswerSources, Fold, Mark, useFoldOpen } from "./sources.jsx";
import { TurnIndex, TurnIndexButton, turnEntries } from "./turns.jsx";
import { IMAGE_ACCEPT, ImageError, prepareImage } from "./upload.js";
import { WeatherCard, forecastsOf } from "./weather.jsx";
import { NotifyToggle } from "./alert.jsx";

// 今日灵感没取到时，新对话页显示这几个
const EXAMPLES = [
  "深圳这周会不会降温？",
  "最近一周 AI 圈有什么新闻？",
  "珠穆朗玛峰现在的官方高度是多少？",
];

// 正文 680 居中，右边空出来的那块放得下 184 宽的目录加两边各 16 的空：滚动区至少这么宽才放右栏
const RAIL_MIN = 1120;

// 阅读位置（第 45 条）：每个对话看到哪儿了记在这个标签页里（sessionStorage），离开（换对话、去海报页、登录后整页重载、刷新）
// 再回来，回到原处。记的是“第几轮，视口上沿在这一轮顶上往下多少”；在底部就记“底部”，回来还在底部、接着跟。从没记过的照旧到底（最新一轮）
const READING = "lingdraft.reading.";
const readingOf = (id) => { try { return JSON.parse(sessionStorage.getItem(READING + id)); } catch { return null; } };
const keepReadingOf = (id, at) => { try { sessionStorage.setItem(READING + id, JSON.stringify(at)); } catch { /* 存不下就只管这一次打开 */ } };
export const forgetReading = (id) => { try { sessionStorage.removeItem(READING + id); } catch { /* 没有就算了 */ } };

export function Conversation({ st, live, busy, suggesting, error, now, act: rawAct, tid, putBack, listedTitle, models, credits, picker, manage, who }) {
  const scrollRef = useRef(null);
  const stick = useRef(true); // 用户往上翻看时不自动滚到底
  const lastTop = useRef(0); // 上一次滚动时的位置：往上滚了才算用户离开底部
  const [away, setAway] = useState(false); // 离底部远了：输入框上面出“回到底部”
  const [more, setMore] = useState(false); // 离开底部以后下面又长出了内容：“回到底部”写上“有新内容”
  const [wide, setWide] = useState(false); // 正文右边放得下轮次目录（见 RAIL_MIN）
  const runningRef = useRef(false); // 有一轮在跑：这时内容变高才算“有新内容”（用户自己点开收起的过程不算）
  const fresh = !tid && !live;
  const mobile = !!useContext(NavContext)?.mobile;
  const opening = !!tid && !st && !live && !error; // 点开了一个对话，状态还没取回来：先显示骨架
  const toBottom = () => {
    const el = scrollRef.current;
    holding.current = null;
    stick.current = true;
    setAway(false);
    setMore(false);
    el.scrollTop = el.scrollHeight;
    lastTop.current = el.scrollTop;
  };
  // 阅读位置：滚动时量好放在 reading 里（这时页面上还是这个对话），隔一会儿、离开时写下
  const reading = useRef(null); // {tid, at: {bottom: true} | {turn, offset}}
  const readTimer = useRef(0);
  // 刚回到上次看到的地方：那一轮里视口上面的内容可能还在长（天气卡先量宽度、下一遍才画出逐小时图，一张高 132），
  // 浏览器的滚动锚定会让看到的字不动，结果比原处往下偏了那么多。所以回去以后先守着这个位置，内容一变高就再对一次，
  // 用户自己动了（滚、按键、点、摸）或者过了 3 秒就不守了
  const holding = useRef(null);
  const holdTimer = useRef(0);
  const keepReading = () => {
    clearTimeout(readTimer.current);
    if (reading.current) keepReadingOf(reading.current.tid, reading.current.at);
  };
  const measure = (el) => {
    if (stick.current) return { bottom: true };
    const top = el.getBoundingClientRect().top;
    for (const turn of el.querySelectorAll(".ws-turn[data-turn]")) {
      const r = turn.getBoundingClientRect();
      if (r.bottom > top + 1) return { turn: Number(turn.dataset.turn), offset: Math.round(top - r.top) };
    }
    return { bottom: true };
  };
  const backTo = (at) => {
    const el = scrollRef.current;
    const turn = at && !at.bottom && el.querySelector(`.ws-turn[data-turn="${Number(at.turn)}"]`);
    if (!turn) return false; // 那一轮没了（改了最后一句以后轮数变了）：到底
    el.scrollTop += turn.getBoundingClientRect().top - el.getBoundingClientRect().top + (Number(at.offset) || 0);
    lastTop.current = el.scrollTop;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stick.current = near;
    setAway(!near);
    setMore(false);
    return true;
  };

  // 换了对话：先把上一个看到哪儿写下，再到这个对话上次看到的地方（没记过就到底）；之后内容变高（字出来、过程展开）或可视区变矮时，
  // 贴着底的跟到底，往上翻着看的不动，“回到底部”写上“有新内容”。看的是尺寸变化，不靠这里重新渲染
  useLayoutEffect(() => {
    keepReading();
    const at = st?.thread_id && readingOf(st.thread_id);
    if (!backTo(at)) return toBottom();
    holding.current = at;
    clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => { holding.current = null; }, 3000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st?.thread_id]);
  useEffect(() => {
    const release = () => { holding.current = null; };
    const evs = ["wheel", "touchstart", "keydown", "pointerdown"];
    evs.forEach((e) => addEventListener(e, release, { capture: true, passive: true }));
    return () => {
      evs.forEach((e) => removeEventListener(e, release, { capture: true, passive: true }));
      clearTimeout(holdTimer.current);
    };
  }, []);
  useEffect(() => {
    addEventListener("pagehide", keepReading);
    return () => { // 去海报页：这个对话看到哪儿写下
      removeEventListener("pagehide", keepReading);
      keepReading();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    let h = el.firstElementChild.offsetHeight;
    const ro = new ResizeObserver(() => {
      const grew = el.firstElementChild.offsetHeight > h;
      h = el.firstElementChild.offsetHeight;
      if (stick.current) el.scrollTop = el.scrollHeight;
      else {
        if (holding.current) backTo(holding.current); // 刚回到原处，上面又长了：再对一次（见 holding）
        if (!stick.current && grew && runningRef.current) setMore(true);
      }
    });
    ro.observe(el);
    ro.observe(el.firstElementChild);
    const width = new ResizeObserver(() => setWide(el.clientWidth >= RAIL_MIN));
    width.observe(el);
    return () => {
      ro.disconnect();
      width.disconnect();
    };
  }, []);
  // 用户自己做的（发一句话、重新回答、重试、改最后一句、点接着问）：跳到底跟着看这一轮。排着的话自动开跑、打开一个正在跑的对话都不拉人。
  // 光标不搬：按钮点完没了（接着问、重试、重新回答这一轮跑起来就不在了），焦点会掉到页面上，这时才放回输入框，好接着写；原来在哪就还在哪
  const act = { ...rawAct };
  for (const k of ["start", "say", "retry", "regenerate", "edit"]) {
    act[k] = (...a) => {
      toBottom();
      const r = rawAct[k](...a);
      requestAnimationFrame(() => { if (!document.activeElement || document.activeElement === document.body) focusInput(); });
      return r;
    };
  }
  // 新对话的今日灵感：桌面在输入框下面，窄屏输入框贴底、它们在输入框上面。按看到的顺序放，Tab 和读屏的先后和眼睛看的一致
  const starters = fresh && <Inspirations onPick={act.start} />;

  const mode = fresh ? "new" : opening ? "opening" : st?.status === "queued" ? "queued" : busy || ACTIVE.has(st?.status) ? "busy" : st?.status || "busy";
  const stopping = !!(live?.stopping || st?.stopping);
  // 回答时（包括还在排队、正在停止）发的话排着，这一轮答完就发；新对话刚发出去、还没拿到 id 时排不了
  const queueing = !!tid && (mode === "busy" || mode === "queued");
  const onSend = (text, image) => { // image：带的图（{file, url}），排着的话不带
    if (mode === "new") return act.start(text, image);
    if (mode === "idle") return act.say(text, image);
    if (queueing) return act.queue(text);
    return undefined;
  };
  const title = live?.title || st?.title || live?.said || (opening ? listedTitle ?? null : "新对话"); // null：打开中、列表里也还没有，顶栏先放占位
  const running = busy || ACTIVE.has(st?.status);
  // 顶栏的“⋯”（第 14 条）：这个对话的重命名、导出、打印、删除，和左栏那一行的菜单是同一些事（置顶只在左栏）。改名就在顶栏标题处改
  const [menuAt, setMenuAt] = useState(null); // 菜单开着：打开它的按钮
  const [renaming, setRenaming] = useState(false);
  useEffect(() => {
    setMenuAt(null);
    setRenaming(false);
  }, [tid]);
  const manageable = !!(manage && tid && st && !opening);
  const status = running ? "running" : st?.status;
  const headMenu = manageable && (
    <button className="ws-icon-btn ws-head-more" aria-label="这段对话的更多操作" title="更多操作" aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={!!menuAt}
      onClick={(e) => setMenuAt(menuAt ? null : e.currentTarget)}>
      <IconMore size={18} />
    </button>
  );
  const sz = mobile ? 18 : 14;
  const closeMenu = () => setMenuAt(null);
  const menuItems = [
    { key: "rename", icon: <IconEdit size={sz} />, text: "重命名", disabled: status !== "idle", onClick: () => { closeMenu(); setRenaming(true); } },
    { key: "export", icon: <IconDownload size={sz} />, text: "导出为 Markdown", onClick: () => { closeMenu(); manage.exportMd(tid); } },
    // 打印：等菜单收起来再开打印（打印的是收起菜单以后的页面）
    { key: "print", icon: <IconPrint size={sz} />, text: "打印", onClick: () => { closeMenu(); setTimeout(() => window.print(), 50); } },
    { key: "delete", icon: <IconTrash size={sz} />, text: "删除", danger: true, disabled: status === "running" || status === "queued",
      onClick: () => {
        closeMenu();
        manage.remove(tid);
        // 对话换成了新对话：桌面上光标去输入框；手机上不弹键盘，焦点放到抽屉钮
        if (mobile) requestAnimationFrame(() => document.querySelector('[data-nav="drawer"]')?.focus());
        else focusInput();
      } },
  ];
  const menuNote = running ? "正在回答，答完或停下后才能改名、删除" : status === "failed" ? "这段对话没有完成，重试以后才能改名" : null;
  const edit = renaming && manageable ? {
    value: title || "",
    onDone: (t, how) => {
      setRenaming(false);
      if (t && t !== title) manage.rename(tid, t);
      if (how !== "blur") requestAnimationFrame(() => document.querySelector(".ws-head-more")?.focus());
    },
  } : null;
  runningRef.current = running;
  const pendingKinds = running ? [st?.guided_message && "guided", st?.queued_message && "queued"].filter(Boolean) : [];
  const said = useAnnounce(st, running, stopping); // 读屏：只在开始、答完、停下、出错时说一句（见 useAnnounce）
  // 轮次目录：三轮起才有，只列轮次，见 turnEntries。正文右边放得下就放一栏，放不下顶栏右边一个按钮点开
  const entries = !fresh && !opening ? turnEntries(st, live) : [];
  const index = entries.length > 0;
  const jumpTo = (id) => {
    const el = scrollRef.current?.querySelector(`[data-turn="${CSS.escape(id)}"]`);
    if (!el) return;
    stick.current = false;
    el.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    el.focus({ preventScroll: true }); // 键盘、读屏接着从这一轮往下读
  };

  return (
    <section className={"ws-main" + (fresh ? " fresh" : "")} aria-label="对话">
      <HeadBar title={fresh ? "新对话" : title} edit={edit}
        end={<>{index && !wide && <TurnIndexButton entries={entries} scroller={scrollRef} onJump={jumpTo} />}{headMenu}</>} />
      {menuAt && manageable && <ActionMenu label="这段对话的操作" anchor={menuAt} mobile={mobile} items={menuItems} note={menuNote} onClose={closeMenu} />}
      <div className={"ws-scroll" + (index && wide ? " rail" : "")} ref={scrollRef}
        onScroll={(e) => {
          // 往上滚了：离底部远了就不再跟着到底。没往上滚（内容长高了、往下滚）不算离开：跳到底以后内容马上长高，滚动事件晚一帧到，
          // 这时离底已经远了，但用户没动
          const el = e.currentTarget;
          const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          stick.current = el.scrollTop < lastTop.current - 1 ? near : stick.current || near;
          lastTop.current = el.scrollTop;
          setAway(!stick.current);
          if (stick.current) setMore(false);
          if (st?.thread_id && !opening) {
            reading.current = { tid: st.thread_id, at: measure(el) };
            clearTimeout(readTimer.current);
            readTimer.current = setTimeout(keepReading, 400);
          }
        }}>
        <div className="ws-flow">
          {!fresh && !opening && <h1 className="ws-print-title">{title}</h1>/* 只在打印时出现：屏幕上标题在顶栏，打印不印顶栏（第 49 条） */}
          {fresh ? <Welcome who={who} /> : opening ? <ThreadSkeleton /> : <Turns st={st} live={live} busy={busy} suggesting={suggesting} stopping={stopping} now={now} act={act} models={models} title={title} />}
          {error && <div className="ws-error" role="alert"><IconWarn size={15} /><span>{error.text ?? error}</span>
            {error.login && picker.onLogin && <button className="ws-btn ws-error-act" onClick={picker.onLogin}>登录</button>}</div>}
        </div>
      </div>
      {index && wide && <TurnIndex entries={entries} scroller={scrollRef} onJump={jumpTo} />}
      <div className="ws-sr" role="status" aria-live="polite">{said}</div>
      {mobile && starters}
      <div className="ws-dock">
        {/* 刚被额度拦住、报错里已经说了用完了和登录后会怎样：输入框上面不再重复一遍（第 27 条：同一句别在最烦的时候说好几遍） */}
        {!error?.quota && <CreditsNote credits={credits} onLogin={picker.onLogin} />}
        {/* 按下去不抢光标：输入框里写着的字和光标都留着 */}
        {away && <button className={"ws-jump" + (more ? " more" : "")} aria-label={more ? "有新内容，回到底部" : "回到底部"} title="回到底部"
          onMouseDown={(e) => e.preventDefault()} onClick={toBottom}><IconArrowDown size={16} />{more && <span>有新内容</span>}</button>}
        <Composer thread={tid || "new"} carry={!!live} mode={mode} queueing={queueing} held={pendingKinds.length > 0} stopping={stopping} putBack={putBack} onSend={onSend} onStop={act.stop}
          above={running && <Pending st={st} act={act} />} onTakeBack={pendingKinds.length ? () => act.withdraw(pendingKinds) : null}
          tools={<ModelTools models={models} loading={models === null || opening} {...picker} />} />
      </div>
      {!mobile && starters}
    </section>
  );
}

// 读屏播报（polite）：这一轮开始、答完（带来源条数）、停下、出错时各说一句。过程里的阶段、正文逐字出现都不播：
// 阶段一变就播会打断，正文要听的人等“答完了”再从这一轮开头读（这一轮在跑时标着 aria-busy，见 Turns）
function useAnnounce(st, running, stopping) {
  const [said, setSaid] = useState("");
  const was = useRef(running);
  const tid = st?.thread_id;
  const seen = useRef(tid);
  useEffect(() => {
    if (seen.current !== tid) { // 换了对话：不播，从这个对话现在的样子开始算
      seen.current = tid;
      was.current = running;
      return;
    }
    if (running === was.current) return;
    was.current = running;
    if (running) { setSaid(stopping ? "" : "开始回答"); return; }
    const t = st?.turns?.at(-1);
    const rec = t?.record;
    const n = Object.keys(citeNumbers((t?.items || []).filter((i) => i.type === "text").map((i) => i.text), st?.sources)).length; // 这一轮正文引用的
    setSaid(st?.status === "failed" ? "这一轮中途停下了，可以重试"
      : rec?.outcome === "stopped" ? "已停止"
        : rec?.outcome === "blocked" ? "内容审核没通过，这一轮停下了"
          : `回答完了${n ? `，用到 ${n} 个来源` : ""}`);
  }, [running, tid, st, stopping]);
  return said;
}

// 打开对话、状态还没回来时的骨架：一句话、几行回答的样子。取得快（本机、缓存）时不闪一下：过 200ms 还没回来才显示
function ThreadSkeleton() {
  const [show, setShow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShow(true), 200);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="ws-skel-thread" role="status" aria-label="正在打开对话">
      {show && (
        <>
          <div className="ws-skel ws-skel-bubble" />
          {[96, 88, 92, 60].map((w) => <div key={w} className="ws-skel ws-skel-line" style={{ width: `${w}%` }} />)}
          <div className="ws-skel ws-skel-bubble short" />
          {[90, 72].map((w) => <div key={w} className="ws-skel ws-skel-line" style={{ width: `${w}%` }} />)}
        </>
      )}
    </div>
  );
}

// 新对话：欢迎语在输入框上面，下面是今日灵感，一起放在中间偏上（窄屏输入框贴底，这些在它上面）。
// 欢迎语（2026-11-07 Sean 定，照 Claude 网页版）：按这台设备上的钟点问候，带登录名字；每次打开新对话随机挑一句，停在页面上不换。
// 一半是这个钟点的话，一半是不分钟点的（回来啦这类）和周五、周末的。名字没有或太长就不带名字，不拿邮箱前缀凑。
// 游客（和没有登录功能的部署）不带名字，也不说“回来啦、接着来”这类当他来过的话。一句长了窄屏折成两行，两行差不多长
const NAME_MAX = 12;
const c = (n) => (n ? `，${n}` : ""); // “晚上好，Sean”：没名字就只有“晚上好”
const p = (n) => (n ? `${n}，` : ""); // “Sean，回来啦”：没名字就只有“回来啦”
// 每段钟点 [从几点, 到几点（不含）, 几句话]。一句话是一个小场景，不只是“下午好”（Sean 嫌短）。back：当他来过的话，游客不说
const GREET_SLOTS = [
  [5, 9, (n) => [{ t: `早上好${c(n)}，先来杯咖啡？` }, { t: `早${c(n)}，今天从哪儿开始？` }, { t: `新的一天${c(n)}，想先弄明白点什么？` }, { t: `起得真早${c(n)}` }]],
  [9, 12, (n) => [{ t: `上午好${c(n)}，今天想弄明白点什么？` }, { t: `咖啡续上了吗${c(n)}？` }, { t: `趁上午脑子清楚${c(n)}，来点难的？` }, { t: `${p(n)}上午的事多吗？先问我也行` }]],
  [12, 14, (n) => [{ t: `午饭吃了吗${c(n)}？` }, { t: `中午好${c(n)}，歇会儿也行，聊会儿也行` }, { t: `吃饱了${c(n)}？聊点轻松的` }, { t: `午休时间${c(n)}，想到什么就问` }]],
  [14, 18, (n) => [{ t: `下午好${c(n)}，来杯茶歇一歇？` }, { t: `下午容易犯困${c(n)}，聊点有意思的` }, { t: `下午茶时间${c(n)}，想聊点什么？` }, { t: `下午接着来${c(n)}`, back: true }]],
  [18, 23, (n) => [{ t: `晚上好${c(n)}，今天辛苦了` }, { t: `忙完了吗${c(n)}？聊点轻松的` }, { t: `晚上的时间${c(n)}，想琢磨点什么？` }, { t: `吃过晚饭了吗${c(n)}？` }]],
  [23, 29, (n) => [{ t: `夜深了${c(n)}，问完早点休息` }, { t: `还没睡呀${c(n)}？` }, { t: `夜猫子时间${c(n)}` }, { t: `这么晚还在想事情${c(n)}？` }]], // 23 点到第二天 5 点
];
// 不分钟点的，和周五、周末的
const greetAny = (n, day) => [
  { t: `${p(n)}回来啦，接着聊点什么？`, back: true },
  { t: `欢迎回来${c(n)}，今天想问什么？`, back: true },
  { t: `今天有什么想弄明白的${c(n)}？` },
  { t: `有什么新鲜事${c(n)}？` },
  ...(day === 5 ? [{ t: `周五了${c(n)}，周末有什么打算？` }] : []),
  ...(day === 0 || day === 6 ? [{ t: `周末愉快${c(n)}，今天想聊点什么？` }, { t: `周末也在忙呀${c(n)}？` }] : []),
];
function greeting(who, at, roll) {
  const guest = who === null;
  const name = !guest && who.trim().length <= NAME_MAX ? who.trim() : "";
  const h = at.getHours() < 5 ? at.getHours() + 24 : at.getHours();
  const ok = (x) => !(guest && x.back);
  const slot = GREET_SLOTS.find(([from, to]) => h >= from && h < to)[2](name).filter(ok);
  const rest = greetAny(name, at.getDay()).filter(ok);
  const pool = roll < 0.5 ? slot : rest;
  return pool[Math.floor(((roll * 2) % 1) * pool.length)].t;
}

// who：登录了是名字（没填是空字符串），游客是 null，还没认出是谁是 undefined（先空着占好高度，认出来再写，不先闪一句别的）
function Welcome({ who }) {
  const [at] = useState(() => ({ time: new Date(), roll: Math.random() }));
  return (
    <div className="ws-welcome">
      <h2>{who === undefined ? "\u00a0" : greeting(who, at.time, at.roll)}</h2>
    </div>
  );
}

// 今日灵感：Fusion 每天出的题（后端代取，只留新闻、调研、常识、天气、生图），大家看到的一样，每次打开新对话打乱顺序。
// 只有两行题，上面不放小标题、说明和“换一批”，也不放“能做的事”（2026-11-07 Sean 定）。放得下几个放几个；
// 每 15 秒一个接一个翻过去换下一批（批次 A 停过，2026-11-07 Sean 定恢复、改成 15 秒），鼠标放在上面、键盘停在上面时不翻。
// 系统设了减少动态效果：直接换，不翻。一行放不下的题折成两行，不截断。点一下直接发。取不到就显示固定的几个例子。取回来之前先放占位，出来时页面不跳
const ROTATE_MS = 15000;
const FLIP_MS = 300; // 和 .ws-inspire 里翻的过渡时长一致
const FLIP_STAGGER = 80;
const CHIP_GAP = 8; // 和 .ws-inspire 的 gap 一致
const ROWS = 2;

function shuffled(xs) {
  const a = [...xs];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// 从第 start 个往后（到头了接着从头来），两行放得下的那几个的序号；比一行还宽的独占一行（折成两行显示）
function fitRows(widths, start, room) {
  const out = [];
  let row = 0;
  let used = 0;
  for (let k = 0; k < widths.length; k++) {
    const i = (start + k) % widths.length;
    const w = Math.min(widths[i], room);
    if (used && used + CHIP_GAP + w > room) {
      if (++row >= ROWS) break;
      used = w;
    } else used = used ? used + CHIP_GAP + w : w;
    out.push(i);
  }
  return out;
}

function Inspirations({ onPick }) {
  const [pool, setPool] = useState(null); // {questions, today}；today 为假：没取到，用的固定例子
  const [start, setStart] = useState(0);
  const [size, setSize] = useState(null); // {room：能用多宽, widths：每个题多宽}
  const [flipped, setFlipped] = useState(false);
  const [hover, setHover] = useState(false);
  const [focused, setFocused] = useState(false);
  const box = useRef(null);
  const ruler = useRef(null); // 看不见的一排，用来量每个题多宽
  const timers = useRef([]);

  useEffect(() => {
    let on = true;
    listInspirations()
      .then((qs) => on && setPool(qs.length ? { questions: shuffled(qs), today: true } : { questions: EXAMPLES, today: false }))
      .catch(() => on && setPool({ questions: EXAMPLES, today: false }));
    return () => {
      on = false;
      timers.current.forEach(clearTimeout);
    };
  }, []);
  useLayoutEffect(() => {
    if (!pool) return undefined;
    const read = () => setSize({ room: box.current.clientWidth, widths: [...ruler.current.children].map((c) => c.getBoundingClientRect().width) });
    read();
    const ro = new ResizeObserver(read); // 窗口宽度变了重新排
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [pool]);

  const shown = pool && size?.room ? fitRows(size.widths, start, size.room) : [];
  const more = !!pool && shown.length > 0 && pool.questions.length > shown.length;
  const next = useRef(null);
  next.current = () => {
    if (!more) return; // 一屏就放得下全部：不翻
    const to = (start + shown.length) % pool.questions.length;
    timers.current.forEach(clearTimeout);
    timers.current = [];
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return setStart(to);
    setFlipped(true);
    timers.current.push(setTimeout(() => {
      setStart(to);
      timers.current.push(setTimeout(() => setFlipped(false), 50));
    }, shown.length * FLIP_STAGGER + FLIP_MS));
  };
  const paused = hover || focused;
  useEffect(() => {
    if (paused || !more) return undefined;
    const t = setTimeout(() => next.current(), ROTATE_MS); // 换了一批重新数
    return () => clearTimeout(t);
  }, [paused, more, start]);

  return (
    <section className="ws-examples" aria-label="新对话的起点">
      {/* 停翻只看两行题：外面这块往下撑满了页面，鼠标放在下面的空白处不算 */}
      <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        onFocus={() => setFocused(true)} onBlur={(e) => !e.currentTarget.contains(e.relatedTarget) && setFocused(false)}>
      <div className="ws-inspire-ruler" ref={ruler} aria-hidden="true">
        {pool?.questions.map((q) => <span key={q} className="ws-suggest-item">{q}</span>)}
      </div>
      <div className={"ws-inspire" + (flipped ? " flip" : "")} ref={box} aria-label={pool?.today ? "今日灵感" : "可以这样问"} role="group">
        {pool ? shown.map((i, n) => {
          const q = pool.questions[i];
          const d = `${n * FLIP_STAGGER}ms`; // 只有翻的两项错开，悬停变色不等
          return <button key={q} className="ws-suggest-item" style={{ transitionDelay: `${d}, ${d}, 0ms, 0ms` }} onClick={() => onPick(q)}>{q}</button>;
        }) : [112, 168, 136, 150].map((w) => <span key={w} className="ws-skel ws-skel-chip" style={{ width: w }} aria-hidden="true" />)}
      </div>
      </div>
    </section>
  );
}

// ---------------- 一轮一轮 ----------------

function Turns({ st, live, busy, suggesting, stopping, now, act, models, title }) {
  const turns = st?.turns || [];
  const running = busy || ACTIVE.has(st?.status);
  // 刚说的话还没出现在存下来的对话里：先按页面手上的显示
  const pending = live?.said && turns.length <= (live.turnsAtStart ?? 0) ? { id: "pending", user: live.said, items: [], record: null, local: live.image?.url } : null;
  const all = pending ? [...turns, pending] : turns;
  let shots = 0;
  // 每一轮一个 section：轮次目录跳过来的落点（data-turn 是第几轮，从 0 数；正在发的那句还没 id，按位置记），这一轮在跑时 aria-busy
  return (
    <>
      {all.map((t, i) => {
        const last = i === all.length - 1;
        const images = shots; // 这一轮之前画成了几张：下载的文件名按整段对话数第几张
        shots += imagesOf(t.items).filter(drawn).length;
        return (
          <section key={t.id} className="ws-turn" data-turn={i} tabIndex={-1} aria-label={`第 ${i + 1} 轮`} aria-busy={last && running ? true : undefined}>
            <Turn t={t} k={`${st?.thread_id}:${i}`} st={st} live={last && running ? live : null} running={last && running} suggesting={last && suggesting} stopping={stopping} last={last} now={now} act={act} models={models} title={title} images={images} />
          </section>
        );
      })}
    </>
  );
}

// k：这一轮在页面上的记号（对话 id + 第几轮）。正在发的这一轮存下来以后 id 会变（见 Turns 的 pending），过程展开没展开按 k 记，不跟着重来
function Turn({ t, k, st, live, running, suggesting, stopping, last, now, act, models, title, images }) {
  const items = mergeItems(t.items, live);
  const { process, answer } = splitTurn(items);
  const texts = items.filter((i) => i.type === "text").map((i) => i.text);
  const nums = citeNumbers(texts, st?.sources);
  // 过程里还有子任务卡：它们的报告引用的来源接着往下编号。这一轮的文字排前面，正文的编号和底下的来源列表不变
  const procNums = citeNumbers([...texts, ...subTexts(items)], st?.sources);
  const rec = t.record;
  const idle = !running && st?.status === "idle";
  const failed = last && !running && st?.status === "failed";
  const replied = answer.some((i) => i.type === "text");
  return (
    <>
      {t.user && <UserMsg text={live?.edit ?? t.user} image={t.image} src={userImageSrc(t, live, st?.thread_id)} at={live?.sentAt ?? t.sent_at} pinned={last} now={now}
        editable={last && idle} onEdit={act.edit} />}
      <div className="ws-ai">
        <div className="ws-ai-head"><span className="ws-ai-mark" aria-hidden="true">灵</span>灵稿</div>
        {(running || rec) && <Process k={k} items={process} st={st} nums={procNums} rec={rec} running={running} failed={failed} now={now} />}
        {forecastsOf(items).map((fc) => <WeatherCard key={fc.location} fc={fc} />)}
        {mapsOf(items).map((card) => <MapCard key={card.key} card={card} />)}
        {travelOf(items).map((card) => <TravelCard key={card.key} card={card} />)}
        {itinerariesOf(items).map((card) => <ItineraryCard key={card.key} card={card} />)}
        <ImageCards steps={imagesOf(items)} tid={st?.thread_id} running={running} onRegenerate={last && idle ? act.say : null} title={title} base={images} />
        {answer.map((it, k) => <Item key={`${it.type}:${it.id}`} it={it} st={st} nums={nums} running={running} lastItem={k === answer.length - 1} />)}
        {running && <LiveLine live={live} items={items} stopping={stopping} now={now} since={Date.parse(st?.running_since || rec?.started_at || "") || null} />}
        {!running && <AnswerSources nums={nums} sources={st?.sources} />}
        {!running && rec?.ended_at && rec.outcome === "no_reply" && <p className="ws-muted">这一轮没有给出文字回复。</p>}
        {failed && <Failed st={st} act={act} />}
        {!running && (replied || rec?.ended_at || (last && idle)) && (
          <div className={"ws-ai-foot" + (last ? "" : " quiet")}>
            <Stamp iso={rec?.ended_at} now={now} />
            {rec?.model && models?.length > 0 && <span className="ws-ai-model">{modelName(models, rec.model.ref)}</span>}
            {rec?.credits != null && <span className="ws-ai-model">本轮 {creditsText(rec.credits)}</span>}
            {replied && <CopyButton text={() => answerText(items, st?.sources)} />}
            {last && idle && <button className="ws-btn ghost sm" onClick={act.regenerate}><IconLoop size={13} />重新回答</button>}
          </div>
        )}
      </div>
      {last && idle && rec?.suggestions?.length > 0 && <Suggestions items={rec.suggestions} onPick={(q) => act.say(q)} />}
      {last && idle && suggesting && !rec?.suggestions?.length && <SuggestWait />}
    </>
  );
}

// 用户说的话：下面是发送时间和“复制”，最后一句在空闲时还有“编辑”。都一直显示，pinned（最后一轮）以外的淡一档
// 临时的输入框（改这句话、左栏改名）关掉以后，光标回到对话的输入框（等它从页面上撤掉再放）
export const focusInput = () => setTimeout(() => dispatchEvent(new Event("ws-focus-input")));

// 这句话带的图从哪儿取：刚发出去、还没存下来的用本机的；发出去的这一轮记下本机地址，之后接着用（不再从服务器取一遍）；别的从后端取
function userImageSrc(t, live, tid) {
  if (t.local) return t.local;
  if (!t.image || !tid) return null;
  if (live?.image?.url && !sentImages.has(t.image.id)) sentImages.set(t.image.id, live.image.url);
  return sentImages.get(t.image.id) || imageUrl(tid, t.image);
}

// 用户说的话带的图：气泡上面一张缩略图（不超过 240px 见方），点开看大图；图已经不在了（删掉的）说一声
const THUMB = 240;
function UserImage({ img, src }) {
  const [failed, setFailed] = useState(false);
  const [viewing, setViewing] = useState(null);
  const k = img?.width && img?.height ? Math.min(THUMB / img.width, THUMB / img.height, 1) : 0;
  const box = k ? { width: Math.round(img.width * k), aspectRatio: `${img.width} / ${img.height}` } : undefined;
  return (
    <div className="ws-user-img" style={box}>
      {failed ? <span className="ws-user-img-gone">图片取不到了</span> : (
        <button className="ws-user-img-open" aria-label="看大图" onClick={(e) => setViewing(e.currentTarget)}>
          <img src={src} alt="发的图" onError={() => setFailed(true)} />
        </button>
      )}
      {viewing && <Viewer img={{ alt: "发的图" }} src={src} onClose={() => { viewing.focus(); setViewing(null); }} />}
    </div>
  );
}

function UserMsg({ text, image, src, at, pinned, now, editable, onEdit }) {
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editable) setEditing(false); }, [editable]); // 开着编辑框时这一轮又跑起来了（点了重新回答、推荐问题），编辑框就收起
  if (editing && editable) return <EditBox text={text} onCancel={() => { setEditing(false); focusInput(); }} onSend={(t) => { setEditing(false); onEdit(t); }} />;
  return (
    <div className={"ws-user" + (pinned ? " pinned" : "")}>
      {src && <UserImage key={src} img={image} src={src} />}
      <div className="ws-bubble">{text}</div>
      <div className={"ws-user-foot" + (pinned ? " pinned" : "")}>
        <Stamp iso={at} now={now} />
        <CopyButton text={() => text} />
        {editable && <button className="ws-btn ghost sm" onClick={() => setEditing(true)}><IconEdit size={13} />编辑</button>}
      </div>
    </div>
  );
}

// 还没发出去的话（最多一句，见 Composer 的 held），贴在输入框上沿：排着的（这一轮答完就发，可以改成插进这一轮），或者等着插进这一轮的。
// 撤回就放回输入框，可以改了再发。按钮按下去不抢光标，输入框里正写着的接着写
function Pending({ st, act }) {
  const rows = [
    st?.guided_message && { key: "guided", text: st.guided_message.text, note: "做完手上这一步就插进这一轮", onWithdraw: act.unguide },
    st?.queued_message && { key: "queued", text: st.queued_message.text, note: "这一轮答完就发", onGuide: act.guide, onWithdraw: act.unqueue },
  ].filter(Boolean);
  if (!rows.length) return null;
  const keep = (e) => e.preventDefault();
  return (
    <div className="ws-pending" aria-label="还没发出去的话">
      {rows.map((r) => (
        <div key={r.key} className="ws-pending-row">
          <div className="ws-pending-main">
            <div className="ws-pending-note">{r.note}{!touch() && <span className="key" aria-hidden="true">，输入框最前面按 <kbd className="ws-kbd">↑</kbd>也能撤回</span>}</div>
            <div className="ws-pending-text" title={r.text}>{r.text}</div>
          </div>
          <div className="ws-pending-acts">
            {r.onGuide && <button className="ws-btn ghost sm" title="不等这一轮答完：模型做完手上这一步就看到这句话，接着按它调整" onMouseDown={keep} onClick={r.onGuide}><IconInsert size={13} />插进这一轮</button>}
            <button className="ws-btn ghost sm" title="放回输入框，改了再发（光标在输入框最前面时按 ↑ 也行）" onMouseDown={keep} onClick={r.onWithdraw}><IconUndo size={13} />撤回</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// 改最后一句：回车发送，Shift+回车换行，Esc 取消；没改动不能发
function EditBox({ text, onCancel, onSend }) {
  const [v, setV] = useState(text);
  const ref = useRef(null);
  useAutoHeight(ref, v);
  useEffect(() => {
    const el = ref.current;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  const changed = v.trim() && v.trim() !== text.trim();
  const send = () => changed && onSend(v.trim());
  return (
    <div className="ws-edit">
      <textarea ref={ref} rows={1} value={v} data-esc aria-label="改这句话" onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
          else if (e.key === "Escape") onCancel();
        }} />
      <div className="ws-edit-bar">
        <span className="hint">发送后按新的话重新回答，这一轮原来的回答会被替换</span>
        <button className="ws-btn ghost sm" onClick={onCancel}>取消</button>
        <button className="ws-btn primary sm" disabled={!changed} onClick={send}>发送</button>
      </div>
    </div>
  );
}

// 发送或回答完的时间，悬停看精确到秒
function Stamp({ iso, now }) {
  const s = stamp(iso, now);
  return s && <time className="ws-time" dateTime={iso} title={s.full}>{s.text}</time>;
}

// 复制：点了以后显示“已复制”一会儿。text 是函数，点的时候才算要复制的文字
function CopyButton({ text }) {
  const [done, setDone] = useState(null); // null / "ok" / "fail"
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await copyText(text());
      setDone("ok");
    } catch {
      setDone("fail");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setDone(null), 1500);
  };
  return (
    <button className="ws-btn ghost sm" onClick={copy} aria-live="polite">
      {done === "ok" ? <IconCheck size={13} /> : <IconCopy size={13} />}
      {done === "ok" ? "已复制" : done === "fail" ? "没复制成" : "复制"}
    </button>
  );
}

// 输入框跟着字数长高，最高 160px，再多就在框里滚
function useAutoHeight(ref, value) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [ref, value]);
}

// 这一轮的过程：一行“已完成 / 已停止 / 已中断 / 中途停下 + 用时 + 做了什么”，点开看每一步。正在跑时展开，跑完不自动收
function Process({ k, items, st, nums, rec, running, failed, now }) {
  const [open, toggle, found] = useFoldOpen(`proc:${k}`, running); // 正在跑、这次打开页面里跑过的展开；重新打开的对话收起
  const shown = open && items.length > 0;
  const time = running ? elapsed(st?.running_since || rec?.started_at, now)
    : rec?.ended_at ? elapsed(rec.started_at, Date.parse(rec.ended_at)) : "";
  const verb = running ? "处理中" : failed ? "中途停下" : rec?.outcome === "stopped" ? "已停止" : rec?.outcome === "blocked" ? "已中断" : "已完成";
  const did = running ? "" : didText(items);
  const label = `${verb}${time ? ` ${time}` : ""}${did ? ` · ${did}` : ""}`;
  return (
    <div className="ws-proc">
      {items.length
        ? <button className="ws-proc-head" aria-expanded={shown} onClick={toggle}>{label}<IconChevron size={13} className={"chev" + (shown ? " up" : "")} /></button>
        : <div className="ws-proc-head">{label}</div>}
      {items.length > 0 && <Fold open={shown} onFound={found}><div className="ws-proc-body"><ProcItems items={items} st={st} nums={nums} running={running} /></div></Fold>}
    </div>
  );
}

// 收起来那一行带上做了什么：搜了几次、读了几页（子任务里的也算），开了几个子任务；做成了的才算
function didText(items) {
  let search = 0, read = 0, tasks = 0;
  const count = (xs) => xs.forEach((i) => {
    if (i.type !== "step" || i.pending) return;
    if (i.kind === "task") { tasks++; count(i.items || []); }
    else if (i.kind === "search" && i.ok) search++;
    else if (i.kind === "read" && i.body) read++; // 和 ToolRow 一样：读到原文的才算
  });
  count(items);
  return [search && `搜了 ${search} 次`, read && `读了 ${read} 页`, tasks && `${tasks} 个子任务`].filter(Boolean).join(" · ");
}

// 过程里的一条条：思考、中途说的话、工具步骤，子任务是一张卡片。子任务卡里是它的子 agent 的这些，排法一样。
// hideUnknown：子 agent 还在跑，它查到的来源要等它做完才进这一轮的来源表，还对不上的引用先不显示（不然先冒一个问号）
function ProcItems({ items, st, nums, running, hideUnknown = false }) {
  const cite = (sid) => (hideUnknown && !st?.sources?.[sid] ? null : <Mark n={nums[sid]} s={st?.sources?.[sid]} nums={nums} sources={st?.sources} />);
  return items.map((it) => (it.type === "step" ? (it.kind === "task" ? <TaskCard key={`step:${it.id}`} s={it} st={st} nums={nums} running={running} />
    : <ToolRow key={`step:${it.id}`} s={it} running={running} />)
    : it.type === "thinking" ? <Thinking key={`thinking:${it.id}`} it={it} running={running} thinking={running && it.live && it.ms == null} />
      : it.type === "blocked" ? <div key={`blocked:${it.id}`} className="ws-tool bad"><IconWarn size={12} /><span className="main">内容审核没通过，这个子任务停在这里</span></div>
        : it.type === "guide" ? <GuideNote key={`guide:${it.id}`} it={it} />
          : <ProcText key={`text:${it.id}`} it={it} cite={cite} />));
}

// 用户插进这一轮的话：模型从这里开始看到它
function GuideNote({ it }) {
  return (
    <div className="ws-guide">
      <span className="ws-guide-tag">你插了一句</span>
      <div className="ws-guide-text">{it.text}</div>
    </div>
  );
}

// 子任务卡里报告和中途说的话：给它们的引用编号用
const subTexts = (items) => items.flatMap((i) => (i.type === "step" && i.kind === "task" ? (i.items || []).filter((x) => x.type === "text").map((x) => x.text) : []));

// 子任务：主 agent 派给一个子 agent 去查的事。卡片一行是任务标题和它现在在做什么，做完了是搜了几次、读了几页；
// 点开是交代给它的话，和它的思考、搜索、读页、交回的报告。默认收着：几个子 agent 并行时，展开的过程太长
function TaskCard({ s, st, nums, running }) {
  const [open, toggle, found] = useFoldOpen(`task:${s.id}`, false); // 默认收着，卡头一行说它在做什么
  const sub = s.items || [];
  const live = running && s.pending;
  const done = (k) => sub.filter((i) => i.type === "step" && i.kind === k && !i.pending).length;
  const did = [done("search") && `搜了 ${done("search")} 次`, done("read") && `读了 ${done("read")} 页`].filter(Boolean).join("、");
  const status = live ? taskActivity(s) : s.pending ? "没有完成" : !s.ok ? s.message || "没做成" : did || "已完成";
  return (
    <div className={"ws-task" + (live ? " live" : !s.pending && !s.ok ? " bad" : "")}>
      <button className="ws-task-head" aria-expanded={open} onClick={toggle}>
        {live ? <Spinner size={12} /> : <IconFork size={12} />}
        <span className="main" title={s.text}>{s.text || "子任务"}</span>
        <span className="sub" title={status}>{status}</span>
        <IconChevron size={13} className={"chev" + (open ? " up" : "")} />
      </button>
      <Fold open={open} onFound={found}>
        <div className="ws-task-body">
          {s.prompt && <div className="ws-task-prompt">{s.prompt}</div>}
          <ProcItems items={sub} st={st} nums={nums} running={live} hideUnknown={live} />
        </div>
      </Fold>
    </div>
  );
}

// 子 agent 现在在做什么：在搜、在读、在想、在写报告，都没有就看它在等什么（排队、等模型……）
const TASK_PHASES = {
  queued: "排队中，前面的子任务做完就开始",
  model: "等待模型响应…",
  retry: "工具参数没写对，让模型重写…",
  guard: "部分资料没通过内容审核，去掉后重试…",
  restart: "模型中途断了，正在重新生成…",
};

function taskActivity(s) {
  const sub = s.items || [];
  const pending = sub.filter((i) => i.type === "step" && i.pending);
  const more = pending.length > 1 ? ` 等 ${pending.length} 项` : "";
  const p = pending.at(-1);
  if (p) return p.kind === "read" ? `正在读 ${p.title || domain(p.text)}${more}` : `正在搜「${p.text}」${more}`;
  const last = sub.at(-1);
  if (last?.live && last.type === "text") return "正在写报告…";
  if (last?.live && last.type === "thinking" && last.ms == null) return "思考中…";
  return TASK_PHASES[s.phase] || "准备中…";
}

// 被模型服务的内容审核拦下、这一轮停了：说明是固定的，原因来自后端（这一轮从哪里被拦）
const BLOCKED_TEXT = {
  input: "这句话没通过模型服务的内容审核，这一轮停在这里。换个说法再说一次。",
  material: "这一轮查到的资料或写出的内容没通过模型服务的内容审核，这一轮停在这里。可以换个问法再试。",
  output: "模型的回答没通过内容审核，被中断了，这一轮停在这里。可以换个问法再试。",
};

function Item({ it, st, nums, running, lastItem }) {
  if (it.type === "blocked") {
    return (
      <div className="ws-card warn">
        <div className="ws-card-title"><IconWarn size={15} />内容审核没通过</div>
        <div className="ws-card-sub">{BLOCKED_TEXT[it.reason] || BLOCKED_TEXT.input}这一轮不会带进之后的对话。</div>
      </div>
    );
  }
  return <Reply it={it} cite={(sid) => <Mark n={nums[sid]} s={st?.sources?.[sid]} nums={nums} sources={st?.sources} />} caret={running && lastItem} />;
}

// 正文：收到多少显示多少（后端约每 80ms 来一批），不再人为地一个个字往外放；正在写的时候末尾有光标
function Reply({ it, cite, caret }) {
  return (
    <div className={"ws-reply" + (caret ? " typing" : "")}>
      <Markdown text={mdText(it)} cite={cite} />
    </div>
  );
}

// 过程里中途说的话（之后又去搜了、读了），一样收到就显示
function ProcText({ it, cite }) {
  return <div className="ws-proc-text"><Markdown text={mdText(it)} cite={cite} /></div>;
}

// 还在写的，最后一行半截的 Markdown 先收拾一下（见 tidyTyping）；写完的只去掉末尾半个引用
const mdText = (it) => (it.live ? tidyTyping(it.text) : cutPartialCite(it.text));

// 思考：收到就显示。这一轮在跑时展开，想完的那段不收（跑着的时候收起来页面会往回缩、跳一下）；这一轮跑完就收成一行（过程不收）：
// 思考往往很长，开着把回答推远。重新打开的对话里也收着，点开看原文
function Thinking({ it, running, thinking }) {
  const text = it.text;
  const [shown, toggle, found] = useFoldOpen(`thinking:${it.id}`, running, false);
  const box = useRef(null);
  useLayoutEffect(() => { // 正在想的时候框里跟着最新的字
    if (thinking && box.current) box.current.scrollTop = box.current.scrollHeight;
  });
  const secs = it.ms != null ? Math.max(1, Math.round(it.ms / 1000)) : null;
  const label = thinking ? "深度思考中" : secs ? `思考了 ${secs} 秒` : "思考过程";
  return (
    <div className={"ws-think" + (thinking ? " live" : "")}>
      <button className="ws-think-head" aria-expanded={shown} onClick={toggle}>
        {thinking ? <Spinner size={12} /> : <IconChevron size={13} className={"chev" + (shown ? " up" : "")} />}
        <span>{label}</span>
      </button>
      <Fold open={shown} onFound={found}><div ref={box} className="ws-think-text">{text}</div></Fold>
    </div>
  );
}

// 还没有结果的一步（pending）：这一轮在跑就是还在做；没在跑了（这一轮出错停在这一步）就是没有完成，和子任务卡一样说
function ToolRow({ s, running }) {
  const Icon = { read: IconPage, weather: IconCloudSun, place: IconPin, route: IconRoute, flight: IconPlane, train: IconTrain, itinerary: IconTicket,
    image: IconImage, docs: IconBook }[s.kind] || IconSearch;
  const busy = running && s.pending;
  let main, sub;
  let bad = !s.ok;
  if (s.kind === "read") {
    main = <>读 <a href={s.text} target="_blank" rel="noreferrer" title={s.text}>{s.title || domain(s.text)}</a></>;
    sub = s.pending ? "读取中…" : s.body ? "读到原文" : `没读成${s.message ? `：${s.message}` : ""}`;
    bad = !s.pending && !s.body;
  } else if (s.kind === "weather") {
    main = <>查天气「{s.text}」</>;
    sub = s.pending ? "查询中…" : s.ok ? s.title : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "place") {
    main = <>查地点「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    const n = s.places?.places.length;
    sub = s.pending ? "查询中…" : s.places ? (n ? `${n} 个地方` : "没找到") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "route") {
    main = <>查路线「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    const got = s.routes?.routes.map((r) => MODES[r.mode] || r.mode).join("、");
    sub = s.pending ? "计算中…" : s.routes ? (got ? `算出${got}` : "没算出来") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "flight" || s.kind === "train") {
    main = <>{s.kind === "flight" ? "查航班" : "查火车"}「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    const n = s.travel?.services.length;
    sub = s.pending ? "查询中…" : s.travel ? (n ? `${n} 个班次` : "没查到班次") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "itinerary") {
    main = <>排行程「{s.text}」</>;
    sub = s.pending ? "排列中…" : s.itinerary ? `${s.itinerary.plans.length} 个方案` : "没排成";
  } else if (s.kind === "library") { // 查文档前先找库（Context7）
    main = <>找文档库「{s.text}」</>;
    sub = s.pending ? "查找中…" : s.ok ? (s.hits ? `${s.hits} 个候选` : "没找到") : `没找成：${s.message || "出错了"}`;
  } else if (s.kind === "docs") { // 灰色小标是哪个库
    main = <>查文档「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    sub = s.pending ? "查询中…" : s.ok ? (s.hits ? `${s.hits} 段文档` : "没有带出处的文档") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "image") {
    main = <>{s.edit ? "改图" : "画图"}「{s.text}」</>;
    sub = s.pending ? "生成中…" : s.image ? "已生成" : `没画成：${s.message || "出错了"}`;
  } else {
    main = <>搜索「{s.text}」{s.limits.map((l) => <span key={l} className="lim">{l}</span>)}</>;
    sub = s.pending ? "搜索中…" : s.ok ? `${s.hits} 条结果${s.message ? `（${s.message}）` : ""}` : `没搜成：${s.message || "出错了"}`;
  }
  if (s.pending && !busy) {
    sub = "没有完成";
    bad = false;
  }
  return (
    <div className={"ws-tool" + (busy ? " pending" : bad ? " bad" : "")}>
      {busy ? <Spinner size={12} /> : <Icon size={12} />}
      <span className="main" title={s.text}>{main}</span>
      {sub && <span className="sub" title={sub}>{sub}</span>}
    </div>
  );
}

// 空档里在等什么：后端在调模型之前发的阶段（见 app/graph/research/agent.py）
const PHASES = {
  model: "等待模型响应…",
  retry: "工具参数没写对，让模型重写…",
  guard: "部分资料没通过内容审核，去掉后重试…",
  restart: "模型中途断了，正在重新生成…",
};

// 在等的那一步是什么（并行的几步里先做完的排在后面也算还在等；有子任务在查，先说子任务）
const WAITING = [
  ["task", (n) => `${n} 个子任务在查`],
  ["image", () => "正在画图"],
  ["read", (n) => (n > 1 ? `正在读 ${n} 个网页` : "正在读网页")],
  ["search", (n) => (n > 1 ? `正在搜 ${n} 组关键词` : "正在搜索")],
  ["docs", () => "正在查文档"],
  ["library", () => "正在找文档库"],
  ["weather", () => "正在查天气"],
  ["place", () => "正在查地点"],
  ["route", () => "正在算路线"],
  ["flight", () => "正在查航班"],
  ["train", () => "正在查火车"],
  ["itinerary", () => "正在排行程"],
];
const SHOW_WAIT = 3000; // 同一步等了这么久才写秒数，一闪而过的不写
const SLOW = 30000; // 同一步这么久没有新进展：说一声比平常慢，可以接着等，也可以停止

// 正在跑的这一轮最下面一行：在等什么、等了几秒（思考、正文在出字时这一行空着，它们自己在动）。
// 不是读屏的活区：阶段一变就播会打断，开始、答完由 useAnnounce 说
function LiveLine({ live, items, stopping, now, since }) {
  const born = useRef(Date.now()); // 取不到这一轮的开始时间（刚发出去、状态还没回来）就从这行出现算
  const lastItem = items.at(-1);
  const pending = items.filter((i) => i.type === "step" && i.pending);
  const streaming = lastItem?.live && (lastItem.type === "text" || (lastItem.type === "thinking" && lastItem.ms == null));
  const kind = WAITING.find(([k]) => pending.some((i) => i.kind === k));
  const stage = stopping ? "正在停止…" : live?.stage || (kind ? kind[1](pending.filter((i) => i.kind === kind[0]).length)
    : streaming ? null : PHASES[live?.phase] || "正在准备…");
  // 看得到的进展：条目多了、字长了、子任务里有动静、阶段变了。变了就重新计时
  const sub = pending.filter((i) => i.kind === "task").map((i) => `${i.items?.length ?? 0}.${i.items?.at(-1)?.text?.length ?? 0}.${i.phase}`).join(",");
  const waited = useWaited(`${items.length}:${lastItem?.text?.length ?? 0}:${pending.length}:${live?.phase}:${stage}:${sub}`, now);
  const secs = stage && !stopping && waited >= SHOW_WAIT ? Math.floor(waited / 1000) : 0;
  return (
    <div className="ws-live">
      {stage && <><Spinner size={14} /><span className="stage">{stage}</span></>}
      {secs > 0 && <span className="secs">{secs} 秒</span>}
      {secs > 0 && !touch() && <span className="key" aria-hidden="true"><kbd className="ws-kbd">Esc</kbd>停止</span>}
      {secs * 1000 >= SLOW && <span className="slow">这一步比平常慢，可以接着等，也可以停止</span>}
      {!stopping && <NotifyToggle since={since ?? born.current} now={now} />}
    </div>
  );
}

// sig 是“看得到的进展”：变了就重新计时，返回没变的这段已经多久。这一轮在跑时页面每秒重新渲染一次（见 useNow）
function useWaited(sig, now) {
  const mark = useRef({ sig: null, at: 0 });
  if (mark.current.sig !== sig) mark.current = { sig, at: Date.now() };
  return Math.max(0, now - mark.current.at);
}

// 正文写完以后在起“接着问”：起好了换成那几个问题，没起成（或用户先说了话）就不显示
function SuggestWait() {
  return <div className="ws-suggest-wait"><Spinner size={12} /><span>正在想接着问什么…</span></div>;
}

// 接着问：和新对话页的今日灵感同一种胶囊（横排、放不下换行），名字分开（规范术语表，2026-11-06 Sean 定）
function Suggestions({ items, onPick }) {
  return (
    <div className="ws-suggest" role="group" aria-labelledby="ws-suggest-label">
      <div className="ws-suggest-label" id="ws-suggest-label">接着问</div>
      <div className="ws-suggest-list">
        {items.map((q) => <button key={q} className="ws-suggest-item" onClick={() => onPick(q)}>{q}</button>)}
      </div>
    </div>
  );
}

// 这一轮中途停下了：原因和能做什么按后端给的类别说（见 errors.js 的 failureText）
function Failed({ st, act }) {
  return (
    <div className="ws-card warn">
      <div className="ws-card-title"><IconWarn size={15} />这一轮中途停下了</div>
      <div className="ws-card-sub">{failureText(st)}</div>
      <div className="ws-card-actions"><button className="ws-btn primary" onClick={act.retry}><IconRetry size={14} />重试</button></div>
    </div>
  );
}

// ---------------- 输入框 ----------------

const PLACEHOLDER = {
  new: "想问什么？",
  opening: "正在打开对话…",
  idle: "接着问…",
  busy: "正在回答，最多先排一句，这一轮答完就发",
  queued: QUEUED,
  failed: "这一轮没有完成，点上面的“重试”",
};
const HELD = "上面那句发出去以后才能再发，也可以撤回它一起改";
const WITH_IMAGE = "说说要怎么改这张图，或照着它画什么";
const IMAGE_WAITS = "带图的话要等这一轮答完才能发";
const COUNT_FROM = 2000; // 超过这么多字在输入框里显示字数；不设上限（后端也没有），2026-11-06 Sean 定

// 额度快用完（用了八成）或用完了时，输入框上面提醒一句（见后端 app.quota，按最紧的那个额度）：和左下角额度面板同一种说法（creditsLine）；
// 游客再带上登录入口和登录后会怎样（loginLine）
function CreditsNote({ credits, onLogin }) {
  const left = creditsLeft(credits);
  if (!left?.warn) return null;
  const after = onLogin && loginLine(credits, false);
  return (
    <div className={"ws-credits-note" + (left.exhausted ? " out" : "")} role="status">
      <IconWarn size={13} />
      <span>{left.exhausted ? "" : "额度快用完了："}{creditsLine(left)}{after && <>。<button className="ws-link" onClick={onLogin}>登录</button>{after}</>}</span>
    </div>
  );
}

// 每个对话选好、还没发的图（{file, url}，见 upload.js）：存不进本机，只在这次打开的页面里留着——切对话再回来还在，刷新就没了
const attachments = new Map();
const keepImage = (key, att) => (att?.url ? attachments.set(key, att) : attachments.delete(key));
// 发出去的图在本机的地址（图的编号 → blob 地址）：存下来的对话回来以后接着用它显示，不用再从服务器取一遍
const sentImages = new Map();

// 回答时（queueing）也能打字：停止键一直在原位，不变成别的；打了字，旁边另外出一个“这一轮答完再发”，话排着，这一轮答完就发。Esc 停止回答。
// held：已经有一句没发出去（排着或等着插进这一轮）。没发出去的话最多一句：这时还能打字，但发不出去，要先撤回那句、改好一起发。
// 带图的话排不了（排着的话只有字），要等这一轮答完再发。
// 图：点左下角选、粘贴、拖进来都行，一次一张（再选换掉），大图在本机先压到 4 MB 以内（见 upload.js）。
// 没发出去的字每个对话各存一份在本机（见 drafts.js）：切走、刷新、登录回来都在，发出去就清掉；图只在这次打开的页面里留着（见 attachments）。
// carry：新对话刚发出去、拿到了 id，thread 从 "new" 换成 id，还是同一个对话
// putBack：放回输入框的话（撤回的、没发出去的，可能带着图）：放在已经写着的字前面、空一行，放回来的那段选中，看得出哪段是放回来的
//   （被拒的那句还原样留着就换掉它，不叠加）；不是正在看的对话，放进那个对话的草稿
// onTakeBack：有没发出去的话时，光标在最前面（输入框空着也算）按 ↑ 把它们取回来。光标在最前面时 ↑ 本来什么也不做，不和挪光标抢
function Composer({ thread, carry, mode, queueing, held, stopping, putBack, onSend, onStop, above, onTakeBack, tools }) {
  const [text, setText] = useState(() => getDraft(thread));
  const [image, setImage] = useState(() => attachments.get(thread) || null); // {file, url}；还在处理是 {pending: true}
  const [imageError, setImageError] = useState(null);
  const [drag, setDrag] = useState(false);
  const ref = useRef(null);
  const fileRef = useRef(null);
  const shown = useRef(thread); // text、image 是哪个对话的
  const textRef = useRef(text);
  textRef.current = text;
  const imageRef = useRef(image);
  imageRef.current = image;
  const attachSeq = useRef(0); // 每选一次图、去掉一次加一：处理慢的那张回来时发现对不上，就不要了
  const selectNext = useRef(null); // 放回来的话要选中的那段：[起, 止]，字放进去以后再选
  const sendNow = mode === "new" || mode === "idle";
  const canStop = mode === "busy" || mode === "queued";
  const hasText = !!text.trim();
  const canQueue = queueing && !held && !image;
  const enabled = sendNow || (queueing && !held); // 能说话了（光标该回来了）
  const stopNow = useRef(null); // 按 Esc 时调的：能停才有
  stopNow.current = canStop && !stopping ? onStop : null;
  useAutoHeight(ref, text);
  useLayoutEffect(() => {
    const r = selectNext.current;
    selectNext.current = null;
    if (r) ref.current?.setSelectionRange(r[0], r[1]);
  }, [text]);

  // 存草稿：打字停 300ms 存一次；换对话、页面要关（pagehide）、输入框撤掉（去海报页）时马上存
  const saveTimer = useRef(null);
  const save = () => {
    clearTimeout(saveTimer.current);
    setDraft(shown.current, textRef.current);
  };
  useEffect(() => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(save, 300);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  useEffect(() => {
    addEventListener("pagehide", save);
    return () => {
      removeEventListener("pagehide", save);
      save();
      keepImage(shown.current, imageRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 光标放回输入框（批次 G 第 42 条，2026-11-07 Sean 定）：
  // - 触屏上一律不放，不自己弹出键盘；
  // - 桌面上打开页面、新对话、用鼠标点开一段对话一定放；用键盘选的对话不放，焦点留在左栏那一行；
  // - 能说话了、按钮点完焦点掉到页面上时，光标不在别处、也没选中文字才放；切回这个窗口不动光标。
  const focus = (always) => {
    const el = ref.current;
    const at = document.activeElement;
    if (!el || at === el || touch()) return;
    if (!always && ((at && at !== document.body) || getSelection()?.toString())) return;
    el.focus({ preventScroll: true });
  };
  useEffect(() => {
    const was = shown.current;
    shown.current = thread;
    if (was === thread) {
      focus(true);
      return;
    }
    const keyed = byKeyboard() && thread !== "new";
    clearTimeout(saveTimer.current);
    if (was === "new" && carry) { // 同一个对话：等回答时写的字、选的图留着，光标也不动；草稿挪到这个对话名下
      setDraft("new", "");
      setDraft(thread, textRef.current);
      return;
    }
    setDraft(was, textRef.current);
    keepImage(was, imageRef.current);
    setText(getDraft(thread));
    setImage(attachments.get(thread) || null);
    setImageError(null);
    if (!keyed) focus(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread]);
  useEffect(() => { if (enabled) focus(false); }, [enabled]);
  // 被拒（额度用完等）放回来的话记一下：下一句又被拒时，输入框里还是原样的那句就换掉，不叠加（2026-11-06 Sean 发现额度用完连点推荐问题会一句句堆起来）
  const rejected = useRef(null);
  useEffect(() => {
    if (!putBack) return;
    const prev = rejected.current;
    rejected.current = putBack.rejected ? putBack.text : null;
    const joined = (cur) => !!cur.trim() && !(putBack.rejected && cur === prev);
    const join = (cur) => (joined(cur) ? `${putBack.text}\n\n${cur}` : putBack.text);
    if (putBack.thread !== shown.current) {
      setDraft(putBack.thread, join(getDraft(putBack.thread)));
      if (putBack.image) attachments.set(putBack.thread, putBack.image);
      return;
    }
    const cur = textRef.current;
    const next = join(cur);
    selectNext.current = joined(cur) ? [0, putBack.text.length] : [next.length, next.length];
    setText(next);
    if (putBack.image) {
      attachSeq.current += 1;
      setImage(putBack.image);
    }
    focus(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [putBack]);
  useEffect(() => {
    // 图上点了“改这张”（见 images.jsx）：当成选了这张图，桌面上光标进输入框好接着说
    const take = (e) => {
      if (!e.detail) return setImageError("没取到这张图，再点一次“改这张”");
      attachRef.current(e.detail);
      focus(true);
    };
    addEventListener("ws-attach-image", take);
    return () => {
      removeEventListener("ws-attach-image", take);
    };
  }, []);
  useEffect(() => {
    const back = () => focus(false);
    // 光标在输入框里、或者没在哪儿（点了正文）时才停；在改名、菜单、角标这些地方，Esc 归它们自己。
    // 在捕获阶段看：等它们处理完，改名框已经撤掉，光标就不在那儿了
    const esc = (e) => {
      const at = document.activeElement;
      if (e.key !== "Escape" || e.isComposing || !stopNow.current) return;
      if (at && at !== document.body && at !== ref.current) return;
      stopNow.current();
    };
    addEventListener("ws-focus-input", back); // 见 focusInput
    addEventListener("keydown", esc, true);
    return () => {
      removeEventListener("ws-focus-input", back);
      removeEventListener("keydown", esc, true);
    };
  }, []);

  // 选了、粘贴了、拖进来一张图：先占上位置转圈，处理好（大图压缩）再显示；处理的时候换了对话，处理好的图归原来那个对话
  const attach = async (file) => {
    if (!file) return;
    const key = shown.current;
    const seq = ++attachSeq.current;
    setImageError(null);
    setImage({ pending: true });
    try {
      const f = await prepareImage(file);
      if (seq !== attachSeq.current) return;
      const att = { file: f, url: URL.createObjectURL(f) };
      if (shown.current === key) setImage(att);
      else attachments.set(key, att);
    } catch (e) {
      if (seq !== attachSeq.current || shown.current !== key) return;
      setImage(null);
      setImageError(e instanceof ImageError ? e.message : "这张图读不出来，换一张再试");
    }
  };
  const attachRef = useRef(attach);
  attachRef.current = attach;
  const removeImage = () => {
    attachSeq.current += 1;
    setImage(null);
    setImageError(null);
    focus(true);
  };
  // 粘贴：剪贴板里只有图（截图、网页上“复制图片”）才当图；带着字的（从文档里复制的常常也带一张图）照常粘贴文字
  const onPaste = (e) => {
    const cd = e.clipboardData;
    if (!cd || cd.types.includes("text/plain")) return;
    const file = [...cd.files].find((f) => f.type.startsWith("image/"));
    if (!file) return;
    e.preventDefault();
    attach(file);
  };
  const dragging = (e) => !!e.dataTransfer?.types?.includes("Files");

  const send = () => {
    const t = text.trim();
    if (!t || image?.pending) return;
    if (sendNow) {
      onSend(t, image);
      setImage(null);
    } else if (canQueue) onSend(t, null);
    else return;
    setText("");
    setImageError(null);
    clearTimeout(saveTimer.current);
    setDraft(shown.current, "");
  };
  const note = imageError ? { text: imageError, warn: true }
    : queueing && hasText && held ? { text: HELD }
      : queueing && hasText && image ? { text: IMAGE_WAITS } : null;
  const count = text.length > COUNT_FROM ? Array.from(text).length : 0;
  const keep = (e) => e.preventDefault(); // 按下去不抢光标
  return (
    <div className="ws-composer">
      {above}
      {/* 点输入框这一块的空白处（四周留白、按钮之间）也把光标放进去：可点的是整块，不只是一行高的 textarea（第 31 条） */}
      <div className={"ws-input" + (drag ? " drop" : "")}
        onClick={(e) => { if (e.target === e.currentTarget || e.target.classList.contains("ws-input-bar") || e.target.classList.contains("ws-input-tools")) ref.current?.focus(); }}
        onDragOver={(e) => { if (dragging(e)) { e.preventDefault(); setDrag(true); } }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDrag(false); }}
        onDrop={(e) => { if (!dragging(e)) return; e.preventDefault(); setDrag(false); attach(e.dataTransfer.files[0]); }}>
        {image && <Attachment att={image} onRemove={removeImage} />}
        <textarea ref={ref} rows={1} value={text} aria-label="输入"
          placeholder={queueing && held ? HELD : image && sendNow ? WITH_IMAGE : PLACEHOLDER[mode] || PLACEHOLDER.busy}
          onChange={(e) => setText(e.target.value)} onPaste={onPaste}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return;
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            } else if (e.key === "ArrowUp" && onTakeBack && !(e.shiftKey || e.altKey || e.metaKey || e.ctrlKey)
              && e.currentTarget.selectionStart === 0 && e.currentTarget.selectionEnd === 0) {
              e.preventDefault();
              onTakeBack();
            }
          }} />
        {note && <div className={"ws-input-note" + (note.warn ? " warn" : "")} role={note.warn ? "alert" : undefined}>{note.warn && <IconWarn size={13} />}<span>{note.text}</span></div>}
        <div className="ws-input-bar">
          <button className="ws-attach" aria-label="添加图片" title="添加一张图：让它改这张图，或照着画" disabled={mode === "opening"} onMouseDown={keep}
            onClick={() => fileRef.current?.click()}><IconImage size={16} /></button>
          <input ref={fileRef} type="file" accept={IMAGE_ACCEPT} hidden tabIndex={-1}
            onChange={(e) => { attach(e.target.files?.[0]); e.target.value = ""; }} />
          {count > COUNT_FROM && <span className="ws-input-count">{count.toLocaleString("en-US")} 字</span>}
          <div className="ws-input-tools">{tools}</div>
          {canStop && hasText && (
            <button className="ws-btn sm ws-send-later" disabled={!canQueue} onMouseDown={keep} onClick={send}><IconArrowUp size={13} />这一轮答完再发</button>
          )}
          {canStop
            ? <button className="ws-send stop" aria-label={stopping ? "正在停止" : "停止回答"} title={stopping ? "正在停止" : "停止回答（Esc）"} disabled={stopping} onClick={onStop}>
              {stopping ? <Spinner size={14} /> : <IconStop size={13} />}
            </button>
            : <button className="ws-send" aria-label="发送" disabled={!sendNow || !hasText || !!image?.pending} onClick={send}><IconArrowUp size={15} /></button>}
        </div>
      </div>
      <div className="ws-ai-note">内容由 AI 生成，请仔细甄别</div>
    </div>
  );
}

// 选好要发的图：缩略图（点开看大图）、能拿它做什么，右上角去掉。大图还在本机压缩时先转圈
function Attachment({ att, onRemove }) {
  const [viewing, setViewing] = useState(null);
  return (
    <div className="ws-attach-row">
      <div className="ws-attach-thumb">
        {att.url
          ? <button className="ws-attach-open" aria-label="看大图" onClick={(e) => setViewing(e.currentTarget)}><img src={att.url} alt="要发的图" /></button>
          : <Spinner size={16} />}
        <button className="ws-attach-remove" aria-label="去掉这张图" onMouseDown={(e) => e.preventDefault()} onClick={onRemove}><IconClose size={12} /></button>
      </div>
      <span className="ws-attach-note" role="status">{att.url ? "可以让它改这张图、照着它画；它看不到图里的内容" : "正在处理图片…"}</span>
      {viewing && <Viewer img={{ alt: "要发的图" }} src={att.url} onClose={() => { viewing.focus(); setViewing(null); }} />}
    </div>
  );
}

// 输入框里发送按钮左边：深度思考开关（选的模型能开关才有）和选模型。新对话随便选；对话开始后模型定下不换，
// 点开只说明一句、列出能选的给开新对话时参考。思考开关每句话都能改，按下去不抢光标（输入框里的字和光标都留着）。
// 模型分三组：推荐（Auto，写明它现在用哪个：按接口给的 model 对出名字，不写死）、可用、登录后可用（游客锁着的，默认收起）。
// 桌面是菜单：往放得下的那边开（新对话的输入框在页面中间，往下开），高度按那边剩下的空间，再多就在里面滚，第一项不会被切掉。
// 窄屏是底部弹层：最高 70dvh、里面滚，深度思考的开关和说明也在里面（输入框里只放模型名，能开关思考的名字前有个灯泡，亮着是开）。
// 菜单、弹层打开时光标在里面：Esc 只收起它们，不会停掉正在跑的这一轮（见 Composer 的 Esc）；点外面、关闭钮也能收起。
// locked：对话开始了，模型不能换。游客看到的要登录的模型（m.locked）点了去登录（onLogin），登录了还锁着的（只给管理员的）不列，见 ModelList。
// loading：模型列表还没取到、或者打开的对话状态还没回来，先放一块占位（输入框照常能打字、能发，按 Auto）
function ModelTools({ models, loading, current, locked, onPick, thinking, onThinking, onLogin }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  const btn = useRef(null);
  // 收起：桌面光标回到输入框；手机上回到模型按钮（放回输入框会弹出键盘）
  const close = () => {
    setOpen(false);
    if (mobile) btn.current?.focus({ preventScroll: true });
    else {
      document.activeElement?.blur();
      focusInput();
    }
  };
  useEffect(() => {
    if (!open) return undefined;
    const down = (e) => !box.current?.contains(e.target) && setOpen(false);
    addEventListener("mousedown", down);
    return () => removeEventListener("mousedown", down);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  useLayer(open, close);
  useEffect(() => { setOpen(false); }, [mobile]); // 窗口拉宽、变窄：菜单和弹层不是一个东西
  const opt = models?.find((m) => m.id === current);
  if (loading) return <span className="ws-skel ws-skel-model" aria-hidden="true" />;
  if (!models?.length || !current) return null;
  const toggle = opt?.thinking === "toggle";
  const name = opt?.name || current;
  const note = locked ? "对话开始后不能换模型，想换模型请开新对话" : "开始对话后不能换模型";
  const list = (
    <ModelList models={models} current={current} locked={locked} onLogin={onLogin}
      onChoose={(m) => { close(); if (m.locked) onLogin?.(m); else if (!locked) onPick(m.id); }} />
  );
  return (
    <div className="ws-model" ref={box}>
      {toggle && !mobile && <ThinkToggle on={thinking} onChange={onThinking} />}
      <button ref={btn} className="ws-model-btn" aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={open}
        aria-label={mobile && toggle ? `${name}，深度思考${thinking ? "已开" : "已关"}` : undefined}
        title={mobile ? undefined : locked ? "这段对话用的模型" : "选模型"} onClick={() => (open ? close() : setOpen(true))}>
        {mobile && toggle && <IconBulb size={14} className={"bulb" + (thinking ? " on" : "")} />}
        <span className="name">{name}</span><IconChevron size={13} className="caret" />
      </button>
      {open && !mobile && <ModelMenu anchor={box} note={note} onLeave={() => setOpen(false)}>{list}</ModelMenu>}
      {open && mobile && (
        <ModelSheet title={locked ? "这段对话的模型" : "选模型"} onClose={close}>
          {toggle && <ThinkSwitch on={thinking} onChange={onThinking} />}
          <div className="ws-menu-note">{note}</div>
          {list}
        </ModelSheet>
      )}
    </div>
  );
}

// 深度思考：开、关各是什么样（2026-11-06 Sean 定这段话，不写具体秒数、额度）
const THINK_ON = "开：先想清楚再答，复杂问题更稳，要多等一会儿、多花点额度";
const THINK_OFF = "关：直接答，更快更省";

// 桌面的思考开关：悬停或用键盘移到上面时，在上方说明开、关的差别（不用浏览器自带的 title：出得慢、键盘看不到）。Esc 收起说明
function ThinkToggle({ on, onChange }) {
  const [tip, setTip] = useState(false);
  useLayer(tip, () => setTip(false)); // 说明浮着时 Esc 先收说明（光标在输入框里也是），再按才停止回答
  return (
    <span className="ws-think-wrap" onMouseEnter={() => setTip(true)} onMouseLeave={() => setTip(false)}>
      <button className={"ws-think-toggle" + (on ? " on" : "")} aria-pressed={on} aria-describedby="ws-think-tip" onMouseDown={(e) => e.preventDefault()} onClick={() => onChange(!on)}
        onFocus={(e) => { if (e.currentTarget.matches(":focus-visible")) setTip(true); }} onBlur={() => setTip(false)}>
        <IconBulb size={14} />深度思考
      </button>
      <span id="ws-think-tip" role="tooltip" className={"ws-tip" + (tip ? " show" : "")}>
        <b>深度思考{on ? "已开" : "已关"}</b>
        <span className={on ? "cur" : undefined}>{THINK_ON}</span>
        <span className={on ? undefined : "cur"}>{THINK_OFF}</span>
      </span>
    </span>
  );
}

// 底部弹层里的思考开关：一整行都能点，下面写明开、关的差别
function ThinkSwitch({ on, onChange }) {
  return (
    <div className="ws-sheet-think">
      <button className="ws-switch-row" role="switch" aria-checked={on} aria-describedby="ws-think-desc" onClick={() => onChange(!on)}>
        <IconBulb size={16} /><span className="label">深度思考</span><span className={"ws-switch" + (on ? " on" : "")} aria-hidden="true" />
      </button>
      <p id="ws-think-desc" className="ws-think-desc"><span>{THINK_ON}</span><span>{THINK_OFF}</span></p>
    </div>
  );
}

// 桌面的模型菜单：上面空间够（或比下面多）往上开，不然往下开；高度不超过那边剩下的，里面滚。窗口大小变了重新算
function ModelMenu({ anchor, note, onLeave, children }) {
  const ref = useRef(null);
  const [place, setPlace] = useState(null); // {up, max}
  useLayoutEffect(() => {
    const fit = () => {
      const r = anchor.current.getBoundingClientRect();
      const need = ref.current.scrollHeight;
      const above = r.top - 16; // 离按钮 8px、离视口边 8px
      const below = innerHeight - r.bottom - 16;
      const up = above >= need || above >= below;
      setPlace({ up, max: Math.floor(Math.max(120, Math.min(up ? above : below, 560))) });
    };
    fit();
    addEventListener("resize", fit);
    return () => removeEventListener("resize", fit);
  }, [anchor]);
  // 位置算好、看得见了再把光标放进来（还藏着的时候放不进去）
  const placed = !!place;
  useEffect(() => {
    if (!placed) return;
    const el = ref.current;
    (el.querySelector('[aria-checked="true"]:not(:disabled)') || el.querySelector("button:not(:disabled)"))?.focus();
  }, [placed]);
  return (
    <div ref={ref} className="ws-model-menu" style={place ? { [place.up ? "bottom" : "top"]: "calc(100% + 8px)", maxHeight: place.max } : { visibility: "hidden" }}
      onBlur={(e) => { if (e.relatedTarget && !anchor.current?.contains(e.relatedTarget)) onLeave(); }}>
      <div className="ws-menu-note">{note}</div>
      {children}
    </div>
  );
}

// 手机上的底部弹层：盖一层暗底，点暗底、按 Esc（外接键盘）、点关闭都收起；打开时光标在里面、Tab 在里面转
function ModelSheet({ title, onClose, children }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    (el.querySelector('[aria-checked="true"]:not(:disabled):not([role="switch"])') || el.querySelector(".ws-sheet-close"))?.focus();
  }, []);
  const trap = (e) => {
    if (e.key !== "Tab") return;
    const els = [...ref.current.querySelectorAll("button:not(:disabled)")];
    const i = els.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) {
      e.preventDefault();
      els.at(-1)?.focus();
    } else if (!e.shiftKey && i === els.length - 1) {
      e.preventDefault();
      els[0]?.focus();
    }
  };
  return (
    <>
      <div className="ws-sheet-scrim" onClick={onClose} aria-hidden="true" />
      <div ref={ref} className="ws-sheet" role="dialog" aria-modal="true" aria-label={title} onKeyDown={trap}>
        <div className="ws-sheet-head">
          <b>{title}</b>
          <button className="ws-icon-btn ws-sheet-close" aria-label="关闭" onClick={onClose}><IconClose size={18} /></button>
        </div>
        <div className="ws-sheet-body">{children}</div>
      </div>
    </>
  );
}

// 模型列表（菜单和弹层共用）：推荐 / 可用 / 登录后可用（N，默认收起；正在用的在里面就展开）。↑↓ 在能点的之间挪。
// 锁着的只给能登录的游客看（点了开登录弹窗，说登录后就能用它）；登录了、或者没有登录功能，锁着的就是怎么都选不了，不列（第 28 条：原来列成“暂不开放”）
function ModelList({ models, current, locked, onLogin, onChoose }) {
  const [auto, ...rest] = models;
  const free = rest.filter((m) => !m.locked);
  const shut = onLogin ? rest.filter((m) => m.locked) : [];
  const [showShut, setShowShut] = useState(() => shut.some((m) => m.id === current));
  // 点开“登录后可用”：展开的在下面、多半在菜单外，把这一组滚到菜单顶上让人看见
  const shutRef = useRef(null);
  const reveal = useRef(false);
  useEffect(() => {
    if (!reveal.current) return;
    reveal.current = false;
    const t = shutRef.current;
    const box = t?.closest(".ws-model-menu, .ws-sheet-body");
    if (!box) return;
    const top = box.scrollTop + t.getBoundingClientRect().top - box.getBoundingClientRect().top - 4;
    box.scrollTo({ top, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [showShut]);
  const actual = auto.model ? modelName(models, auto.model, null) : null;
  const autoNote = [auto.description, actual && `现在用 ${actual}`].filter(Boolean).join("，");
  const row = (m, note) => (
    <button key={m.id} role="menuitemradio" aria-checked={m.id === current} className={"ws-model-row" + (m.locked ? " locked" : "")}
      disabled={m.locked ? !onLogin : locked && m.id !== current} onClick={() => onChoose(m)}>
      <span className="ws-model-item"><b>{m.name}</b>{note && <small>{note}</small>}</span>
      {m.id === current ? <IconCheck size={14} /> : m.locked && <IconLock size={13} className="lock" />}
    </button>
  );
  const move = (e) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const els = [...e.currentTarget.querySelectorAll("button:not(:disabled)")];
    const i = els.indexOf(document.activeElement);
    const n = els.length;
    const to = e.key === "Home" ? 0 : e.key === "End" ? n - 1 : (i + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    e.preventDefault();
    els[to]?.focus();
  };
  return (
    <div className="ws-model-list" role="menu" aria-label="选模型" onKeyDown={move}>
      <div role="group" aria-label="推荐">
        <div className="ws-model-group" aria-hidden="true">推荐</div>
        {row(auto, autoNote)}
      </div>
      {free.length > 0 && (
        <div role="group" aria-label="可用">
          <div className="ws-model-group" aria-hidden="true">可用</div>
          {free.map((m) => row(m, m.description))}
        </div>
      )}
      {shut.length > 0 && (
        <div role="group" aria-label="登录后可用">
          <button ref={shutRef} className="ws-model-group toggle" role="menuitem" aria-expanded={showShut} onClick={() => { reveal.current = !showShut; setShowShut(!showShut); }}>
            <span>登录后可用（{shut.length}）</span><IconChevron size={13} className={"chev" + (showShut ? " up" : "")} />
          </button>
          {showShut && shut.map((m) => row(m, m.description))}
        </div>
      )}
    </div>
  );
}
