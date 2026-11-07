// 登录：左栏下面的登录入口和账号菜单、额度按钮和面板、登录弹窗（邮箱验证码、GitHub、Google）、/auth/callback 页。身份本身见 auth.js。
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { EmailLogin, checkSession, completeCallback, emailLoginAvailable, emailLoginKnown, init, signIn, signOut, snapshot, subscribeAuth } from "./auth.js";
import { IconClose, IconLock, Spinner } from "./icons.jsx";
import { opener, useLayer } from "./layers.js";
import { touch } from "./nav.jsx";
import { asksOf, creditsLeft, creditsLine, creditsParts, creditsShort, loginLine, num } from "./research.js";
import { Sheet } from "./sheet.jsx";

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

// 左栏下面：游客看到“登录”，登录了看到头像和名字，点开是账号菜单（谁、退出登录）。手机上是底部弹层（规范：菜单用 bottom sheet）。
// 记忆不在这里：2026-11-07 Sean 定放回左栏“新对话”下面一行，游客也看得见（推翻 11-06 收进这里的做法，评审第 30 条）
export function UserFoot({ auth, onLogin, mobile }) {
  const [open, setOpen] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const btn = useRef(null);
  const box = useRef(null);
  usePopClose(open && !mobile, box, btn, () => setOpen(false));
  if (!auth.ready || !auth.login) return null;
  if (!auth.me) return <button className="ws-login-link" onClick={onLogin}>登录</button>;
  const { me } = auth;
  const label = me.name || me.email || "已登录";
  const close = () => setOpen(false);
  const leave = () => { setLeaving(true); signOut().catch(() => setLeaving(false)); };
  const note = "退出后在 Fusion 等其他应用也会退出。";
  const who = (
    <div className="ws-menu-note ws-user-note">
      <b>{me.name || me.email}</b>
      {me.name && me.email && <span>{me.email}</span>}
      {me.tier === "admin" && <span>管理员</span>}
    </div>
  );
  const rect = open && !mobile ? btn.current?.getBoundingClientRect() : null;
  return (
    <>
      <button ref={btn} className="ws-user-btn" aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={open} title={mobile ? undefined : me.email} onClick={() => setOpen(!open)}>
        <Avatar me={me} />
        <span className="ws-user-name">{label}</span>
      </button>
      {open && mobile && onWs(
        <Sheet title="账号" onClose={close} className="ws-act-sheet">
          {who}
          <button className="ws-sheet-act" disabled={leaving} onClick={leave}>{leaving ? <Spinner size={18} /> : <IconClose size={18} />}退出登录</button>
          <div className="ws-menu-note">{note}</div>
        </Sheet>,
      )}
      {rect && (
        <div ref={box} className="ws-menu ws-user-menu" role="menu" aria-label="账号" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: 232 }}>
          {who}
          <button role="menuitem" disabled={leaving} onClick={leave}>
            {leaving ? <Spinner size={14} /> : <IconClose size={14} />}退出登录
          </button>
          <div className="ws-menu-note">{note}</div>
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

// 左栏最下面常驻的额度按钮：小圆环画剩余比例，旁边写“约可问 N 次”（2026-11-07 Sean 定，第 27 条）；管理员写“额度不限”。
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
  const cls = "ws-credits-btn" + (left?.exhausted ? " out" : left?.warn ? " warn" : "");
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
      {open && mobile && onWs(<Sheet title="额度" onClose={close} className="ws-usage-sheet">{panel}</Sheet>)}
      {rect && (
        <div ref={box} className="ws-menu ws-usage" role="dialog" aria-label="额度" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: 264 }}>
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
    <svg className="ws-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeOpacity=".18" strokeWidth="2.5" />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c * (1 - frac)} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
    </svg>
  );
}

// 额度面板：完整的一句（“今天还剩 7,300 额度，约可问 36 次 · 明天 0 点恢复”，和输入框上面的提醒、用完时的报错同一种说法），
// 额度来源，怎么估的；游客下面是去登录的按钮，写着登录后会怎样。只写最紧的那个额度，日、周不并排列（第 27 条）
function UsagePanel({ credits, left, guest, mobile, onLogin }) {
  if (credits.unlimited) {
    const used = usedBy(credits);
    return (
      <>
        <div className="ws-usage-main">额度不限</div>
        <div className="ws-menu-note">管理员。今天用了 {num(used.day)} 额度，这周 {num(used.week)}；按北京时间算。</div>
      </>
    );
  }
  const after = guest ? loginLine(credits) : null;
  const [amount, when] = creditsParts(left);
  const [more, asks] = after ? after.split("，") : [];
  return (
    <>
      {/* 和别处同一句，分两行：还剩多少 / 什么时候恢复（中间的“ · ”只是不显示，读屏、复制照旧是整句） */}
      <div className={"ws-usage-main" + (left.exhausted ? " out" : left.warn ? " warn" : "")}>
        {amount}<span className="ws-usage-when"><span className="sep"> · </span>{when}</span>
      </div>
      <div className="ws-menu-note">
        {guest ? "游客试用的额度。" : "登录账号的额度。"}
        {credits.per_ask > 0 && `按一次约 ${num(credits.per_ask)} 额度估算，联网查得多、开着深度思考的问题会用得多一些。`}按北京时间算。
      </div>
      {after && onLogin && (
        <button className={"ws-usage-login" + (mobile ? " ws-sheet-act" : "")} onClick={onLogin}>
          <IconLock size={mobile ? 18 : 14} /><span>{more}{asks && <>，<span className="ws-keep">{asks}</span></>}</span>
        </button>
      )}
    </>
  );
}

