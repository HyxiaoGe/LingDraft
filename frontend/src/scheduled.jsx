// 定时任务页（/scheduled，2026-11-12 Sean 定，照豆包的 /chat/cron-jobs）：到点了灵稿自己在一段对话里多跑一轮，结果留在那段对话里。后端见 app/scheduled。
// 上面是任务卡片，按进行中、已暂停、已完成分开：名称、两行交代、什么时候运行；右下角三个钮（鼠标移上去才出来，触屏一直在）：运行、编辑、更多（打开对话、暂停或恢复、删除）。
// 点卡片进它跑的那段对话（还没跑过的打开编辑）。下面“为你推荐”是几个现成的例子，点了打开填好的表单。最底下的输入框说一句话，开一段新对话由模型建。
// 新建、编辑的表单：名称、要做的事、重复（每小时 / 每天 / 每周 / 自定义 / 仅一次）。“自定义”照豆包用一句话说，后端的轻量模型转成时间规则。
// 时间都是北京时间（后端按北京时间算，页面上那一行“每天 · 08:00”也是后端给的）。删了 5 秒内能撤销（同删对话）。游客那里写着登录了才有
import { MessageSquare, Pause, Play, Plus } from "lucide-react";
import { useContext, useEffect, useId, useRef, useState } from "react";
import { createScheduledTask, deleteScheduledTask, getScheduledTasks, readSchedule, runScheduledTask, updateScheduledTask } from "./api.js";
import { Dock, enterSends, useAutoHeight } from "./composer.jsx";
import { errorText, quotaText } from "./errors.js";
import { IconArrowUp, IconCheck, IconClock, IconClose, IconEdit, IconMore, IconTrash, Spinner } from "./icons.jsx";
import { opener, trapTab, useLayer } from "./layers.js";
import { ActionMenu } from "./menu.jsx";
import { HeadBar, NavContext, touch } from "./nav.jsx";
import composer from "./composer.module.css";
import dialog from "./dialog.module.css";
import styles from "./scheduled.module.css";

const cx = (...names) => names.filter(Boolean).join(" ");
const TITLE_MAX = 40; // 后端 app.scheduled.service 的 TITLE_MAX、PROMPT_MAX
const PROMPT_MAX = 4000;
const WHEN_MAX = 200; // 一句话说的时间最多这么长（后端 ScheduleText）
const ABOUT = "到点了灵稿自己去做，结果留在对话里，回来就能看。";
const ASK_HINT = "说一句话建定时任务，比如：每天早上 8 点给我一份 AI 新闻简报";
const WHEN_HINT = "比如：每个月 1 号和 15 号早上 9 点半";

// 现成的例子：点了打开填好的表单，改一改再存
const TEMPLATES = [
  { title: "每日 AI 新闻简报", about: "每天早上汇总过去一天 AI 行业的要闻", cron: "0 8 * * *",
    prompt: "搜索过去 24 小时 AI 行业的重要新闻，挑出最值得关注的 5 到 8 条。每条用一两句话说清发生了什么、为什么重要，并标出来源。没有重要新闻就照实说，不凑数。" },
  { title: "每周行业动态", about: "每周一早上回顾上一周的重要变化", cron: "0 9 * * 1",
    prompt: "回顾过去 7 天新能源汽车行业的重要动态：新品发布、销量数据、政策变化和主要公司的动作。按重要程度排序，写成一份简短的周报，并标出来源。" },
  { title: "历史上的今天", about: "每天早上讲一件历史上今天发生的事", cron: "30 7 * * *",
    prompt: "讲一件历史上的今天发生的、值得知道的事：先用一句话说是什么事，再用两三段讲清来龙去脉和后来的影响。查证以后再写，并标出来源。" },
  { title: "本周热门开源项目", about: "每周五傍晚看看这一周值得关注的开源项目", cron: "0 18 * * 5",
    prompt: "搜索过去 7 天 GitHub 上新近热门的开源项目，挑 5 个最值得关注的。每个说清是做什么的、为什么受关注，并附上项目地址。" },
];

