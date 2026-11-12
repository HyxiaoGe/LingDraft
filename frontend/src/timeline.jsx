// 对话。一轮是用户的一句话加上模型这一轮做的事：先是过程（思考、搜索、读页按发生的先后），然后是天气卡片、画的图，再是正文和这条回答用到的来源；
// 最后一轮下面是推荐问题（起的时候先说一声）。回答时又说的话排着（最多一句），贴在输入框上沿，这一轮答完就发，能撤回到输入框；
// 也能插进这一轮：模型做完手上这一步就看到它，插进去以后排在过程里。过程正在跑时展开，正文一出来就收成一行（重新打开的对话里也收着）
// “已完成 25 秒 · 搜了 6 次 · 读了 4 页”，点开看每一步。收起的内容还留在页面里，浏览器的查找搜得到（见 Fold）。
// 每一轮是一个 section（data-turn），回答开头有“灵稿”的标识；三轮起右边（宽屏）或顶栏（窄屏）有轮次目录，见 TurnIndex。
// 每句话和每条回答下面有时间，都能复制；最后一句在空闲时、问着的时候能改，改完按新的话重新回答。这些一直显示，之前各轮的淡一档。
// 模型做到一半会停下来问用户（状态 asking）：最后一轮下面出一张问题卡（AskCard），点选项、在输入框里写、或者跳过，这一轮接着跑；问过的问题和回答排在过程里。
// 问着的时候不想答也有出口：输入框空着时右边是停止（这一轮到此为止），那句话下面能编辑，问题卡下面能重新回答。
// 模型选了“当前位置”时停下来要的是位置（st.ask.kind 是 location）：出位置卡（LocateCard），浏览器里已经允许过定位的不出卡、直接取。
// 存下来的对话为准，正在跑的这一轮叠上实时片段。
import { BriefcaseBusiness, MapPin, MessageCircleQuestionMark, MessageSquare } from "lucide-react";
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { listInspirations } from "./api.js";
import shell from "./card.module.css";
import { FileCards, fileVersions, turnCards } from "./artifact.jsx";
import { CommandRow, OutputFiles, outputsOf } from "./commands.jsx";
import { Composer, CreditsNote, Dock, DropHint, JumpButton, ModelIcon, ModelTools, Pending, enterSends, focusInput, useAutoHeight, useImageDrop } from "./composer.jsx";
import { failureText } from "./errors.js";
import { SentFiles, fileIndex, partUnit } from "./files.jsx";
import { FilesButton, workspaceFiles } from "./workspace-files.jsx";
import { IconBook, IconCheck, IconChevron, IconClip, IconCloudSun, IconCopy, IconDownload, IconEdit, IconEye, IconFolder, IconFork, IconImage, IconLoop, IconMore, IconPage, IconPin, IconPlane, IconPrint, IconRetry, IconRoute, IconSearch, IconTicket, IconTrain, IconTrash, IconWarn, IconWrite, Spinner } from "./icons.jsx";
import { ChartCards, ImageCards, Viewer, drawn, imageUrl, imagesOf } from "./images.jsx";
import { MODES, MapCard, mapsOf } from "./maps.jsx";
import { ItineraryCard, TravelCard, itinerariesOf, travelOf } from "./travel.jsx";
import { locate, locateAgreed, locatePermission } from "./locate.js";
import { Markdown } from "./markdown.jsx";
import { ActionMenu } from "./menu.jsx";
import { HeadBar, NavContext, touch, typeHere, typeNext } from "./nav.jsx";
import proc from "./process.module.css";
import { ACTIVE, ASKING, LOCATION, answerText, citeNumbers, copyText, creditsText, cutPartialCite, domain, elapsed, mergeItems, modelName, splitTurn, stamp, tidyTyping, typingUnit } from "./research.js";
import { AnswerSources, Fold, Mark, useFoldOpen } from "./sources.jsx";
import task from "./task.module.css";
import styles from "./timeline.module.css";
import { TurnIndex, TurnIndexButton, turnEntries } from "./turns.jsx";
import { WeatherCard, forecastsOf } from "./weather.jsx";

// 今日灵感没取到时，新对话页显示这几个
const EXAMPLES = [
  "深圳这周会不会降温？",
  "最近一周 AI 圈有什么新闻？",
  "珠穆朗玛峰现在的官方高度是多少？",
];
// 切到“工作”时：今日灵感里没有调研题、没取到，就显示这几个
const TASK_EXAMPLES = [
  "调研国内主流的向量数据库，写一份选型报告",
  "帮我做一个三天两晚的成都出行计划",
  "对比今年新出的折叠屏手机，写一份购买建议",
];

// 轮次目录放哪（见 turns.jsx）：正文 832 宽居中（两边各留 28），右边空出来的那块放得下 184 宽的目录、离右边 16、离正文 24：滚动区至少这么宽才放右栏；
// 只放得下一列短横线（离右边 16、最宽 20，离正文 28）就放短横线，鼠标移上去展开，没有鼠标的（平板）不放；再窄放顶栏的按钮
const RAIL_MIN = 1280;
const TICKS_MIN = 960;

// 阅读位置（第 45 条）：每个对话看到哪儿了记在这个标签页里（sessionStorage），离开（换对话、去海报页、登录后整页重载、刷新）
// 再回来，回到原处。记的是“第几轮，视口上沿在这一轮顶上往下多少”；在底部就记“底部”，回来还在底部、接着跟。从没记过的照旧到底（最新一轮）
const READING = "lingdraft.reading.";
const readingOf = (id) => { try { return JSON.parse(sessionStorage.getItem(READING + id)); } catch { return null; } };
const keepReadingOf = (id, at) => { try { sessionStorage.setItem(READING + id, JSON.stringify(at)); } catch { /* 存不下就只管这一次打开 */ } };
export const forgetReading = (id) => { try { sessionStorage.removeItem(READING + id); } catch { /* 没有就算了 */ } };

