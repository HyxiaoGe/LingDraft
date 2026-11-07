// 登录：左栏下面的账号菜单（游客的有登录、设置）、额度按钮和面板、登录弹窗（邮箱验证码、GitHub、Google）、/auth/callback 页。身份本身见 auth.js。
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { EmailLogin, checkSession, completeCallback, emailLoginAvailable, emailLoginKnown, init, signIn, signOut, snapshot, subscribeAuth } from "./auth.js";
import { IconClose, IconLock, IconLogin, IconSliders, Spinner } from "./icons.jsx";
import { opener, useLayer } from "./layers.js";
import { touch } from "./nav.jsx";
import { creditsLeft, creditsLine, creditsShort, loginLine, num } from "./research.js";
import { Sheet } from "./sheet.jsx";
import dialog from "./dialog.module.css";
import styles from "./login.module.css";
import menu from "./menu.module.css";
import sheet from "./sheet.module.css";
import side from "./sidebar.module.css";

// 当前身份：{ready, login, me}（见 auth.snapshot）。回到这个标签页时和 auth 服务对一下账（别处退出了就跟着退）
export function useAuth() {
  const [s, setS] = useState(snapshot);
  useEffect(() => {
    const off = subscribeAuth(setS);
    init().then(() => setS(snapshot()));
    const on = () => document.visibilityState === "visible" && checkSession();
    document.addEventListener("visibilitychange", on);
    addEventListener("focus", on);
    return () => {
      off();
      document.removeEventListener("visibilitychange", on);
      removeEventListener("focus", on);
    };
  }, []);
  return s;
}

// 左栏下面：头像和名字，点开是账号菜单。手机上是底部弹层。
// 设置（2026-11-07 Sean 定）：登录了是“谁、设置、退出登录”；游客也有一个“游客”头像，菜单里是“登录、设置”。
// 记忆只从设置窗口里进，这里和左栏都不放。没有登录功能的部署没有账号，只留一个“设置”
export function UserFoot({ auth, onLogin, onSettings, mobile }) {
  const [open, setOpen] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const btn = useRef(null);
  const box = useRef(null);
  usePopClose(open && !mobile, box, btn, () => setOpen(false));
  if (!auth.ready) return null;
  if (!auth.login) {
    return <button className="min-h-6 border-0 bg-transparent p-0 text-[12px] text-accent hover:underline hover:underline-offset-3 phone:min-h-11 phone:min-w-11 phone:px-2"
      onClick={onSettings}>设置</button>;
  }
  const { me } = auth;
  const label = me ? me.name || me.email || "已登录" : "游客";
  const close = () => setOpen(false);
  // 点了一项：菜单收起，焦点先回头像按钮，再打开的窗口（设置、登录）关上以后就回到这里（桌面；手机上底部弹层自己把焦点还回去）
  const pick = (fn) => () => { close(); if (!mobile) btn.current?.focus({ preventScroll: true }); fn?.(); };
  const leave = () => { setLeaving(true); signOut().catch(() => setLeaving(false)); };
  // 名字就是邮箱 @ 前面那段的（邮箱登录没填名字时就是这样）只写邮箱，不上下各写一遍
  const named = me?.name && me.name !== me.email?.split("@")[0] ? me.name : null;
  const who = me && (
    <div className={menu.note + " " + styles.who}>
      <b>{named || me.email || me.name}</b>
      {named && me.email && <span>{me.email}</span>}
      {me.tier === "admin" && <span>管理员</span>}
    </div>
  );
  // 每一项：[图标, 字, 点了做什么, 正在退出时转圈]
  const items = me
    ? [[IconSliders, "设置", pick(onSettings)], [IconClose, "退出登录", leave, leaving]]
    : [[IconLogin, "登录", pick(onLogin)], [IconSliders, "设置", pick(onSettings)]];
  const rect = open && !mobile ? btn.current?.getBoundingClientRect() : null;
  return (
    <>
      <button ref={btn} className={"-my-1 -mr-1 ml-0 flex h-6 max-w-[120px] min-w-0 items-center gap-1.5 rounded-sm border-0 bg-transparent px-1 py-0 text-[12px] text-muted"
        + " hover:bg-hover hover:text-ink aria-expanded:bg-hover aria-expanded:text-ink phone:m-0 phone:h-11"}
        aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={open} title={mobile || !me ? undefined : me.email} onClick={() => setOpen(!open)}>
        <Avatar me={me} />
        <span className="min-w-0 truncate">{label}</span>
      </button>
      {open && mobile && onWs(
        <Sheet title="账号" onClose={close}>
          {who}
          {items.map(([Icon, text, fn, busy]) => (
            <button key={text} className={sheet.act} disabled={leaving} onClick={fn}>{busy ? <Spinner size={18} /> : <Icon size={18} />}{text}</button>
          ))}
        </Sheet>,
      )}
      {rect && (
        <div ref={box} className={menu.menu} role="menu" aria-label="账号" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: me ? 232 : 160 }}>
          {who}
          {items.map(([Icon, text, fn, busy]) => (
            <button key={text} role="menuitem" disabled={leaving} onClick={fn}>{busy ? <Spinner size={14} /> : <Icon size={14} />}{text}</button>
          ))}
        </div>
      )}
    </>
  );
}