function Avatar({ me }) {
  const [broken, setBroken] = useState(false);
  if (me.avatar && !broken) return <img className="ws-avatar" src={me.avatar} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />;
  return <span className="ws-avatar">{(me.name || me.email || "?").trim()[0].toUpperCase()}</span>;
}

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])';

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
    const to = touch() ? el : el.querySelector("input:not(:disabled), .ws-login-btn:not(:disabled)") || el;
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
  const asks = asksOf(credits?.login?.day ?? crowded?.login?.day ?? 0, credits?.per_ask ?? crowded?.per_ask);
  const desc = crowded
    ? `你所在的网络今天新来的游客太多了（防刷，同一网络每天最多 ${crowded.per_ip || 5} 个游客身份）。登录后不受这个限制${asks ? `，每天约可问 ${asks} 次` : "，马上能接着用"}。`
    : `${want ? `登录后就能用 ${want.name}。` : ""}登录后能用全部模型${asks ? `，每天约可问 ${asks} 次` : ""}；对话和记忆按账号保存，现在的对话会一起并进账号。`;
  return (
    <div className="ws-overlay" onMouseDown={(e) => e.target === e.currentTarget && !critical && onClose()}>
      <div ref={box} className="ws-dialog" role="dialog" aria-modal="true" aria-labelledby="ws-login-title" aria-describedby="ws-login-desc" tabIndex={-1} onKeyDown={trap}>
        {!critical && <button className="ws-dialog-close" aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>}
        {phase === "start" && (
          <>
            <h2 id="ws-login-title" className="ws-dialog-title">{crowded ? "登录后接着用" : "登录灵稿"}</h2>
            <p id="ws-login-desc" className="ws-dialog-desc">{desc}</p>
          </>
        )}
        {email === null && phase === "start" && <div className="ws-login-wait" role="status"><Spinner size={14} />正在准备邮箱登录…</div>}
        {email && <EmailFlow phase={phase} setPhase={setPhase} setCritical={setCritical} blocked={!!going} />}
        {phase === "start" && (
          <>
            {email !== false && <div className="ws-login-or">或者</div>}
            <div className="ws-login-methods">
              <button className="ws-login-btn" disabled={!!going} onClick={() => go("github")}>{going === "github" ? <Spinner size={15} /> : <GitHubMark />}用 GitHub 登录</button>
              <button className="ws-login-btn" disabled={!!going} onClick={() => go("google")}>
                {going === "google" ? <Spinner size={15} /> : <GoogleMark />}用 Google 登录<small>需要能打开 Google</small>
              </button>
            </div>
            {error && <div className="ws-login-err" role="alert">{error}</div>}
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
  const err = error && <div id={ids + "err"} className="ws-login-err" role="alert">{error}</div>;
  if (phase !== "code" || !sent) {
    return (
      <form className="ws-login-email" onSubmit={start} noValidate>
        <label className="ws-field-label" htmlFor={ids + "addr"}>邮箱</label>
        <input ref={input} id={ids + "addr"} className="ws-field" type="email" autoComplete="email" inputMode="email" placeholder="name@example.com" value={addr} disabled={busy}
          aria-invalid={error ? true : undefined} aria-describedby={error ? ids + "err" : undefined} onChange={(e) => setAddr(e.target.value)} />
        {err}
        <button type="submit" className="ws-btn primary ws-login-send" disabled={busy || blocked || !addr.trim()}>{busy && <Spinner size={14} />}发送验证码</button>
        <div className="ws-login-tip">没有账号会直接注册。</div>
      </form>
    );
  }
  return (
    <form onSubmit={verify} noValidate>
      <h2 id="ws-login-title" className="ws-dialog-title">输入验证码</h2>
      <p id="ws-login-desc" className="ws-dialog-desc">验证码已发到 {sent.masked}{sent.expiresIn > 0 && `，${Math.max(1, Math.round(sent.expiresIn / 60))} 分钟内有效`}。</p>
      <label className="ws-field-label" htmlFor={ids + "code"}>验证码<span>{sent.codeLength} 位数字</span></label>
      <input ref={input} id={ids + "code"} className="ws-field code" inputMode="numeric" autoComplete="one-time-code" maxLength={sent.codeLength}
        value={code} disabled={busy} aria-invalid={error ? true : undefined} aria-describedby={error ? ids + "err" : undefined}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
      {err}
      <div className="ws-dialog-actions">
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
    <div className="ws ws-callback">
      {error ? (
        <div className="ws-callback-box" role="alert">
          <div>{error}</div>
          <a href="/">回到首页</a>
        </div>
      ) : (
        <div className="ws-callback-box"><Spinner size={22} />{interactive && <span>正在完成登录…</span>}</div>
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