// files：模型写的文件在右栏怎么开（见 artifact.jsx）：{open(目标), current（右栏正开着的，没开是 null）}
// work：{on：这一页是工作任务（新页面上选着“工作”，或者开着的是工作任务）, onChange：新页面上换“对话 / 工作”}，开始以后不能换
export function Conversation({ st, live, busy, suggesting, error, now, act: rawAct, tid, putBack, listedTitle, models, modelNames, credits, picker, work, manage, who, files }) {
  const scrollRef = useRef(null);
  const stick = useRef(true); // 用户往上翻看时不自动滚到底
  const lastTop = useRef(0); // 上一次滚动时的位置：往上滚了才算用户离开底部
  const [away, setAway] = useState(false); // 离底部远了：输入框上面出“回到底部”
  const [more, setMore] = useState(false); // 离开底部以后下面又长出了内容：“回到底部”写上“有新内容”
  const [rail, setRail] = useState(null); // 轮次目录在正文右边是一栏（"full"）、短横线（"ticks"），还是放顶栏的按钮（null），见 RAIL_MIN
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
    for (const turn of el.querySelectorAll("[data-turn]")) {
      const r = turn.getBoundingClientRect();
      if (r.bottom > top + 1) return { turn: Number(turn.dataset.turn), offset: Math.round(top - r.top) };
    }
    return { bottom: true };
  };
  const backTo = (at) => {
    const el = scrollRef.current;
    const turn = at && !at.bottom && el.querySelector(`[data-turn="${Number(at.turn)}"]`);
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
    const width = new ResizeObserver(() => {
      const w = el.clientWidth;
      setRail(w >= RAIL_MIN ? "full" : w >= TICKS_MIN && matchMedia("(hover: hover)").matches ? "ticks" : null);
    });
    width.observe(el);
    return () => {
      ro.disconnect();
      width.disconnect();
    };
  }, []);
  // 用户自己做的（发一句话、重新回答、重试、改最后一句、点接着问）：跳到底跟着看这一轮。排着的话自动开跑、打开一个正在跑的对话都不拉人。
  // 光标不搬：按钮点完没了（接着问、重试、重新回答这一轮跑起来就不在了），焦点会掉到页面上，这时才放回输入框，好接着写；原来在哪就还在哪
  const act = { ...rawAct };
  for (const k of ["start", "say", "retry", "regenerate", "edit", "answer"]) {
    act[k] = (...a) => {
      toBottom();
      const r = rawAct[k](...a);
      requestAnimationFrame(() => { if (!document.activeElement || document.activeElement === document.body) focusInput(); });
      return r;
    };
  }
  // 新对话的今日灵感：桌面在输入框下面，窄屏输入框贴底、它们在输入框上面。按看到的顺序放，Tab 和读屏的先后和眼睛看的一致
  const starters = fresh && <Inspirations key={work?.on ? "work" : "chat"} work={!!work?.on} onPick={act.start} />;

  const mode = fresh ? "new" : opening ? "opening" : st?.status === "queued" ? "queued" : busy || ACTIVE.has(st?.status) ? "busy" : st?.status || "busy";
  const stopping = !!(live?.stopping || st?.stopping);
  // 拖图进来：整个对话区都接，盖一层“松手添加图片”（见 useImageDrop）；打开中加不了图，和左下角加图的按钮一样
  const mainRef = useRef(null);
  const dropping = useImageDrop(mainRef, mode !== "opening");
  // 回答时（包括还在排队、正在停止）发的话排着，这一轮答完就发；新对话刚发出去、还没拿到 id 时排不了
  const queueing = !!tid && (mode === "busy" || mode === "queued");
  const onSend = (text, images, docs) => { // images：带的图（[{key, file, url}]），docs：带的文件（解析好了的，见 composer.jsx 的 picked），排着的话都不带
    if (mode === "new") return act.start(text, images, docs);
    if (mode === "idle") return act.say(text, images, docs);
    if (mode === ASKING) return act.answer({ text, typed: true }); // 问着的时候输入框里写的就是回答
    if (queueing) return act.queue(text);
    return undefined;
  };
  const blank = work?.on ? "新工作任务" : "新对话";
  const title = live?.title || st?.title || live?.said || (opening ? listedTitle ?? null : blank); // null：打开中、列表里也还没有，顶栏先放占位
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
    <button className="ws-icon-btn" data-head-more aria-label="这段对话的更多操作" title="更多操作" aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={!!menuAt}
      onClick={(e) => setMenuAt(menuAt ? null : e.currentTarget)}>
      <IconMore size={18} />
    </button>
  );
  const sz = mobile ? 18 : 14;
  const closeMenu = () => setMenuAt(null);
  const menuItems = [
    { key: "rename", icon: <IconEdit size={sz} />, text: "重命名", disabled: status !== "idle", onClick: () => { closeMenu(); setRenaming(true); } },
    // 这段对话在问题卡上点过“别再问了”（st.asks_off）才有这一项：改回来。问题卡答完就没了，恢复放在这里
    ...(st?.asks_off ? [{ key: "asks", icon: <MessageCircleQuestionMark size={sz} />, text: "恢复提问", disabled: status !== "idle",
      onClick: () => { closeMenu(); manage.askAgain(tid); } }] : []),
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
  const menuNote = running ? "正在回答，答完或停下后才能改名、删除" : status === "failed" ? "这段对话没有完成，重试以后才能改名"
    : status === ASKING ? "回答或跳过问题以后才能改名" : null;
  const edit = renaming && manageable ? {
    value: title || "",
    onDone: (t, how) => {
      setRenaming(false);
      if (t && t !== title) manage.rename(tid, t);
      if (how !== "blur") requestAnimationFrame(() => document.querySelector("[data-head-more]")?.focus());
    },
  } : null;
  runningRef.current = running;
  const asking = mode === ASKING; // 停下来问用户了：排着的话还留着（回答以后这一轮答完再发），照样贴在输入框上沿
  const pendingKinds = running || asking ? [st?.guided_message && "guided", st?.queued_message && "queued"].filter(Boolean) : [];
  const said = useAnnounce(st, running, stopping); // 读屏：只在开始、答完、停下、出错时说一句（见 useAnnounce）
  // 轮次目录：三轮起才有，只列轮次，见 turnEntries。正文右边放得下就放一栏，放不下顶栏右边一个按钮点开
  const entries = !fresh && !opening ? turnEntries(st, live, running) : [];
  const index = entries.length > 0;
  const jumpTo = (id) => {
    const el = scrollRef.current?.querySelector(`[data-turn="${CSS.escape(id)}"]`);
    if (!el) return;
    stick.current = false;
    el.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    el.focus({ preventScroll: true }); // 键盘、读屏接着从这一轮往下读
  };

  return (
    <section ref={mainRef} className={styles.main} data-fresh={fresh || undefined} aria-label="对话">
      <HeadBar title={fresh ? blank : title} edit={edit}
        mark={!fresh && work?.on && <BriefcaseBusiness size={15} strokeWidth={1.8} className="mr-1.5 inline-block align-[-2px] text-accent" role="img" aria-label="工作任务" />}
        end={<>
          {files && !fresh && !opening && <FilesButton count={workspaceFiles(st, live, title).count} on={!!files.current?.list} onClick={files.toggle} />}
          {index && !rail && <TurnIndexButton entries={entries} scroller={scrollRef} onJump={jumpTo} />}{headMenu}
        </>} />
      {menuAt && manageable && <ActionMenu label="这段对话的操作" anchor={menuAt} mobile={mobile} items={menuItems} note={menuNote} onClose={closeMenu} />}
      <div className={styles.scroll} data-scroll ref={scrollRef}
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
        <div className={styles.flow}>
          {!fresh && !opening && <h1 className={styles.printTitle}>{title}</h1>/* 只在打印时出现：屏幕上标题在顶栏，打印不印顶栏（第 49 条） */}
          {fresh ? <Welcome who={who} work={work} /> : opening ? <ThreadSkeleton /> : <Turns st={st} live={live} busy={busy} suggesting={suggesting} stopping={stopping} now={now} act={act} models={models} modelNames={modelNames} title={title} files={files} />}
          {error && <div className="ws-error" role="alert"><IconWarn size={15} /><span>{error.text ?? error}</span>
            {error.login && picker.onLogin && <button className="ws-btn ws-error-act" onClick={picker.onLogin}>登录</button>}</div>}
        </div>
      </div>
      {index && rail && <TurnIndex entries={entries} scroller={scrollRef} onJump={jumpTo} mini={rail === "ticks"} />}
      <div className="sr-only" role="status" aria-live="polite">{said}</div>
      {mobile && starters}
      <Dock>
        {/* 刚被额度拦住、报错里已经说了用完了和登录后会怎样：输入框上面不再重复一遍（第 27 条：同一句别在最烦的时候说好几遍） */}
        {!error?.quota && <CreditsNote credits={credits} onLogin={picker.onLogin} />}
        {away && <JumpButton more={more} onClick={toBottom} />}
        <Composer thread={tid || "new"} carry={!!live} mode={mode} queueing={queueing} held={pendingKinds.length > 0} stopping={stopping} putBack={putBack} onSend={onSend} onStop={act.stop}
          above={(running || asking) && <Pending st={st} act={act} asking={asking} />} locating={asking && st?.ask?.kind === LOCATION} onTakeBack={pendingKinds.length ? () => act.withdraw(pendingKinds) : null}
          work={!!work?.on}
          tools={<ModelTools models={models} loading={models === null || opening} {...picker} />} />
      </Dock>
      {dropping && <DropHint />}
      {!mobile && starters}
    </section>
  );
}