// 桌面上的小浮层：点外面、按 Esc 收起（Esc 时焦点回到按钮）
function usePopClose(on, box, btn, close) {
  const closing = useRef(close);
  closing.current = close;
  useEffect(() => {
    if (!on) return;
    const down = (e) => !box.current?.contains(e.target) && !btn.current?.contains(e.target) && closing.current();
    addEventListener("mousedown", down);
    return () => removeEventListener("mousedown", down);
  }, [on, box, btn]);
  useLayer(on, () => { closing.current(); btn.current?.focus(); });
}

// 弹层挂到 .ws 里：颜色等变量定义在 .ws 上；手机上左栏是带 transform 的抽屉，挂在里面 fixed 会跟着抽屉走
const onWs = (node) => createPortal(node, document.querySelector(".ws") || document.body);

// 左栏最下面常驻的额度按钮：小圆环画剩余比例，旁边写剩多少（2026-11-07 Sean 定：不换算成次数）；管理员写“额度不限”。
// 显示最紧的那个额度，用到八成变黄、用完变红；没取到不显示。点开是额度面板（UsagePanel，手机上是底部弹层），完整那句在面板里
export function CreditsFoot({ credits, guest, onLogin, onOpen, mobile }) {
  const [open, setOpen] = useState(false);
  const btn = useRef(null);
  const box = useRef(null);
  usePopClose(open && !mobile, box, btn, () => setOpen(false));
  if (!credits) return null;
  const left = credits.unlimited ? null : creditsLeft(credits);
  if (!credits.unlimited && !left) return null;
  const frac = left ? Math.max(0, Math.min(1, left.remaining / left.limit)) : 1;
  const cls = side.credits + (left?.exhausted ? " " + side.out : left?.warn ? " " + side.warn : "");
  const line = credits.unlimited ? `额度不限，今天用了 ${num(usedBy(credits).day)} 额度` : creditsLine(left);
  const close = () => setOpen(false);
  const panel = <UsagePanel credits={credits} left={left} guest={guest} mobile={mobile} onLogin={onLogin && (() => { close(); onLogin(); })} />;
  const rect = open && !mobile ? btn.current?.getBoundingClientRect() : null;
  return (
    <>
      <button ref={btn} className={cls} title={mobile ? undefined : line} aria-label={"额度：" + line} aria-haspopup="dialog" aria-expanded={open}
        onClick={() => { if (!open) onOpen?.(); setOpen(!open); }}>
        <Ring frac={frac} />
        <span>{credits.unlimited ? "额度不限" : creditsShort(left)}</span>
      </button>
      {open && mobile && onWs(<Sheet title="额度" onClose={close} className={styles.usageSheet}>{panel}</Sheet>)}
      {rect && (
        <div ref={box} className={menu.menu + " " + styles.usage} role="dialog" aria-label="额度" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: 264 }}>
          {panel}
        </div>
      )}
    </>
  );
}