// ---- 什么时候运行：表单里的几样和后端的写法（cron、一个时间点）互相转 ----

const TZ = "Asia/Shanghai";
const ABROAD = new Date().getTimezoneOffset() !== -480; // 这台设备不在北京时间：表单里选的钟点下面写一句“按北京时间”（在国内的不用看这句）
const MODES = [["hourly", "每小时"], ["daily", "每天"], ["weekly", "每周"], ["custom", "自定义"], ["once", "仅一次"]];
const DAYS = [[1, "一"], [2, "二"], [3, "三"], [4, "四"], [5, "五"], [6, "六"], [0, "日"]]; // cron 里的星期：周日是 0

// 时间点 → 北京时间的 “2026-11-13T09:00”（日期时间输入框的值）
function beijing(at) {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const p = {};
  for (const x of new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(d)) p[x.type] = x.value;
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

// cron 里星期那一段（“1-5”“1,3,5”“7”）→ 选中的几天；带步长这类少见的写法返回 null（表单里算“自定义”）
function weekdays(field) {
  const on = new Set();
  for (const part of field.split(",")) {
    const m = part.match(/^([0-7])(?:-([0-7]))?$/);
    if (!m || (m[2] != null && +m[2] < +m[1])) return null;
    for (let d = +m[1]; d <= +(m[2] ?? m[1]); d++) on.add(d % 7);
  }
  return DAYS.map(([d]) => d).filter((d) => on.has(d));
}

// 任务（或者现成的例子）→ 表单里“重复”那几样：{mode, minute, time, days, once, text, read}。
// text 是“自定义”里写的那句话，read 是它转出来的（{cron | once_at, schedule, from: 是哪句话转的}）。几种固定的对不上的 cron 算“自定义”，read 就是它现在的规则
function scheduleOf(t) {
  const base = { mode: "daily", minute: "0", time: "09:00", days: [1], once: `${beijing(Date.now() + 86400000).slice(0, 10)}T09:00`, text: "", read: null };
  if (!t) return base;
  if (t.once_at) return { ...base, mode: "once", once: beijing(t.once_at) };
  const [mi, h, d, mo, w] = (t.cron || "").split(" ");
  const num = (s) => /^\d+$/.test(s || "");
  if (num(mi) && d === "*" && mo === "*") {
    const time = `${String(h).padStart(2, "0")}:${mi.padStart(2, "0")}`;
    if (h === "*" && w === "*") return { ...base, mode: "hourly", minute: String(+mi) };
    if (num(h) && w === "*") return { ...base, mode: "daily", time };
    const days = num(h) && w ? weekdays(w) : null;
    if (days?.length) return { ...base, mode: "weekly", time, days };
  }
  return { ...base, mode: "custom", read: { cron: t.cron, schedule: t.schedule, from: "" } };
}

// 表单里那几样 → 发给后端的 {cron} 或 {once_at}；还没填全是 null
function ruleOf(s) {
  const [h, m] = (s.time || "").split(":").map(Number);
  const timed = Number.isInteger(h) && Number.isInteger(m);
  if (s.mode === "hourly") return /^\d+$/.test(s.minute) && +s.minute < 60 ? { cron: `${+s.minute} * * * *` } : null;
  if (s.mode === "daily") return timed ? { cron: `${m} ${h} * * *` } : null;
  if (s.mode === "weekly") return timed && s.days.length ? { cron: `${m} ${h} * * ${DAYS.map(([d]) => d).filter((d) => s.days.includes(d)).join(",")}` } : null;
  if (s.mode === "once") return s.once ? { once_at: s.once.replace("T", " ") } : null;
  return s.read ? (s.read.cron ? { cron: s.read.cron } : { once_at: s.read.once_at }) : null;
}

// 表单里“重复”那几样没填全时说的话（每周选了哪几天、只差钟点的，说的是每天那句）
const MISSING = { hourly: "填一下每小时的第几分", daily: "选一下几点运行", weekly: "选一下每周哪几天运行", custom: "写一下什么时候运行", once: "选一下运行的时间" };

// ---- 出错时怎么说（后端给的原因见 app/api/main.py 的 _TASK_REJECTED，这里说人话） ----

const REJECTED = {
  login_required: "登录后才能用定时任务",
  bad_title: `名称不能为空，最多 ${TITLE_MAX} 个字`,
  bad_prompt: `要做的事不能为空，最多 ${PROMPT_MAX} 个字`,
  no_schedule: "还没选什么时候运行",
  in_the_past: "这个时间已经过了，选一个以后的时间",
  too_often: "最密每小时运行一次",
  never_runs: "这个时间永远到不了，换一个",
  bad_cron: "运行时间的写法不对，换个说法试试",
  bad_time: "运行时间的写法不对，换个说法试试",
  unclear_time: "没看懂这个时间，换个说法试试",
  busy: "这会儿没认出来，再试一次",
  thread_busy: "这段对话正在回答，等它答完再运行",
  thread_stuck: "这段对话里上一轮还没完成，先去对话里看看",
  changed: "任务刚有改动，再点一次",
  not_found: "这个定时任务已经删掉了",
};
function taskErrorText(e, what) {
  const d = e?.detail;
  if (d?.code === "credits_exhausted") return quotaText(d);
  if (d?.code === "task_rejected") {
    if (d.reason === "too_many") return `最多 ${d.limit} 个定时任务，先删掉一个`;
    if (REJECTED[d.reason]) return REJECTED[d.reason];
  }
  if (e?.status === 404) return REJECTED.not_found;
  return errorText(what, e, "可以再试一次");
}

// 卡片上要让人知道的一句（上次没运行、自己停下了）；没什么要说的是 null
function noteOf(t) {
  if (t.last_status === "running") return null;
  if (t.status === "paused") return t.paused_by === "failures" && t.last_status !== "ok" ? "连着几次没运行成，已经停下" : null;
  if (t.last_status === "failed") return t.status === "done" ? "没运行成，可以点运行再试一次" : "上次没运行成";
  if (t.last_status === "no_credits") return "上次额度用完了，没运行";
  if (t.last_status === "blocked") return "上次没运行：那段对话里上一轮还没完成";
  return null;
}

const GROUPS = [["active", "进行中"], ["paused", "已暂停"], ["done", "已完成"]];

// onLogin：游客去登录；undo：对话页的撤销（同一条提示）；models：能选的模型（/api/models，第一个是 Auto），新建时选新开的那段对话用哪个；
// onOpen(threadId, fresh)：打开任务跑的那段对话（fresh：刚点了运行，那段对话多了一轮，要重新取）；onAsk(text)：开一段新对话把这句话发出去
export function ScheduledPage({ auth, onLogin, undo, models, onOpen, onAsk }) {
  const nav = useContext(NavContext);
  const [data, setData] = useState(null); // getScheduledTasks 回的
  const [error, setError] = useState(null);
  const [failed, setFailed] = useState(null); // 运行、暂停、删除没成：一句话
  const [form, setForm] = useState(null); // 新建是 {}，照例子建是 {preset}，编辑是 {task}
  const [menu, setMenu] = useState(null); // 开着“更多”的那个任务：{id, anchor}
  const [acting, setActing] = useState(null); // 正在发起运行的那个任务
  const [gone, setGone] = useState(() => new Set()); // 删了、还在等撤销的：不显示
  const [ask, setAsk] = useState("");
  const page = useRef(null);
  const newBtn = useRef(null);
  const alive = useRef(true);
  const seq = useRef(0); // 每改一次加一：改之前发出去的那次取列表回来得晚，不拿它盖掉刚改的
  const guest = auth?.ready && !auth.me;

  const load = (quiet = false) => {
    const at = seq.current;
    if (!quiet) setError(null);
    return getScheduledTasks().then((d) => {
      if (alive.current && at === seq.current) setData(d);
    }).catch((e) => { if (!quiet && alive.current) setError(errorText("没取到定时任务", e, "可以再试一次")); });
  };
  useEffect(() => {
    alive.current = true;
    if (!guest) load();
    return () => { alive.current = false; undo?.flush("task"); }; // 离开这一页：删了的不等了
  }, [guest]); // eslint-disable-line react-hooks/exhaustive-deps
  // 任务到点了是后台自己跑的：隔一会儿取一次，有正在运行的取得勤一点
  const running = !!data?.tasks.some((t) => t.last_status === "running");
  useEffect(() => {
    if (guest) return undefined;
    const t = setInterval(() => load(true), running ? 4000 : 20000);
    return () => clearInterval(t);
  }, [guest, running]);

  const tasks = data ? data.tasks.filter((t) => !gone.has(t.id)) : [];
  const live = tasks.filter((t) => t.status !== "done").length;
  const full = !!data && live >= data.limit;
  const put = (t) => {
    seq.current += 1;
    setData((d) => (d ? { ...d, tasks: d.tasks.some((x) => x.id === t.id) ? d.tasks.map((x) => (x.id === t.id ? t : x)) : [...d.tasks, t] } : d));
  };
  const hide = (id, on) => setGone((g) => {
    const n = new Set(g);
    if (on) n.add(id); else n.delete(id);
    return n;
  });
  const openForm = (f) => {
    undo?.flush("task"); // 刚删的还在等撤销：算在个数里，先真删掉
    setFailed(null);
    setForm(f);
  };

  const open = (t) => (t.thread_id ? onOpen(t.thread_id) : openForm({ task: t }));
  const run = async (t) => {
    if (acting) return;
    setActing(t.id);
    setFailed(null);
    try {
      const v = await runScheduledTask(t.id);
      if (!alive.current) return;
      put(v);
      if (v.thread_id) onOpen(v.thread_id, true); // 跑起来了：去那段对话里看
    } catch (e) {
      if (alive.current) setFailed(taskErrorText(e, "没运行起来"));
    } finally {
      if (alive.current) setActing(null);
    }
  };
  const toggle = async (t) => {
    const resume = t.status === "paused";
    setFailed(null);
    try {
      const v = await updateScheduledTask(t.id, { status: resume ? "active" : "paused" });
      if (alive.current) put(v);
    } catch (e) {
      if (!alive.current) return;
      setFailed(resume && e.detail?.reason === "in_the_past" ? "定的时间已经过了，先在编辑里改成以后的时间" : taskErrorText(e, resume ? "没恢复成" : "没暂停成"));
    }
  };
  // 删：先从页面上拿掉（等撤销），焦点放到“新建”上（按的那个钮跟着卡片没了）
  const remove = (t) => {
    setFailed(null);
    hide(t.id, true);
    newBtn.current?.focus({ preventScroll: true });
    undo.push({
      kind: "task",
      text: `已删除定时任务“${t.title}”`,
      commit: ({ keepalive }) => deleteScheduledTask(t.id, { keepalive }).then(() => {
        seq.current += 1;
        setData((d) => (d ? { ...d, tasks: d.tasks.filter((x) => x.id !== t.id) } : d));
        hide(t.id, false);
      }).catch((e) => {
        hide(t.id, false);
        setFailed(errorText("那个定时任务没删成", e));
      }),
      undo: () => hide(t.id, false),
    });
  };

  const menuTask = menu && tasks.find((t) => t.id === menu.id);
  const sz = nav?.mobile ? 18 : 15;
  const menuItems = (t) => [
    t.thread_id && { key: "open", icon: <MessageSquare size={sz} strokeWidth={1.8} />, text: "打开对话", onClick: () => { setMenu(null); onOpen(t.thread_id); } },
    nav?.mobile && { key: "edit", icon: <IconEdit size={sz} />, text: "编辑", onClick: () => { setMenu(null); openForm({ task: t }); } },
    t.status !== "done" && (t.status === "paused"
      ? { key: "resume", icon: <Play size={sz} strokeWidth={1.8} />, text: "恢复", onClick: () => { setMenu(null); toggle(t); } }
      : { key: "pause", icon: <Pause size={sz} strokeWidth={1.8} />, text: "暂停", onClick: () => { setMenu(null); toggle(t); } }),
    { key: "delete", icon: <IconTrash size={sz} />, text: "删除", danger: true, onClick: () => { setMenu(null); remove(t); } },
  ].filter(Boolean);

  let body;
  if (guest) {
    body = (
      <div className={styles.empty}>
        <IconClock size={28} className={styles.emptyIcon} />
        <b>登录后才能用定时任务</b>
        <p>{ABOUT}</p>
        {onLogin && <button className="ws-btn primary" onClick={onLogin}>登录</button>}
      </div>
    );
  } else if (error) {
    body = <div className={styles.status}>{error} <button className="ws-btn sm" onClick={() => load()}>再试一次</button></div>;
  } else if (!data) {
    body = <div className={styles.status} role="status"><Spinner size={14} />正在取定时任务…</div>;
  } else {
    body = (
      <div className={styles.wrap}>
        {failed && <div className="ws-error" role="alert">{failed}</div>}
        {!tasks.length && (
          <div className={styles.none}>
            <b>还没有定时任务</b>
            <p>{ABOUT}</p>
          </div>
        )}
        {GROUPS.map(([key, name]) => {
          const items = tasks.filter((t) => t.status === key);
          if (!items.length) return null;
          return (
            <section key={key} aria-label={name}>
              <h2 className={styles.groupHead}>{name}</h2>
              <ul className={styles.grid}>
                {items.map((t) => (
                  <TaskCard key={t.id} t={t} mobile={nav?.mobile} acting={acting === t.id} menuOpen={menu?.id === t.id}
                    onOpen={() => open(t)} onRun={() => run(t)} onEdit={() => openForm({ task: t })}
                    onMenu={(anchor) => setMenu(menu?.id === t.id ? null : { id: t.id, anchor })} />
                ))}
              </ul>
            </section>
          );
        })}
        <section aria-label="为你推荐">
          <h2 className={styles.groupHead}>为你推荐</h2>
          <ul className={styles.grid}>
            {TEMPLATES.map((p) => (
              <li key={p.title}>
                <button className={styles.preset} disabled={full} title={full ? `最多 ${data.limit} 个定时任务` : undefined} onClick={() => openForm({ preset: p })}>
                  <span className={styles.presetText}>
                    <span className={styles.cardTitle}>{p.title}</span>
                    <span className={styles.presetAbout}>{p.about}</span>
                  </span>
                  <Plus size={18} strokeWidth={1.8} className={styles.presetPlus} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>
    );
  }

  const send = () => {
    const text = ask.trim();
    if (text) onAsk(text);
  };
  return (
    <section ref={page} className={styles.page} aria-label="定时任务">
      <HeadBar title="定时任务" end={!guest && data && (
        <button ref={newBtn} className={cx("ws-btn sm", styles.new)} disabled={full} title={full ? `最多 ${data.limit} 个定时任务` : undefined} onClick={() => openForm({})}>
          <Plus size={14} strokeWidth={2} />新建
        </button>
      )} />
      <div className={styles.scroll}>{body}</div>
      {!guest && data && <AskBox value={ask} onChange={setAsk} onSend={send} />}
      {menuTask && (
        <ActionMenu label={`“${menuTask.title}”的更多操作`} anchor={menu.anchor} mobile={nav?.mobile} onClose={() => setMenu(null)} items={menuItems(menuTask)} />
      )}
      {form && (
        <TaskForm task={form.task} preset={form.preset} models={models} onClose={() => setForm(null)}
          onSaved={(t) => { put(t); setForm(null); }} />
      )}
    </section>
  );
}

// 一个任务：上面整块点了进它的对话（还没跑过的打开编辑）；下面一行是什么时候运行，右边三个钮
function TaskCard({ t, mobile, acting, menuOpen, onOpen, onRun, onEdit, onMenu }) {
  const running = t.last_status === "running";
  const note = noteOf(t);
  return (
    <li className={cx(styles.card, menuOpen && styles.open)}>
      <button className={styles.cardMain} onClick={onOpen} aria-label={`${t.title}，${t.schedule}${t.thread_id ? "，打开对话" : "，编辑"}`}>
        <span className={styles.cardHead}>
          <span className={styles.cardTitle} title={t.title}>{t.title}</span>
          {running && <span className={styles.running}><Spinner size={12} />正在运行</span>}
        </span>
        <span className={styles.cardPrompt}>{t.prompt}</span>
        {note && <span className={styles.cardNote}>{note}</span>}
      </button>
      <div className={styles.cardFoot}>
        <span className={styles.when} title={t.schedule}><IconClock size={14} /><span>{t.schedule}</span></span>
        <span className={styles.acts}>
          <button className="ws-icon-btn" aria-label={`运行“${t.title}”`} title="运行任务" disabled={running || acting} aria-busy={acting || undefined} onClick={onRun}>
            {acting ? <Spinner size={14} /> : <Play size={15} strokeWidth={1.8} />}
          </button>
          {!mobile && <button className="ws-icon-btn" aria-label={`编辑“${t.title}”`} title="编辑" onClick={onEdit}><IconEdit size={15} /></button>}
          <button className="ws-icon-btn" aria-label={`“${t.title}”的更多操作`} title="更多" aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={menuOpen}
            onClick={(e) => onMenu(e.currentTarget)}><IconMore size={16} /></button>
        </span>
      </div>
    </li>
  );
}

// 最底下的输入框：样子和对话的输入框一样（用它的样式），回车或点按钮发出去
function AskBox({ value, onChange, onSend }) {
  const ref = useRef(null);
  useAutoHeight(ref, value);
  return (
    <Dock>
      <div className={styles.ask}>
        <div className={composer.box} onClick={(e) => { if (e.target === e.currentTarget || e.target.hasAttribute("data-blank")) ref.current?.focus(); }}>
          <textarea ref={ref} rows={1} value={value} aria-label="说一句话建定时任务" placeholder={ASK_HINT} maxLength={2000}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => { if (enterSends(e)) { e.preventDefault(); onSend(); } }} />
          <div className={styles.askTools} data-blank>
            <button className={composer.send} aria-label="发送" disabled={!value.trim()} onClick={onSend}><IconArrowUp size={15} /></button>
          </div>
        </div>
      </div>
    </Dock>
  );
}

// 几选一的一排（重复、对话还是工作任务）：左右方向键换
function Seg({ label, value, options, onChange, className }) {
  const box = useRef(null);
  const move = (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const i = options.findIndex(([v]) => v === value);
    const next = (i + (e.key === "ArrowRight" ? 1 : -1) + options.length) % options.length;
    onChange(options[next][0]);
    box.current?.children[next]?.focus();
  };
  return (
    <div ref={box} className={cx(styles.seg, className)} role="radiogroup" aria-label={label} onKeyDown={move}>
      {options.map(([v, text]) => (
        <button key={String(v)} type="button" role="radio" aria-checked={value === v} tabIndex={value === v ? 0 : -1} onClick={() => onChange(v)}>{text}</button>
      ))}
    </div>
  );
}

// 新建、编辑的窗口。task：编辑的那个；preset：照哪个例子建。
// 新建时还选新开的那段对话是对话还是工作任务、用哪个模型（定下以后不变，和对话一样）；编辑时不放。
// “自定义”里写的那句话点“确定”（或回车）先转出来看一眼；没点就保存的，保存时先转
function TaskForm({ task, preset, models, onClose, onSaved }) {
  const from = task || preset;
  const [first] = useState(() => scheduleOf(from)); // 打开时什么时候运行：编辑时没动它就不发，免得“下一次”重新算
  const [title, setTitle] = useState(from?.title || "");
  const [prompt, setPrompt] = useState(from?.prompt || "");
  const [sch, setSch] = useState(first);
  const [work, setWork] = useState(false);
  const [model, setModel] = useState("");
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const [reading, setReading] = useState(false);
  const box = useRef(null);
  const titleRef = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  const ids = { head: useId(), title: useId(), prompt: useId(), when: useId(), model: useId(), err: useId() };
  useEffect(() => {
    const back = opener();
    if (touch()) box.current?.focus({ preventScroll: true }); // 触屏：光标不进输入框，不自己弹键盘
    else titleRef.current?.focus();
    return () => { if (back?.isConnected) back.focus({ preventScroll: true }); };
  }, []);
  useLayer(true, () => closing.current());
  const set = (v) => { setErr(null); setSch((s) => ({ ...s, ...v })); };
  const options = (models || []).filter((m) => !m.locked);

  // 把“自定义”里那句话转成时间规则，记下是哪句话转的；转完时框里已经改成别的了就不记
  const read = async (s) => {
    const text = s.text.trim();
    setReading(true);
    try {
      const got = { ...(await readSchedule(text)), from: text };
      setSch((cur) => (cur.text.trim() === text ? { ...cur, read: got } : cur));
      return { ...s, read: got };
    } finally {
      setReading(false);
    }
  };
  const stale = sch.mode === "custom" && !!sch.text.trim() && sch.read?.from !== sch.text.trim(); // 写了新的一句、还没转
  const confirm = () => {
    if (!stale || reading || saving) return;
    setErr(null);
    read(sch).catch((x) => setErr(taskErrorText(x, "没认出来")));
  };

  const submit = async (e) => {
    e.preventDefault();
    const v = { title: title.trim(), prompt: prompt.trim() };
    if (!v.title || !v.prompt || saving || reading) return;
    setSaving(true);
    setErr(null);
    try {
      const rule = ruleOf(stale ? await read(sch) : sch);
      if (!rule) {
        setSaving(false);
        setErr(MISSING[sch.mode === "weekly" && sch.days.length ? "daily" : sch.mode]);
        return;
      }
      const moved = JSON.stringify(rule) !== JSON.stringify(ruleOf(first));
      const body = task ? { ...v, ...(moved ? rule : {}) } : { ...v, ...rule, work, ...(model ? { model } : {}) };
      onSaved(task ? await updateScheduledTask(task.id, body) : await createScheduledTask(body));
    } catch (x) {
      setSaving(false);
      setErr(taskErrorText(x, task ? "没改成" : "没建成"));
    }
  };

  const shown = sch.mode === "custom" && !stale ? sch.read : null; // 转出来的、现在还作数的
  return (
    <div className={dialog.overlay} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form ref={box} tabIndex={-1} className={cx(dialog.dialog, styles.form)} role="dialog" aria-modal="true" aria-labelledby={ids.head} onSubmit={submit}
        onKeyDown={(e) => trapTab(e, box.current)}>
        <button type="button" className={dialog.close} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>
        <h2 id={ids.head} className={styles.formTitle}>{task ? "编辑定时任务" : "新建定时任务"}</h2>

        <div className={styles.fieldHead}>
          <label htmlFor={ids.title}>名称</label>
          <span className={cx("ws-count", title.length >= TITLE_MAX && "full")}>{title.length}/{TITLE_MAX}</span>
        </div>
        <input id={ids.title} ref={titleRef} className={styles.field} value={title} maxLength={TITLE_MAX} autoComplete="off" placeholder="比如：每日 AI 新闻简报"
          onChange={(e) => setTitle(e.target.value)} />

        <div className={styles.fieldHead}>
          <label htmlFor={ids.prompt}>要做的事</label>
          <span className={cx("ws-count", prompt.length >= PROMPT_MAX && "full")}>{prompt.length}/{PROMPT_MAX}</span>
        </div>
        <textarea id={ids.prompt} className={cx(styles.field, styles.area)} value={prompt} maxLength={PROMPT_MAX} rows={4}
          placeholder="每次到点了要灵稿做什么。写清楚查什么、要多少、结果什么样子，到时候没法再问你" onChange={(e) => setPrompt(e.target.value)} />

        {!task && (
          <div className={styles.pair}>
            <div>
              <div className={styles.fieldHead}><span>类型</span></div>
              <Seg label="对话还是工作任务" value={work} options={[[false, "对话"], [true, "工作任务"]]} onChange={setWork} />
            </div>
            {options.length > 0 && (
              <div className={styles.grow}>
                <div className={styles.fieldHead}><label htmlFor={ids.model}>模型</label></div>
                <select id={ids.model} className={cx(styles.field, styles.select)} value={model} onChange={(e) => setModel(e.target.value)}>
                  {options.map((m, i) => <option key={m.id} value={i ? m.id : ""}>{m.name || m.id}</option>)}
                </select>
              </div>
            )}
          </div>
        )}

        <div className={styles.fieldHead}><span>重复</span></div>
        <Seg label="重复" className={styles.modes} value={sch.mode} options={MODES} onChange={(mode) => set({ mode })} />
        <div className={styles.rule}>
          {sch.mode === "hourly" && (
            <label className={styles.inline}>每小时的第
              <input className={cx(styles.field, styles.minute)} type="number" inputMode="numeric" min={0} max={59} value={sch.minute}
                onChange={(e) => { const v = e.target.value.replace(/\D/g, "").slice(0, 2); set({ minute: +v > 59 ? "59" : v }); }} />分
            </label>
          )}
          {sch.mode === "weekly" && (
            <div className={styles.days} role="group" aria-label="每周哪几天">
              {DAYS.map(([d, name]) => (
                <button key={d} type="button" aria-pressed={sch.days.includes(d)} aria-label={`周${name}`}
                  onClick={() => set({ days: sch.days.includes(d) ? sch.days.filter((x) => x !== d) : [...sch.days, d] })}>{name}</button>
              ))}
            </div>
          )}
          {(sch.mode === "daily" || sch.mode === "weekly") && (
            <input className={cx(styles.field, styles.time)} type="time" aria-label="运行时间" value={sch.time} onChange={(e) => set({ time: e.target.value })} />
          )}
          {sch.mode === "once" && (
            <input className={cx(styles.field, styles.date)} type="datetime-local" aria-label="运行时间" value={sch.once} min={beijing(Date.now() + 60000)}
              onChange={(e) => set({ once: e.target.value })} />
          )}
          {sch.mode === "custom" && (
            <div className={styles.custom}>
              <div className={styles.customRow}>
                <input id={ids.when} className={styles.field} value={sch.text} maxLength={WHEN_MAX} autoComplete="off" aria-label="什么时候运行" placeholder={WHEN_HINT}
                  onChange={(e) => set({ text: e.target.value })}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); confirm(); } }} />
                <button type="button" className="ws-btn" disabled={!stale || reading || saving} aria-busy={reading || undefined} onClick={confirm}>
                  {reading && <Spinner size={13} />}确定
                </button>
              </div>
              {shown && <div className={styles.read} role="status"><IconCheck size={14} />{shown.schedule}</div>}
            </div>
          )}
        </div>
        {ABROAD && sch.mode !== "custom" && <div className={styles.zone}>按北京时间</div>}

        {err && <div id={ids.err} className="ws-error" role="alert">{err}</div>}
        <div className={styles.formActions}>
          <button type="button" className="ws-btn" onClick={onClose}>取消</button>
          <button type="submit" className="ws-btn primary" disabled={!title.trim() || !prompt.trim() || saving || reading} aria-busy={saving || undefined}>
            {saving && <Spinner size={13} />}{task ? "保存" : "创建"}
          </button>
        </div>
      </form>
    </div>
  );
}