// 读屏播报（polite）：这一轮开始、答完（带来源条数）、停下来问用户（带上问题）、停下、出错时各说一句。过程里的阶段、正文逐字出现都不播：
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
    setSaid(st?.status === ASKING ? (st.ask?.kind === LOCATION ? "需要你的位置" : `需要你回答：${st.ask?.question || ""}`)
      : st?.status === "failed" ? "这一轮中途停下了，可以重试"
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
    <div className="flex flex-col gap-3" role="status" aria-label="正在打开对话">
      {show && (
        <>
          <div className="mb-3 h-10 w-[44%] self-end rounded-lg bg-hover" />
          {[96, 88, 92, 60].map((w) => <div key={w} className="h-4 rounded-sm bg-hover" style={{ width: `${w}%` }} />)}
          <div className="mb-3 mt-6 h-10 w-[28%] self-end rounded-lg bg-hover" />
          {[90, 72].map((w) => <div key={w} className="h-4 rounded-sm bg-hover" style={{ width: `${w}%` }} />)}
        </>
      )}
    </div>
  );
}

// 新对话：欢迎语在输入框上面，欢迎语下面是“对话 / 工作”的大开关，输入框下面是今日灵感，一起放在中间偏上（窄屏输入框贴底，这些在它上面）。
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
// 选着“工作”时的欢迎语（2026-11-11，照豆包：换成问有什么活），不分钟点
const greetWork = (n) => [
  { t: `今天有什么工作要处理${c(n)}？` },
  { t: `${p(n)}这次想交给我什么活？` },
  { t: `要调研点什么、写份什么${c(n)}？` },
  { t: `有什么想让我帮着做完的${c(n)}？` },
];
function greeting(who, at, roll, work) {
  const guest = who === null;
  const name = !guest && who.trim().length <= NAME_MAX ? who.trim() : "";
  if (work) {
    const pool = greetWork(name);
    return pool[Math.floor(roll * pool.length)].t;
  }
  const h = at.getHours() < 5 ? at.getHours() + 24 : at.getHours();
  const ok = (x) => !(guest && x.back);
  const slot = GREET_SLOTS.find(([from, to]) => h >= from && h < to)[2](name).filter(ok);
  const rest = greetAny(name, at.getDay()).filter(ok);
  const pool = roll < 0.5 ? slot : rest;
  return pool[Math.floor(((roll * 2) % 1) * pool.length)].t;
}

// who：登录了是名字（没填是空字符串），游客是 null。一打开就有（先按本机记着的，见 auth.localWho），不用等认人
function Welcome({ who, work }) {
  const [at] = useState(() => ({ time: new Date(), roll: Math.random() }));
  return (
    <div className={styles.welcome}>
      <h2>{greeting(who ?? null, at.time, at.roll, !!work?.on)}</h2>
      {work?.onChange && <ModeSwitch on={!!work.on} onChange={work.onChange} />}
    </div>
  );
}

// 对话 / 工作（2026-11-11 Sean 定，照豆包）：新页面上欢迎语下面的两段大开关，开始以后这段对话就定下了、不能再换，所以只在这里有。
// 选着“工作”时欢迎语、输入框里的提示、下面的例子都换成工作的；左栏的“新对话”“新工作任务”和它是同一件事。
// 按下去不抢输入框的光标（写着的字照旧）；键盘移上去以后左右方向键换
function ModeSwitch({ on, onChange }) {
  const chat = useRef(null);
  const workBtn = useRef(null);
  const seg = (v, ref, icon, label) => (
    <button ref={ref} role="radio" aria-checked={on === v} tabIndex={on === v ? 0 : -1}
      onMouseDown={(e) => e.preventDefault()}
      onClick={(e) => {
        if (on !== v) onChange(v);
        if (e.currentTarget.parentElement.contains(document.activeElement)) e.currentTarget.focus(); // 键盘正停在另一段上：光标跟到点的这段
      }}
      onKeyDown={(e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        onChange(!on);
        (on ? chat : workBtn).current?.focus();
      }}>{icon}{label}</button>
  );
  return (
    <div className={styles.modes} role="radiogroup" aria-label="对话还是工作" data-on={on ? "work" : "chat"}>
      {seg(false, chat, <MessageSquare size={16} strokeWidth={1.8} />, "对话")}
      {seg(true, workBtn, <BriefcaseBusiness size={16} strokeWidth={1.8} />, "工作")}
    </div>
  );
}

// 今日灵感：Fusion 每天出的题（后端代取，只留新闻、调研、常识、天气、生图），大家看到的一样，每次打开新对话打乱顺序。
// 选着“工作”时（work）只放其中的调研题（后端另外给的 tasks），没有就放固定的几个任务例子；换“对话 / 工作”时整块重来（见 Conversation 里的 key）。
// 取回来的题在这次打开的页面里记半小时（后端也是半小时取一次），换来换去、再开新对话都不用再等
// 只有两行题，上面不放小标题、说明和“换一批”，也不放“能做的事”（2026-11-07 Sean 定）。放得下几个放几个；
// 每 15 秒一个接一个翻过去换下一批（批次 A 停过，2026-11-07 Sean 定恢复、改成 15 秒），鼠标放在上面、键盘停在上面时不翻。
// 系统设了减少动态效果：直接换，不翻。一行放不下的题折成两行，不截断。点一下直接发。取不到就显示固定的几个例子。取回来之前先放占位，出来时页面不跳
const ROTATE_MS = 15000;
const FLIP_MS = 300; // 和 timeline.module.css 的 .inspire 里翻的过渡时长一致
const FLIP_STAGGER = 80;
const CHIP_GAP = 8; // 和 timeline.module.css 的 .inspire 的 gap 一致
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

const INSPIRE_FRESH = 30 * 60 * 1000;
let inspired = null; // {at, questions, tasks}：最近取到的那批
const inspirePool = (got, work) => {
  const qs = work ? got?.tasks : got?.questions;
  return qs?.length ? { questions: shuffled(qs), today: true } : { questions: work ? TASK_EXAMPLES : EXAMPLES, today: false };
};

function Inspirations({ work, onPick }) {
  // {questions, today}；today 为假：没取到，用的固定例子
  const [pool, setPool] = useState(() => (inspired && Date.now() - inspired.at < INSPIRE_FRESH ? inspirePool(inspired, work) : null));
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
    if (!pool) {
      listInspirations()
        .then((got) => {
          inspired = { at: Date.now(), ...got };
          if (on) setPool(inspirePool(got, work));
        })
        .catch(() => on && setPool(inspirePool(null, work)));
    }
    return () => {
      on = false;
      timers.current.forEach(clearTimeout);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    <section className={styles.examples} aria-label={work ? "新工作任务的起点" : "新对话的起点"}>
      {/* 停翻只看两行题：外面这块往下撑满了页面，鼠标放在下面的空白处不算 */}
      <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        onFocus={() => setFocused(true)} onBlur={(e) => !e.currentTarget.contains(e.relatedTarget) && setFocused(false)}>
      <div className={styles.ruler} ref={ruler} aria-hidden="true">
        {pool?.questions.map((q) => <span key={q} className={styles.chip}>{q}</span>)}
      </div>
      <div className={styles.inspire + (flipped ? " " + styles.flip : "")} ref={box} aria-label={pool?.today ? "今日灵感" : work ? "可以这样交代" : "可以这样问"} role="group">
        {pool ? shown.map((i, n) => {
          const q = pool.questions[i];
          const d = `${n * FLIP_STAGGER}ms`; // 只有翻的两项错开，悬停变色不等
          return <button key={q} className={styles.chip} style={{ transitionDelay: `${d}, ${d}, 0ms, 0ms` }} onClick={() => onPick(q)}>{q}</button>;
        }) : [112, 168, 136, 150].map((w) => <span key={w} className="block h-8 flex-none rounded-md bg-hover phone:h-9" style={{ width: w }} aria-hidden="true" />)}
      </div>
      </div>
    </section>
  );
}

// ---------------- 一轮一轮 ----------------