// 今天、这周用了多少；没有那个窗口算 0
function usedBy(credits) {
  const of = (kind) => credits.windows.find((w) => w.kind === kind)?.used || 0;
  return { day: of("day"), week: of("week") };
}

// 剩余比例的小圆环：frac 是剩下的份额（1 满、0 空），颜色跟着按钮的 warn / out 走
function Ring({ frac, size = 16 }) {
  const r = (size - 3) / 2;
  const c = 2 * Math.PI * r;
  return (
    <svg className={side.ring} width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeOpacity=".18" strokeWidth="2.5" />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c * (1 - frac)} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
    </svg>
  );
}

// 离重置还有多久：“05:32:10”，一天以上前面加“2 天 ”；过了就是 00:00:00（面板开着时每秒走一下，额度随后会重新取）
function countdown(iso, now) {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "—";
  const s = Math.max(0, Math.floor((at - now) / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  const d = Math.floor(s / 86400);
  return `${d ? `${d} 天 ` : ""}${pad(Math.floor(s / 3600) % 24)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

// 额度面板（2026-11-07 Sean 定：简单点，照他给的样子）：两行，左边是名目，右边是数——“今日所剩额度 7,300”“重置倒计时 05:32:10”。
// 只写最紧的那个额度（周的更紧就写“本周所剩额度”，倒计时到下周一 0 点），不换算成次数、不写怎么估的；游客下面是去登录的按钮，写着登录后每天多少。
// 管理员是今日、本周各用了多少
function UsagePanel({ credits, left, guest, mobile, onLogin }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (credits.unlimited) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [credits.unlimited]);
  const rows = (list, tone) => (
    <dl className={styles.usageRows + (tone ? " " + styles[tone] : "")}>
      {list.map(([k, v]) => <div key={k} className={styles.usageRow}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  );
  if (credits.unlimited) {
    const used = usedBy(credits);
    return rows([["今日已用额度", num(used.day)], ["本周已用额度", num(used.week)]]);
  }
  const after = guest ? loginLine(credits) : null;
  return (
    <>
      {rows([[`${left.kind === "week" ? "本周" : "今日"}所剩额度`, num(left.remaining)], ["重置倒计时", countdown(left.resets_at, now)]],
        left.exhausted ? "out" : left.warn ? "warn" : null)}
      {after && onLogin && (
        <button className={styles.usageLogin + (mobile ? " " + sheet.act : "")} onClick={onLogin}>
          <IconLock size={mobile ? 18 : 14} /><span>{after}</span>
        </button>
      )}
    </>
  );
}

// 头像：有图用图（取不到退回首字母），没有图写名字或邮箱的首字母；游客（me 是 null）是浅一档的“游”。large：设置窗口左上角那个大的
export function Avatar({ me, large }) {
  const [broken, setBroken] = useState(false);
  const cls = styles.avatar + (me ? "" : " " + styles.guest) + (large ? " " + styles.large : "");
  if (!me) return <span className={cls} aria-hidden="true">游</span>;
  if (me.avatar && !broken) return <img className={cls} src={me.avatar} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />;
  return <span className={cls}>{(me.name || me.email || "?").trim()[0].toUpperCase()}</span>;
}

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])';
const ERR = "mt-2 text-[13px] leading-[1.5] text-danger"; // 出错的那句（弹窗里、邮箱那栏下面）

// 登录弹窗（第 29 条，2026-11-07 Sean 定）：邮箱验证码在最上面、直接填；GitHub、Google 在“或者”下面，Google 标着要能打开它。
// 邮箱能不能用页面起来时就问好了（auth.js 的 emailLoginAvailable），还没问到的那一会儿给邮箱那栏留着位置。
// 焦点（待确认第 7 条）：打开时进到弹窗里（桌面落在第一个能填、能点的地方；触屏落在弹窗本身，不自己弹出键盘），Tab 在弹窗里转，
// 关掉回到打开它的地方。验证码换登录的那几秒不能关（关了这次验证码就作废，用户还以为在登录）。
// crowded：是因为领不到游客身份弹出来的（auth.js），说清为什么、登录后会怎样（第 26 条）；want：游客点了锁着的模型，说登录后就能用它（第 28 条）；
// credits：现在的额度（游客的里面有登录后每天多少）
export function LoginDialog({ open, onClose, crowded, want, credits }) {
  const [phase, setPhase] = useState("start"); // start：填邮箱、或者选别的方式；code：填验证码
  const [going, setGoing] = useState(null); // 正在跳去的 provider
  const [asked, setAsked] = useState(null); // 打开以后问到的邮箱验证码能不能用
  const [critical, setCritical] = useState(false);
  const [error, setError] = useState(null);
  const box = useRef(null);
  const autoFocused = useRef(null); // 弹窗自己放焦点的地方：焦点还在这里就算用户没动过
  const criticalRef = useRef(false);
  criticalRef.current = critical;
  const closing = useRef(onClose);
  closing.current = onClose;
  useEffect(() => {
    if (!open) return;
    setPhase("start");
    setGoing(null);
    setError(null);
    setAsked(null);
    emailLoginAvailable().then(setAsked);
    const back = opener(); // Safari 点按钮不给按钮焦点：用刚按下的那个按钮
    return () => { if (back?.isConnected) back.focus({ preventScroll: true }); };
  }, [open]);
  useLayer(open, () => { if (!criticalRef.current) closing.current(); }); // 验证码对了、正在换登录的那几秒不能关
  // 邮箱验证码能不能用：页面起来时多半已经问到了（emailLoginKnown），打开的第一下就用它，不先闪一下转圈；null 是还没问到
  const email = asked ?? emailLoginKnown();
  // 焦点进弹窗：打开时、邮箱那栏刚问到时，落到第一个能填能点的地方。用户自己挪过（不在弹窗放的地方了）就不动
  useEffect(() => {
    const el = box.current;
    if (!open || !el || phase !== "start") return;
    const a = document.activeElement;
    if (el.contains(a) && a !== el && a !== autoFocused.current) return;
    const to = touch() ? el : el.querySelector("input:not(:disabled), [data-method]:not(:disabled)") || el;
    autoFocused.current = to;
    to.focus({ preventScroll: true });
  }, [open, email, phase]);
  if (!open) return null;
  const go = (provider) => {
    setGoing(provider);
    setError(null);
    signIn(provider).catch(() => { setGoing(null); setError("没能打开登录页：检查网络后再点一次；一直不行，可以换一种方式登录。"); });
  };
  // Tab 每一下都自己挪（不交给浏览器）：Safari 默认 Tab 跳过按钮，只在首尾拦的话从邮箱框一按就出了弹窗
  const trap = (e) => {
    if (e.key !== "Tab") return;
    const els = [...box.current.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
    if (!els.length) return;
    e.preventDefault();
    const i = els.indexOf(document.activeElement);
    const n = els.length;
    els[i < 0 ? (e.shiftKey ? n - 1 : 0) : (i + (e.shiftKey ? n - 1 : 1)) % n].focus();
  };
  const desc = crowded
    ? `你所在的网络今天新来的游客太多了（防刷，同一网络每天最多 ${crowded.per_ip || 5} 个游客身份）。登录后不受这个限制，马上能接着用。`
    : `${want ? `登录后就能用 ${want.name}。` : ""}登录后能用全部模型；对话和记忆按账号保存，现在的对话会一起并进账号。`;
  return (
    <div className={dialog.overlay} onMouseDown={(e) => e.target === e.currentTarget && !critical && onClose()}>
      <div ref={box} className={dialog.dialog} role="dialog" aria-modal="true" aria-labelledby="ws-login-title" aria-describedby="ws-login-desc" tabIndex={-1} onKeyDown={trap}>
        {!critical && <button className={dialog.close} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>}
        {phase === "start" && (
          <>
            <h2 id="ws-login-title" className={styles.title}>{crowded ? "登录后接着用" : "登录灵稿"}</h2>
            <p id="ws-login-desc" className={styles.desc}>{desc}</p>
          </>
        )}
        {email === null && phase === "start" && <div className="flex min-h-[144px] items-center justify-center gap-2 text-[13px] text-muted phone:min-h-[152px]" role="status"><Spinner size={14} />正在准备邮箱登录…</div>}
        {email && <EmailFlow phase={phase} setPhase={setPhase} setCritical={setCritical} blocked={!!going} />}
        {phase === "start" && (
          <>
            {email !== false && <div className={styles.or}>或者</div>}
            <div className="flex flex-col gap-2">
              <button className={styles.method} data-method="github" disabled={!!going} onClick={() => go("github")}>{going === "github" ? <Spinner size={15} /> : <GitHubMark />}用 GitHub 登录</button>
              <button className={styles.method} data-method="google" disabled={!!going} onClick={() => go("google")}>
                {going === "google" ? <Spinner size={15} /> : <GoogleMark />}用 Google 登录<small>需要能打开 Google</small>
              </button>
            </div>
            {error && <div className={ERR} role="alert">{error}</div>}
          </>
        )}
      </div>
    </div>
  );
}

// 邮箱验证码：填邮箱 → 发码 → 填码。填邮箱那步就在弹窗最上面（phase 是 start），发出去以后整个弹窗换成填验证码（phase 是 code）。
// 两个框都有看得见的标签（第 33 条：只靠占位符，填完就不知道这框是什么，读屏也会丢字段名），出错的那句连在框上（aria-describedby）。
// 重发有间隔；验证码对了换登录的那几秒告诉弹窗别关；换好了整页重载（身份变了，按新身份重取）
function EmailFlow({ phase, setPhase, setCritical, blocked }) {
  const [addr, setAddr] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(null); // {masked, codeLength, expiresIn, resendAt}
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now);
  const flow = useRef(null);
  const ctl = useRef(null);
  const input = useRef(null);
  const first = useRef(true);
  const ids = useId();
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(t);
      ctl.current?.abort();
      flow.current?.cancel();
    };
  }, []);
  useEffect(() => { // 换了一步（发出去了、回去换邮箱）：光标跟到这一步的框里；刚打开时由弹窗决定焦点
    if (first.current) { first.current = false; return; }
    input.current?.focus();
  }, [phase]);
  const run = async (fn, critical = false) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    if (critical) setCritical(true);
    ctl.current = new AbortController();
    try {
      await fn(ctl.current.signal);
    } catch (e) {
      if (e?.name === "AbortError") return;
      setError(e?.message || "出错了，再试一次"); // EmailLogin 抛的都是自己写的说法（EMAIL_ERRORS 等），SDK 的英文报错在 auth.js 里换掉了
      if (["interaction_expired", "interaction_consumed", "invalid_interaction", "flow_expired", "too_many_attempts"].includes(e?.code)) {
        setPhase("start");
        setCode("");
        setSent(null);
      }
    } finally {
      setBusy(false);
      if (critical) setCritical(false);
    }
  };
  const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addr.trim());
  const start = (e) => {
    e.preventDefault();
    if (!valid) return setError("邮箱地址的格式不对，再看一下");
    return run(async (signal) => {
      flow.current?.cancel();
      flow.current = new EmailLogin();
      const r = await flow.current.start(addr.trim(), signal);
      setSent({ ...r, resendAt: Date.now() + r.resendAfter * 1000 });
      setCode("");
      setPhase("code");
    });
  };
  const resend = () => run(async (signal) => {
    const r = await flow.current.resend(signal);
    setSent({ ...r, resendAt: Date.now() + r.resendAfter * 1000 });
  });
  const verify = (e) => {
    e.preventDefault();
    if (code.length !== (sent?.codeLength || 6)) return undefined;
    return run(async (signal) => {
      await flow.current.verify(code, signal);
      location.reload(); // 身份变了，整页按新身份重取
    }, true);
  };
  const wait = sent ? Math.max(0, Math.ceil((sent.resendAt - now) / 1000)) : 0;
  const err = error && <div id={ids + "err"} className={ERR} role="alert">{error}</div>;
  if (phase !== "code" || !sent) {
    return (
      <form className="flex flex-col" onSubmit={start} noValidate>
        <label className={styles.label} htmlFor={ids + "addr"}>邮箱</label>
        <input ref={input} id={ids + "addr"} className={styles.field} type="email" autoComplete="email" inputMode="email" placeholder="name@example.com" value={addr} disabled={busy}
          aria-invalid={error ? true : undefined} aria-describedby={error ? ids + "err" : undefined} onChange={(e) => setAddr(e.target.value)} />
        {err}
        <button type="submit" className={"ws-btn primary " + styles.send} disabled={busy || blocked || !addr.trim()}>{busy && <Spinner size={14} />}发送验证码</button>
        <div className="mt-2 text-center text-[12px] text-muted">没有账号会直接注册。</div>
      </form>
    );
  }
  return (
    <form onSubmit={verify} noValidate>
      <h2 id="ws-login-title" className={styles.title}>输入验证码</h2>
      <p id="ws-login-desc" className={styles.desc}>验证码已发到 {sent.masked}{sent.expiresIn > 0 && `，${Math.max(1, Math.round(sent.expiresIn / 60))} 分钟内有效`}。</p>
      <label className={styles.label} htmlFor={ids + "code"}>验证码<span>{sent.codeLength} 位数字</span></label>
      <input ref={input} id={ids + "code"} className={styles.field + " " + styles.code} inputMode="numeric" autoComplete="one-time-code" maxLength={sent.codeLength}
        value={code} disabled={busy} aria-invalid={error ? true : undefined} aria-describedby={error ? ids + "err" : undefined}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
      {err}
      <div className={styles.actions}>
        <button type="button" className="ws-btn ghost" disabled={busy} onClick={() => { setPhase("start"); setCode(""); setError(null); }}>换个邮箱</button>
        <button type="button" className="ws-btn ghost" disabled={busy || wait > 0} onClick={resend}>{wait > 0 ? `重新发送（${wait} 秒）` : "重新发送"}</button>
        <button type="submit" className="ws-btn primary" disabled={busy || code.length !== sent.codeLength}>{busy && <Spinner size={14} />}登录</button>
      </div>
    </form>
  );
}

// /auth/callback：auth 服务登录完跳回来落在这里，换好 token 回到原来那一页；退出登录的回跳也落在这里，回首页
export function AuthCallbackPage() {
  const [error, setError] = useState(null);
  const once = useRef(false);
  useEffect(() => {
    if (once.current) return;
    once.current = true;
    completeCallback().then((path) => location.replace(path))
      .catch((e) => setError(e?.code === "denied" ? "你取消了登录。回到首页可以接着用，想登录时再点登录。" : "登录没成功。回到首页再登录一次；一直不行，可以先不登录接着用。"));
  }, []);
  const interactive = new URLSearchParams(location.search).has("code");
  return (
    <div className="ws flex items-center justify-center">
      {error ? (
        <div className="flex flex-col items-center gap-2.5 text-ink" role="alert">
          <div>{error}</div>
          <a href="/">回到首页</a>
        </div>
      ) : (
        <div className="flex items-center gap-3 text-muted"><Spinner size={22} />{interactive && <span>正在完成登录…</span>}</div>
      )}
    </div>
  );
}

const GitHubMark = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
  </svg>
);

const GoogleMark = () => (
  <svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true">
    <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z" />
    <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18z" />
    <path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.95H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.05l3.01-2.33z" />
    <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.9 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z" />
  </svg>
);