function Turns({ st, live, busy, suggesting, stopping, now, act, models, modelNames, title, files }) {
  const turns = st?.turns || [];
  const versions = fileVersions(st, live); // 这段对话里写成了的各份文件、各版：卡片和过程里改文件那一步对上标题
  const running = busy || ACTIVE.has(st?.status);
  // 刚说的话还没出现在存下来的对话里：先按页面手上的显示
  const pending = live?.said && turns.length <= (live.turnsAtStart ?? 0)
    ? { id: "pending", user: live.said, items: [], record: null, local: live.images?.map((i) => i.url), localFiles: live.docs?.map((d) => d.file).filter(Boolean) }
    : null;
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
          <section key={t.id} className={styles.turn} data-turn={i} tabIndex={-1} aria-label={`第 ${i + 1} 轮`} aria-busy={last && running ? true : undefined}>
            <Turn t={t} k={`${st?.thread_id}:${i}`} st={st} live={last && running ? live : null} running={last && running} suggesting={last && suggesting} stopping={stopping} last={last} now={now} act={act} models={models} modelNames={modelNames} title={title} images={images}
              versions={versions} files={files} />
          </section>
        );
      })}
    </>
  );
}

// k：这一轮在页面上的记号（对话 id + 第几轮）。正在发的这一轮存下来以后 id 会变（见 Turns 的 pending），过程展开没展开按 k 记，不跟着重来
function Turn({ t, k, st, live, running, suggesting, stopping, last, now, act, models, modelNames, title, images, versions, files }) {
  const items = mergeItems(t.items, live);
  const cards = turnCards(items, live?.drafts, versions); // 这一轮写的文件，正文下面一份一张
  const outputs = outputsOf(items); // 这一轮跑命令产出的：图在正文上面，别的文件在正文下面
  const { process: steps, answer } = splitTurn(items);
  // 停下来等用户回答的那个问题（一次问了两个的先给前一个）：在下面的问题卡里，过程里不重复列；答了、跳过了的才排进过程
  const ask = last && !running && st?.status === ASKING ? st.ask : null;
  const process = steps.some((i) => i.type === "ask" && i.pending) ? steps.filter((i) => !(i.type === "ask" && i.pending)) : steps;
  const texts = items.filter((i) => i.type === "text").map((i) => i.text);
  const nums = citeNumbers(texts, st?.sources);
  // 过程里还有子任务卡：它们的报告引用的来源接着往下编号。这一轮的文字排前面，正文的编号和底下的来源列表不变
  const procNums = citeNumbers([...texts, ...subTexts(items)], st?.sources);
  const rec = t.record;
  const idle = !running && st?.status === "idle";
  const failed = last && !running && st?.status === "failed";
  const replied = answer.some((i) => i.type === "text");
  // 回答开头：这一轮用的模型的厂商图标和名字（照 Fusion）。答完的按这一轮记下的；正在答的按后端说的这一轮用的（Auto 判断完选的、
  // 出问题换的备用；中途打开的按状态里的 model_ref），还没定下就先不写。Auto 用的免费模型不在选项里，名字和图标在 modelNames
  const ref = rec?.model?.ref || (running ? live?.model || st?.model_ref : null);
  const named = ref && modelNames?.[ref];
  const who = ref && models?.length > 0 ? named?.name || modelName(models, ref) : null;
  return (
    <>
      {t.user && <UserMsg text={live?.edit ?? t.user} images={userImages(t, live, st?.thread_id)} files={t.localFiles || t.files || []} tid={t.localFiles ? null : st?.thread_id}
        panel={files} at={live?.sentAt ?? t.sent_at} pinned={last} now={now} editable={last && (idle || !!ask)} onEdit={act.edit} />}
      <div className={styles.ai}>
        {who && <div className={styles.aiHead}><ModelIcon m={{ model: ref, name: who, icon: named?.icon }} size={22} />{who}</div>}
        {(running || rec) && <Process k={k} items={process} st={st} nums={procNums} rec={rec} running={running} answering={replied} failed={failed} asking={ask ? (ask.kind === LOCATION ? LOCATION : true) : false} now={now} versions={versions} />}
        {forecastsOf(items).map((fc) => <WeatherCard key={fc.location} fc={fc} />)}
        {mapsOf(items).map((card) => <MapCard key={card.key} card={card} />)}
        {travelOf(items).map((card) => <TravelCard key={card.key} card={card} />)}
        {itinerariesOf(items).map((card) => <ItineraryCard key={card.key} card={card} />)}
        <ImageCards steps={imagesOf(items)} tid={st?.thread_id} running={running} onRegenerate={last && idle ? act.say : null} title={title} base={images} />
        <ChartCards charts={outputs.charts} tid={st?.thread_id} />
        {answer.map((it, k) => <Item key={`${it.type}:${it.id}`} it={it} st={st} nums={nums} running={running} lastItem={k === answer.length - 1} />)}
        {files && <FileCards cards={cards} running={running} open={files.open} current={files.current} />}
        <OutputFiles files={outputs.files} tid={st?.thread_id} panel={files} after={!!files && cards.length > 0} />
        {running && <LiveLine live={live} items={items} stopping={stopping} now={now} versions={versions} />}
        {ask && (ask.kind === LOCATION ? <LocateCard key={ask.id} ask={ask} onAnswer={act.answer} /> : <AskCard key={ask.id} ask={ask} onAnswer={act.answer} />)}
        {!running && !ask && <AnswerSources nums={nums} sources={st?.sources} />}
        {!running && rec?.ended_at && rec.outcome === "no_reply" && <p className={styles.muted}>这一轮没有给出文字回复。</p>}
        {failed && <Failed st={st} act={act} />}
        {/* 问着的时候也有这一行：只有“重新回答”（这个问题不答了，同一句话重来，2026-11-11 Sean 定） */}
        {!running && (replied || rec?.ended_at || (last && idle) || ask) && (
          <div className={styles.aiFoot + (last ? "" : " " + styles.quiet)}>
            <Stamp iso={rec?.ended_at} now={now} />
            {rec?.credits != null && <span className={styles.model}>本轮 {creditsText(rec.credits)}</span>}
            {replied && <CopyButton text={() => answerText(items, st?.sources)} />}
            {last && (idle || ask) && <button className="ws-btn ghost sm" onClick={act.regenerate}><IconLoop size={13} />重新回答</button>}
          </div>
        )}
      </div>
      {last && idle && rec?.suggestions?.length > 0 && <Suggestions items={rec.suggestions} onPick={(q) => act.say(q)} />}
      {last && idle && suggesting && !rec?.suggestions?.length && <SuggestWait />}
    </>
  );
}

// 发出去的图在本机的地址（图的编号 → blob 地址）：存下来的对话回来以后接着用它显示，不用再从服务器取一遍
const sentImages = new Map();

// 这句话带的图（[{img, src}]）从哪儿取：刚发出去、还没存下来的用本机的；发出去的这一轮记下本机地址（按发的先后对上），之后接着用
// （不再从服务器取一遍）；别的从后端取
function userImages(t, live, tid) {
  if (t.local) return t.local.map((src) => ({ src }));
  if (!t.images?.length || !tid) return [];
  return t.images.map((img, k) => {
    const mine = live?.images?.[k]?.url;
    if (mine && !sentImages.has(img.id)) sentImages.set(img.id, mine);
    return { img, src: sentImages.get(img.id) || imageUrl(tid, img) };
  });
}

// 用户说的话带的图，在气泡上面：一张是缩略图（不超过 240px 见方），几张是一排方块；点开看大图
function UserImages({ list }) {
  if (list.length === 1) return <UserImage key={list[0].src} img={list[0].img} src={list[0].src} />;
  return <div className={styles.userImgs}>{list.map((p, k) => <UserImage key={p.src} img={p.img} src={p.src} n={k + 1} square />)}</div>;
}

// 一张图：图已经不在了（删掉的）说一声
const THUMB = 240;
function UserImage({ img, src, n, square }) {
  const [failed, setFailed] = useState(false);
  const [viewing, setViewing] = useState(null);
  const k = !square && img?.width && img?.height ? Math.min(THUMB / img.width, THUMB / img.height, 1) : 0;
  const box = k ? { width: Math.round(img.width * k), aspectRatio: `${img.width} / ${img.height}` } : undefined;
  return (
    <div className={styles.userImg + (square ? " " + styles.square : "")} style={box}>
      {failed ? <span className={styles.userImgGone}>图片取不到了</span> : (
        <button className={styles.userImgOpen} aria-label={n ? `看第 ${n} 张大图` : "看大图"} onClick={(e) => setViewing(e.currentTarget)}>
          <img src={src} alt={n ? `发的第 ${n} 张图` : "发的图"} onError={() => setFailed(true)} />
        </button>
      )}
      {viewing && <Viewer img={{ alt: "发的图" }} src={src} onClose={() => { typeNext(viewing); setViewing(null); }} />}
    </div>
  );
}

// 用户说的话：下面是发送时间和“复制”，最后一句在空闲时还有“编辑”。都一直显示，pinned（最后一轮）以外的淡一档
// files：这句话带的文件（点了在右栏看，panel 是右栏；tid 是对话编号，刚发出去还没存下来的没有 tid，先不能点）
function UserMsg({ text, images, files, tid, panel, at, pinned, now, editable, onEdit }) {
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editable) setEditing(false); }, [editable]); // 开着编辑框时这一轮又跑起来了（点了重新回答、推荐问题），编辑框就收起
  if (editing && editable) return <EditBox text={text} onCancel={() => { setEditing(false); focusInput(); }} onSend={(t) => { setEditing(false); onEdit(t); }} />;
  return (
    <div className={styles.user}>
      {images.length > 0 && <UserImages list={images} />}
      {files.length > 0 && <SentFiles files={files} tid={tid} panel={panel} />}
      <Bubble text={text} />
      <div className={styles.userFoot + (pinned ? " " + styles.pinned : "")}>
        <Stamp iso={at} now={now} />
        <CopyButton text={() => text} />
        {editable && <button className="ws-btn ghost sm" onClick={() => setEditing(true)}><IconEdit size={13} />编辑</button>}
      </div>
    </div>
  );
}

// 改最后一句：回车发送，Shift+回车换行（触屏上回车换行，点“发送”，见 enterSends），Esc 取消；没改动不能发
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
    <div className={styles.edit}>
      <textarea ref={ref} rows={1} value={v} data-esc aria-label="改这句话" onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (enterSends(e)) { e.preventDefault(); send(); }
          else if (e.key === "Escape") onCancel();
        }} />
      <div className={styles.editBar}>
        <span className={styles.hint}>发送后按新的话重新回答，这一轮原来的回答会被替换</span>
        <button className="ws-btn ghost sm" onClick={onCancel}>取消</button>
        <button className="ws-btn primary sm" disabled={!changed} onClick={send}>发送</button>
      </div>
    </div>
  );
}

// 自己说的话：太长（超过 FOLD_OVER 行）就收起来，只露前 FOLD_SHOW 行、最后一行渐隐，下面居中“展开全部”；展开后最下面是“收起”
// （照豆包，2026-11-07 Sean 要的）。气泡变宽变窄（拖窗口、开合左栏）重新量；收起时这句话的开头跑到上面去了就滚回来
const FOLD_OVER = 6;
const FOLD_SHOW = 4;
function Bubble({ text }) {
  const box = useRef(null);
  const body = useRef(null);
  const [long, setLong] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const el = body.current;
    const check = () => setLong(el.scrollHeight > parseFloat(getComputedStyle(el).lineHeight) * FOLD_OVER + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text]);
  const fold = long ? (open ? "open" : "closed") : undefined;
  const toggle = () => {
    setOpen(!open);
    if (open) requestAnimationFrame(() => box.current?.scrollIntoView({ block: "nearest" }));
  };
  return (
    <div ref={box} className={styles.bubble} data-fold={fold} style={{ "--fold": FOLD_SHOW }}>
      <div ref={body} className={styles.said}>{text}</div>
      {long && <button className={styles.fold} aria-expanded={open} onClick={toggle}>{open ? "收起" : "展开全部"}</button>}
    </div>
  );
}

// 发送或回答完的时间，悬停看精确到秒
function Stamp({ iso, now }) {
  const s = stamp(iso, now);
  return s && <time className={styles.time} dateTime={iso} title={s.full}>{s.text}</time>;
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

// 这一轮的过程：一行“已完成 / 已停止 / 已中断 / 中途停下 + 用时 + 做了什么”，点开看每一步（停下来问用户时是“等你回答”，不写用时）。
// 用时不算等用户回答的那段（rec.waited_ms）。正在跑时展开，正文一出来就收成这一行
// （2026-11-09 Sean 定，原来跑完不收：过程往往很长，开着把回答推远）。收了这次就不再自动展开：模型有时先写一句“我先查一下”再去搜，
// 那句字刚出来时算正文、过程收起，搜索来了它归回过程，这时也不再展开（收起是结束动作，Sean 定），在做什么看下面那一行。
// 按这次运行记（running_since），重新回答是新的一次，又从展开开始。
// 例外是跑命令（2026-11-12 Sean）：模型写命令、跑命令的时候要看得见命令一行行出来（见 commands.jsx），收起了的这时重新展开，
// 下一次出正文再收。模型常常先说一句“我用 Python 生成一份 Excel”再写脚本，不展开的话那几十秒只有底下一行“正在写命令”
const settled = new Set();
function Process({ k, items, st, nums, rec, running, answering, failed, asking, now, versions }) {
  const run = running ? `${k}@${st?.running_since ?? ""}` : null;
  if (run && items.some((i) => i.type === "step" && i.kind === "command" && i.pending)) settled.delete(run);
  else if (run && answering) settled.add(run);
  const [open, toggle, found] = useFoldOpen(`proc:${k}`, running && !settled.has(run)); // 这次运行正文还没出来过的展开；自己点过的照点的来
  const shown = open && items.length > 0;
  const time = running ? elapsed(st?.running_since || rec?.started_at, now)
    : rec?.ended_at ? elapsed(rec.started_at, Date.parse(rec.ended_at), rec.waited_ms || 0) : "";
  const verb = running ? "处理中" : asking === LOCATION ? "等你的位置" : asking ? "等你回答" : failed ? "中途停下" : rec?.outcome === "stopped" ? "已停止" : rec?.outcome === "blocked" ? "已中断" : "已完成";
  const did = running ? "" : didText(items);
  const label = `${verb}${time ? ` ${time}` : ""}${did ? ` · ${did}` : ""}`;
  return (
    <div className={proc.proc}>
      {items.length
        ? <button className={proc.head} aria-expanded={shown} onClick={toggle}>{label}<IconChevron size={13} className={"chev" + (shown ? " up" : "")} /></button>
        : <div className={proc.head}>{label}</div>}
      {items.length > 0 && <Fold open={shown} onFound={found}><div className={proc.body}><ProcItems items={items} st={st} nums={nums} running={running} versions={versions} /></div></Fold>}
    </div>
  );
}

// 收起来那一行带上做了什么：搜了几次、读了几页（子任务里的也算），开了几个子任务；做成了的才算
function didText(items) {
  let search = 0, read = 0, tasks = 0, kb = 0, ran = 0;
  const count = (xs) => xs.forEach((i) => {
    if (i.type !== "step" || i.pending) return;
    if (i.kind === "task") { tasks++; count(i.items || []); }
    else if (i.kind === "search" && i.ok) search++;
    else if (i.kind === "knowledge" && i.ok) kb++;
    else if (i.kind === "command" && i.run) ran++; // 跑了的都算（命令自己出错的也是跑了）
    else if (i.kind === "read" && i.body) read++; // 和 ToolRow 一样：读到原文的才算
  });
  count(items);
  return [kb && `查了 ${kb} 次资料库`, search && `搜了 ${search} 次`, read && `读了 ${read} 页`, ran && `运行了 ${ran} 条命令`, tasks && `${tasks} 个子任务`].filter(Boolean).join(" · ");
}

// 过程里的一条条：思考、中途说的话、工具步骤、问用户的问题和回答，子任务是一张卡片。子任务卡里是它的子 agent 的这些，排法一样。
// hideUnknown：子 agent 还在跑，它查到的来源要等它做完才进这一轮的来源表，还对不上的引用先不显示（不然先冒一个问号）
// versions：这段对话写成了的文件（见 artifact.fileVersions），改文件那一步按它写出是哪份
function ProcItems({ items, st, nums, running, hideUnknown = false, versions }) {
  const cite = (sid) => (hideUnknown && !st?.sources?.[sid] ? null : <Mark n={nums[sid]} s={st?.sources?.[sid]} nums={nums} sources={st?.sources} />);
  const files = items.some((i) => i.kind === "file") ? fileIndex(st?.turns) : null; // 读文件那一步对上文件名、类型
  return items.map((it) => (it.type === "step" ? (it.kind === "task" ? <TaskCard key={`step:${it.id}`} s={it} st={st} nums={nums} running={running} />
    : <ToolRow key={`step:${it.id}`} s={it} running={running} files={files} versions={versions} />)
    : it.type === "thinking" ? <Thinking key={`thinking:${it.id}`} it={it} thinking={running && it.live && it.ms == null} />
      : it.type === "blocked" ? <div key={`blocked:${it.id}`} className={proc.tool + " " + proc.bad}><IconWarn size={12} /><span className={proc.main}>内容审核没通过，这个子任务停在这里</span></div>
        : it.type === "guide" ? <GuideNote key={`guide:${it.id}`} it={it} />
          : it.type === "ask" ? <AskNote key={`ask:${it.id}`} it={it} />
            : <ProcText key={`text:${it.id}`} it={it} cite={cite} />));
}

// 用户插进这一轮的话：模型从这里开始看到它
function GuideNote({ it }) {
  return (
    <div className={proc.guide}>
      <span className={proc.guideTag}>你插了一句</span>
      <div className={proc.guideText}>{it.text}</div>
    </div>
  );
}

// 模型停下来问过的问题：一行问题，下面是用户的回答（和插的话一个样子）；跳过的、点了“别再问了”的、没等到回答这一轮就停了的在行尾说一声。
// 要位置时用户没给、直接说了个地方的（kind 是 location）也是这个样子，问题那一行写“你的位置”
function AskNote({ it }) {
  const where = it.kind === LOCATION;
  return (
    <>
      <div className={proc.tool}>
        {where ? <MapPin size={12} /> : <MessageCircleQuestionMark size={12} />}
        <span className={proc.question}>{where ? "你的位置" : it.question}</span>
        {!it.answer && !it.pending && <span className={proc.sub}>{it.muted ? "你选了别再问" : it.skipped ? "你跳过了" : "没有回答"}</span>}
      </div>
      {it.answer && (
        <div className={proc.guide}>
          <span className={proc.guideTag}>你的回答</span>
          <div className={proc.guideText}>{it.answer}</div>
        </div>
      )}
    </>
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
    <div className={task.task + (live ? " " + task.live : !s.pending && !s.ok ? " " + task.bad : "")}>
      <button className={task.head} aria-expanded={open} onClick={toggle}>
        {live ? <Spinner size={12} /> : <IconFork size={12} />}
        <span className={task.main} title={s.text}>{s.text || "子任务"}</span>
        <span className={task.sub} title={status}>{status}</span>
        <IconChevron size={13} className={"chev" + (open ? " up" : "")} />
      </button>
      <Fold open={open} onFound={found}>
        <div className={task.body}>
          {s.prompt && <div className={task.prompt}>{s.prompt}</div>}
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
      <div className={shell.card + " " + shell.warn}>
        <div className={shell.title}><IconWarn size={15} />内容审核没通过</div>
        <div className={shell.sub}>{BLOCKED_TEXT[it.reason] || BLOCKED_TEXT.input}这一轮不会带进之后的对话。</div>
      </div>
    );
  }
  return <Reply it={it} cite={(sid) => <Mark n={nums[sid]} s={st?.sources?.[sid]} nums={nums} sources={st?.sources} />} caret={running && lastItem} />;
}

// ---------------- 逐字出现（打字机） ----------------
// 后端约每 80ms 发来一批字：正文每批五六个字、很匀，思考每批几个到七十多个不等（2026-11-07 本机量的）。直接显示是一跳一跳的；
// 按“攒了多少放多快”放，批大批小速度就忽快忽慢，看着还是一阵一阵。这里按来字的速度匀速放：
// - 来字速度取最近一秒左右（RATE_S）的平均；手里留大约 BUFFER_S 秒的字，两批之间一直有字可放
// - 留的比该留的多了或少了，按平滑过的存量（HELD_S）在 FIX_S 秒左右里慢慢追平，来一大批也不会突然加速；攒了一秒以上的多出来那部分很快放掉，不越落越远
// - 这一段收完了（答完、停了），剩下的在 DONE_S 秒左右放完，不在答完以后还慢慢吐
// 放到哪、来字速度按条目记在 typed 里：条目在过程和正文之间挪动、重新挂载时接着往下放，不从头再来。
// 打开时就已经有一大段的（存下来的对话、中途打开正在跑的对话）整段显示，不从头打一遍；系统设了减少动效的收到就显示。
// md：是 Markdown（正文、中途说的话）。看不见的符号、引用、链接网址整块跳过（见 typingUnit）；每次显示都要把整段重新解析一遍
// （几千字要好几毫秒），长了就隔几帧才显示一次，最慢一秒 15 次
const RATE_S = 1;
const BUFFER_S = 0.3;
const HELD_S = 0.3;
const FIX_S = 1;
const DONE_S = 0.25;
const MIN_CPS = 30;
const typed = new Map();
const STILL = matchMedia("(prefers-reduced-motion: reduce)");
const fresh = (pos) => ({ pos, seen: pos, rate: 60, held: 0, at: performance.now() });

function useTyped(key, text, live, md) {
  const s = useRef(null);
  s.current ??= !live || STILL.matches ? fresh(text.length) : typed.get(key) ?? fresh(text.length < 200 ? 0 : text.length);
  const [, setShown] = useState(0);
  useEffect(() => {
    const st = s.current;
    if (STILL.matches) st.pos = text.length;
    if (live) typed.set(key, st);
    if (st.pos >= text.length) {
      if (!live) typed.delete(key);
      return undefined;
    }
    const gap = md ? Math.min(66, Math.max(0, (text.length - 1500) / 60)) : 0;
    let raf = 0;
    let drawn = 0;
    const frame = (t) => {
      const dt = Math.max(0.001, (t - st.at) / 1000);
      st.at = t;
      st.rate += ((text.length - st.seen) / dt - st.rate) * (1 - Math.exp(-dt / RATE_S));
      st.seen = text.length;
      const left = text.length - st.pos;
      st.held += (left - st.held) * (1 - Math.exp(-dt / HELD_S));
      const fix = 1 + (st.held - st.rate * BUFFER_S) / Math.max(1, st.rate * FIX_S);
      const v = Math.max(MIN_CPS, st.rate * Math.min(3, Math.max(0.5, fix)), (left - Math.max(80, st.rate)) / 0.3, live ? 0 : left / DONE_S);
      st.pos = Math.min(text.length, st.pos + v * dt);
      if (md) st.pos = Math.max(st.pos, typingUnit(text, Math.floor(st.pos)));
      const end = st.pos >= text.length;
      if (end || t - drawn >= gap) {
        drawn = t;
        setShown(Math.floor(st.pos)); // 整数没变就不重新渲染
      }
      if (!end) raf = requestAnimationFrame(frame);
      else if (!live) typed.delete(key);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [key, text, live, md]);
  let n = Math.floor(s.current.pos);
  if (n >= text.length) return text;
  if (/[\uD800-\uDBFF]/.test(text[n - 1] || "")) n++; // 不把占两个码元的字（emoji 之类）切成两半
  return text.slice(0, n);
}

// 正文：逐字出现，正在写（或者还没放完）的时候末尾有光标，写完才能下载代码
function Reply({ it, cite, caret }) {
  const [text, typing] = useMd(it);
  const on = caret || typing;
  return (
    <div className={styles.reply + (on ? " " + styles.typing : "")}>
      <Markdown text={text} cite={cite} download ready={!on} />
    </div>
  );
}

// 过程里中途说的话（之后又去搜了、读了），一样逐字出现
function ProcText({ it, cite }) {
  return <div className={proc.text}><Markdown text={useMd(it)[0]} cite={cite} /></div>;
}

// 显示到的那段 Markdown 和还在不在往外放。还在写、还没放完的，最后一行半截的 Markdown 先收拾一下（见 tidyTyping）；写完的只去掉末尾半个引用
function useMd(it) {
  const shown = useTyped(`text:${it.id}`, it.text, it.live, true);
  const typing = shown.length < it.text.length;
  return [it.live || typing ? tidyTyping(shown) : cutPartialCite(shown), typing];
}

// 思考：逐字出现。正在想的时候展开，想完这一段、字也放完了就收成一行（2026-11-09 Sean 定，不等这一轮跑完）：思考往往很长，开着把回答推远。
// 重新打开的对话里也收着，点开看原文；自己点开、收起的照点的来
function Thinking({ it, thinking }) {
  const text = useTyped(`thinking:${it.id}`, it.text, it.live, false);
  const [shown, toggle, found] = useFoldOpen(`thinking:${it.id}`, thinking || text.length < it.text.length);
  const box = useRef(null);
  useLayoutEffect(() => { // 正在想的时候框里跟着最新的字
    if (thinking && box.current) box.current.scrollTop = box.current.scrollHeight;
  });
  const secs = it.ms != null ? Math.max(1, Math.round(it.ms / 1000)) : null;
  const label = thinking ? "深度思考中" : secs ? `思考了 ${secs} 秒` : "思考过程";
  return (
    <div className={proc.think + (thinking ? " " + proc.live : "")}>
      <button className={proc.thinkHead} aria-expanded={shown} onClick={toggle}>
        {thinking ? <Spinner size={12} /> : <IconChevron size={13} className={"chev" + (shown ? " up" : "")} />}
        <span>{label}</span>
      </button>
      <Fold open={shown} onFound={found}><div ref={box} className={proc.thinkText}>{text}</div></Fold>
    </div>
  );
}

// 还没有结果的一步（pending）：这一轮在跑就是还在做；没在跑了（这一轮出错停在这一步）就是没有完成，和子任务卡一样说。
// files：这段对话里用户发过的文件（见 fileIndex），读文件那一步用
function ToolRow({ s, running, files, versions }) {
  if (s.kind === "command") return <CommandRow s={s} running={running} />; // 跑命令：能点开看命令和输出，见 commands.jsx
  const Icon = { read: IconPage, weather: IconCloudSun, place: IconPin, route: IconRoute, flight: IconPlane, train: IconTrain, itinerary: IconTicket,
    image: IconImage, docs: IconBook, view: IconEye, file: IconClip, write: IconWrite, knowledge: IconFolder }[s.kind] || IconSearch;
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
    main = <>查地点「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>{l}</span>)}</>;
    const n = s.places?.places.length;
    sub = s.pending ? "查询中…" : s.places ? (n ? `${n} 个地方` : "没找到") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "route") {
    main = <>查路线「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>{l}</span>)}</>;
    const got = s.routes?.routes.map((r) => MODES[r.mode] || r.mode).join("、");
    sub = s.pending ? "计算中…" : s.routes ? (got ? `算出${got}` : "没算出来") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "flight" || s.kind === "train") {
    main = <>{s.kind === "flight" ? "查航班" : "查火车"}「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>{l}</span>)}</>;
    const n = s.travel?.services.length;
    sub = s.pending ? "查询中…" : s.travel ? (n ? `${n} 个班次` : "没查到班次") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "itinerary") {
    main = <>排行程「{s.text}」</>;
    sub = s.pending ? "排列中…" : s.itinerary ? `${s.itinerary.plans.length} 个方案` : "没排成";
  } else if (s.kind === "library") { // 查文档前先找库（Context7）
    main = <>找文档库「{s.text}」</>;
    sub = s.pending ? "查找中…" : s.ok ? (s.hits ? `${s.hits} 个候选` : "没找到") : `没找成：${s.message || "出错了"}`;
  } else if (s.kind === "docs") { // 灰色小标是哪个库
    main = <>查文档「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>{l}</span>)}</>;
    sub = s.pending ? "查询中…" : s.ok ? (s.hits ? `${s.hits} 段文档` : "没有带出处的文档") : `没查成：${s.message || "出错了"}`;
  } else if (s.kind === "view") { // 看不见图的模型调看图工具：问的是什么
    main = <>看图「{s.text}」</>;
    sub = s.pending ? "查看中…" : s.ok ? "看过了" : s.message || "没看成";
  } else if (s.kind === "file") { // 读用户发的文件：找的词，或者读的第几页（PPT 第几张、别的第几段）
    const f = files?.[s.text];
    const name = f?.name || (s.pending || !s.ok ? null : s.text); // 还在读、没读成的记的是编号，对不上就不写
    const unit = partUnit(f?.type);
    main = <>读文件{name && <>「{name}」</>}{s.limits.map((l) => <span key={l} className={proc.lim}>找「{l}」</span>)}{s.parts && <span className={proc.lim}>第 {s.parts} {unit}</span>}</>;
    sub = s.pending ? "读取中…" : !s.ok ? s.message || "没读成" : s.limits.length ? (s.hits ? `找到 ${s.hits} ${unit}` : "没找到") : "读过了";
  } else if (s.kind === "knowledge") { // 查用户的资料库：查的话，找到几段（同一段的几块算一段）；只查了一个库的，灰色小标是哪个
    main = <>查资料库「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>只查「{l}」</span>)}</>;
    sub = s.pending ? "查找中…" : !s.ok ? s.message || "没查成" : s.source_ids?.length ? `找到 ${s.source_ids.length} 段` : "没找到";
  } else if (s.kind === "write") { // 模型写文件、改文件：标题（改的没给新标题，按改的那份对上），写成了是第几版
    const name = s.text || versions?.get(s.target)?.at(-1)?.title;
    main = <>{s.edit ? "改文件" : "写文件"}{name && <>「{name}」</>}</>;
    sub = s.pending ? "保存中…" : s.file ? `第 ${s.file.version} 版` : s.message || "没写成";
  } else if (s.kind === "image") {
    main = <>{s.edit ? "改图" : "画图"}「{s.text}」</>;
    sub = s.pending ? "生成中…" : s.image ? "已生成" : `没画成：${s.message || "出错了"}`;
  } else {
    main = <>搜索「{s.text}」{s.limits.map((l) => <span key={l} className={proc.lim}>{l}</span>)}</>;
    sub = s.pending ? "搜索中…" : s.ok ? `${s.hits} 条结果${s.message ? `（${s.message}）` : ""}` : `没搜成：${s.message || "出错了"}`;
  }
  if (s.pending && !busy) {
    sub = "没有完成";
    bad = false;
  }
  return (
    <div className={proc.tool + (busy ? " " + proc.pending : bad ? " " + proc.bad : "")}>
      {busy ? <Spinner size={12} /> : <Icon size={12} />}
      <span className={proc.main} title={s.text}>{main}</span>
      {sub && <span className={proc.sub} title={sub}>{sub}</span>}
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
  ["view", () => "正在看图"],
  ["file", () => "正在读文件"],
  ["knowledge", () => "正在查资料库"],
  ["write", () => "正在保存文件"],
  ["command", () => "正在运行命令"],
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
function LiveLine({ live, items, stopping, now, versions }) {
  const lastItem = items.at(-1);
  const pending = items.filter((i) => i.type === "step" && i.pending);
  const streaming = lastItem?.live && (lastItem.type === "text" || (lastItem.type === "thinking" && lastItem.ms == null));
  // 模型正在写命令（还没开始跑，见 research.addLive 的 writing）：写的字在过程里那一步，这里说在写
  const acting = pending.filter((i) => !i.writing);
  const kind = WAITING.find(([k]) => acting.some((i) => i.kind === k));
  // 模型正在写文件（参数还在流出来，还没有那一步）：写的字在卡片和右栏里，这里说在写哪份
  const drafting = Object.values(live?.drafts || {}).filter((d) => !items.some((i) => i.type === "step" && i.id === d.id));
  const d = drafting.at(-1);
  const draftName = d && (d.title || versions?.get(d.file_id)?.at(-1)?.title);
  const draftLine = d && `${d.name === "edit_file" ? "正在修改" : "正在写"}${draftName ? `《${draftName}》` : "文件"}`;
  const stage = stopping ? "正在停止…" : live?.stage || (kind ? kind[1](acting.filter((i) => i.kind === kind[0]).length)
    : acting.length < pending.length ? "正在写命令" : draftLine || (streaming ? null : PHASES[live?.phase] || "正在准备…"));
  // 看得到的进展：条目多了、字长了、子任务里有动静、阶段变了。变了就重新计时
  const sub = pending.filter((i) => i.kind === "task").map((i) => `${i.items?.length ?? 0}.${i.items?.at(-1)?.text?.length ?? 0}.${i.phase}`).join(",");
  const waited = useWaited(`${items.length}:${lastItem?.text?.length ?? 0}:${pending.length}:${live?.phase}:${stage}:${sub}:${drafting.map((d) => d.text.length).join(",")}`, now);
  const secs = stage && !stopping && waited >= SHOW_WAIT ? Math.floor(waited / 1000) : 0;
  return (
    <div className={proc.waiting}>
      {stage && <><Spinner size={14} /><span className={proc.stage}>{stage}</span></>}
      {secs > 0 && <span className={proc.secs}>{secs} 秒</span>}
      {secs > 0 && !touch() && <span className={proc.key} aria-hidden="true"><kbd className="ws-kbd">Esc</kbd>停止</span>}
      {secs * 1000 >= SLOW && <span className={proc.slow}>这一步比平常慢，可以接着等，也可以停止</span>}
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
  return <div className={styles.suggestWait}><Spinner size={12} /><span>正在想接着问什么…</span></div>;
}

// 接着问：和新对话页的今日灵感同一种胶囊（横排、放不下换行），名字分开（2026-11-06 Sean 定）
function Suggestions({ items, onPick }) {
  return (
    <div className={styles.suggest} role="group" aria-labelledby="ws-suggest-label">
      <div className={styles.suggestLabel} id="ws-suggest-label">接着问</div>
      <div className={styles.suggestList}>
        {items.map((q) => <button key={q} className={styles.chip} onClick={() => onPick(q)}>{q}</button>)}
      </div>
    </div>
  );
}

// 模型停下来问的问题（见后端 app.tools.ask_user）：一张卡一个问题，下面是它给的选项，点一个就是回答。
// “自己写”把光标放进输入框——问着的时候输入框里写的就是回答；“跳过”不答，它按自己的判断接着做。不回答就一直等着，没有倒计时。
// “别再问了”（2026-11-11 Sean 定）：这个问题跳过，这段对话之后也不再出问题卡；顶栏“⋯”里能恢复提问
function AskCard({ ask, onAnswer }) {
  return (
    <div className={shell.card + " " + styles.ask} role="group" aria-label="需要你回答的问题">
      <div className={shell.title}><MessageCircleQuestionMark size={15} /><span>{ask.question}</span></div>
      {ask.options.length > 0 && (
        <div className={styles.askOptions}>
          {ask.options.map((o) => <button key={o} className={styles.askOption} onClick={() => onAnswer({ text: o })}>{o}</button>)}
        </div>
      )}
      <div className={styles.askActions}>
        <button className="ws-btn ghost sm" onClick={typeHere}><IconEdit size={13} />自己写</button>
        <button className="ws-btn ghost sm" onClick={() => onAnswer({ skip: true })}>跳过</button>
        <button className={"ws-btn ghost sm " + styles.askMute} onClick={() => onAnswer({ mute: true })}>别再问了</button>
      </div>
    </div>
  );
}

// 模型选了“当前位置”（查天气、找附近、算路线），后端停下来要位置（st.ask 的 kind 是 location，见后端 app.tools.location）。
// 要过一次就不再要（2026-11-11 Sean 定）：给过的位置后端留一小段时间，这段时间里再用到的根本不来要；过了再来要的，
// 浏览器里已经允许过定位、或者这台设备上同意过（见 locate.js 的 locateAgreed）的不出卡，直接取了交回去。
// 浏览器里禁了的当用户不给，取不了的（不是 https、不支持）当取不到，也不出卡，模型会改成问在哪。
// 别的（头一次）出这张卡：点“用我的位置”，浏览器弹它自己的授权。不点就一直等；
// 这时在输入框里直接写个地方也行（和回答问题一样），输入框空着时右边是停止。
const LOCATE_FOR = { weather: "查天气", nearby: "找附近的地方", route: "算路线" };
// 这个页面已经替哪几次要位置交过回答（ask 的 id）。没交上的（网络断了、额度用完）卡会重新出来：这时出卡等用户点，不再自己取了交，不然会一直重试
const locateTried = new Set();
function LocateCard({ ask, onAnswer }) {
  const [phase, setPhase] = useState("checking"); // checking：在查浏览器的权限 → card：出卡等用户点 | locating：在取位置
  const sent = useRef(false); // 交过一次就不再交（开发时 effect 跑两遍、点了两下）
  const here = useRef(true);
  const answer = useRef(onAnswer);
  answer.current = onAnswer;
  const send = (a) => {
    if (sent.current || !here.current) return; // 取位置的时候用户已经自己说了地方、点了停止：卡没了，不再交
    sent.current = true;
    locateTried.add(ask.id);
    answer.current(a);
  };
  const take = async () => {
    setPhase("locating");
    const got = await locate();
    send(got.location ? { location: got.location, auto: true } : got.denied ? { skip: true, auto: true } : { unavailable: true, auto: true });
  };
  useEffect(() => {
    here.current = true;
    if (locateTried.has(ask.id)) setPhase("card");
    else locatePermission().then((p) => {
      if (!here.current || sent.current) return;
      if (p === "granted" || (p === "prompt" && locateAgreed())) take();
      else if (p === "denied") send({ skip: true, auto: true });
      else if (p === "unsupported") send({ unavailable: true, auto: true });
      else setPhase("card");
    });
    return () => { here.current = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- 一张卡只查一次（key 是这次要位置的 id）
  if (phase === "checking") return null;
  if (phase === "locating") return <div className={proc.waiting} role="status"><Spinner size={14} /><span className={proc.stage}>正在获取位置…</span></div>;
  return (
    <div className={shell.card + " " + styles.ask} role="group" aria-label="需要你的位置">
      <div className={shell.title}><MapPin size={15} /><span>用你现在的位置{LOCATE_FOR[ask.purpose] || ""}？</span></div>
      <div className={styles.locateActions}>
        <button className="ws-btn primary" onClick={take}>用我的位置</button>
        <button className="ws-btn" onClick={() => send({ skip: true })}>不用</button>
      </div>
    </div>
  );
}

// 这一轮中途停下了：原因和能做什么按后端给的类别说（见 errors.js 的 failureText）
function Failed({ st, act }) {
  return (
    <div className={shell.card + " " + shell.warn}>
      <div className={shell.title}><IconWarn size={15} />这一轮中途停下了</div>
      <div className={shell.sub}>{failureText(st)}</div>
      <div className={shell.actions}><button className="ws-btn primary" onClick={act.retry}><IconRetry size={14} />重试</button></div>
    </div>
  );
}
