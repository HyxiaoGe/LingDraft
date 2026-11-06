// 登录：左栏下面的登录入口和用户菜单、登录弹窗（GitHub、Google、邮箱验证码）、/auth/callback 页。身份本身见 auth.js。
import { useEffect, useRef, useState } from "react";
import { EmailLogin, checkSession, completeCallback, emailLoginAvailable, init, signIn, signOut, snapshot, subscribeAuth } from "./auth.js";
import { IconBook, IconClose, IconLock, Spinner } from "./icons.jsx";
import { creditsLeft } from "./research.js";

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

// 左栏下面：游客看到“登录”，登录了看到头像和邮箱，点开能去记忆页、退出（2026-11-06 Sean 定：记忆入口收进这里，游客没有记忆）
export function UserFoot({ auth, onLogin, onOpen, memory, onMemory }) {
  const [open, setOpen] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const btn = useRef(null);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return;
    const down = (e) => !box.current?.contains(e.target) && !btn.current?.contains(e.target) && setOpen(false);
    const key = (e) => e.key === "Escape" && (setOpen(false), btn.current?.focus());
    addEventListener("mousedown", down);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("mousedown", down);
      removeEventListener("keydown", key);
    };
  }, [open]);
  if (!auth.ready || !auth.login) return null;
  if (!auth.me) return <button className="ws-login-link" onClick={onLogin}>登录</button>;
  const { me } = auth;
  const label = me.name || me.email || "已登录";
  const rect = open ? btn.current?.getBoundingClientRect() : null;
  return (
    <>
      <button ref={btn} className="ws-user-btn" aria-haspopup="menu" aria-expanded={open} title={me.email} onClick={() => setOpen(!open)}>
        <Avatar me={me} />
        <span className="ws-user-name">{label}</span>
      </button>
      {open && rect && (
        <div ref={box} className="ws-menu ws-user-menu" role="menu" aria-label="账号" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: 232 }}>
          <div className="ws-menu-note ws-user-note">
            <b>{me.name || me.email}</b>
            {me.name && me.email && <span>{me.email}</span>}
            {me.tier === "admin" && <span>管理员</span>}
          </div>
          <button role="menuitem" aria-current={memory ? "page" : undefined} onClick={() => { setOpen(false); onMemory?.(); }}>
            <IconBook size={14} />记忆
          </button>
          <button role="menuitem" disabled={leaving} onClick={() => { setLeaving(true); signOut().catch(() => setLeaving(false)); }}>
            {leaving ? <Spinner size={14} /> : <IconClose size={14} />}退出登录
          </button>
          <div className="ws-menu-note">退出后在 Fusion 等其他应用也会退出。</div>
        </div>
      )}
    </>
  );
}

// 左栏最下面常驻的额度按钮（2026-11-06 Sean 定，照 Claude 用量弹层）：小圆环画剩余比例、旁边是剩余数字（游客前面标“试用”，管理员“不限”）；
// 显示最紧的那个窗口，用到八成变黄、用完变红；没取到不显示。点开是额度面板（UsagePanel）：来源、每个窗口剩百分之几、余额、什么时候重置
export function CreditsFoot({ credits, guest, onLogin, onOpen }) {
  const [open, setOpen] = useState(false);
  const btn = useRef(null);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return;
    const down = (e) => !box.current?.contains(e.target) && !btn.current?.contains(e.target) && setOpen(false);
    const key = (e) => e.key === "Escape" && (setOpen(false), btn.current?.focus());
    addEventListener("mousedown", down);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("mousedown", down);
      removeEventListener("keydown", key);
    };
  }, [open]);
  if (!credits) return null;
  const left = credits.unlimited ? null : creditsLeft(credits);
  const used = usedBy(credits); // 管理员不限：按钮上显示今天用了多少
  if (!credits.unlimited && !left) return null;
  const frac = left ? Math.max(0, Math.min(1, left.remaining / left.limit)) : 1;
  const cls = "ws-credits-btn" + (left?.exhausted ? " out" : left?.warn ? " warn" : "");
  const title = credits.unlimited ? `额度不限；今天用了 ${used.day} credits，这周 ${used.week}`
    : `${left.span}还剩 ${Math.floor(left.remaining)} / ${left.limit} credits，${left.resetText}`;
  const rect = open ? btn.current?.getBoundingClientRect() : null;
  return (
    <>
      <button ref={btn} className={cls} title={title} aria-label={"额度：" + title} aria-haspopup="dialog" aria-expanded={open}
        onClick={() => { if (!open) onOpen?.(); setOpen(!open); }}>
        <Ring frac={frac} />
        <span>{credits.unlimited ? <><small>今天</small>{used.day}</> : <>{guest && <small>试用</small>}{Math.floor(left.remaining)}</>}</span>
      </button>
      {open && rect && (
        <div ref={box} className="ws-menu ws-usage" role="dialog" aria-label="额度" style={{ left: Math.max(8, rect.left), bottom: innerHeight - rect.top + 6, width: 232 }}>
          <UsagePanel credits={credits} guest={guest} onLogin={onLogin && (() => { setOpen(false); onLogin(); })} />
        </div>
      )}
    </>
  );
}

// 今天、这周用了多少（取整）；没有那个窗口算 0
function usedBy(credits) {
  const of = (kind) => Math.floor(credits.windows.find((w) => w.kind === kind)?.used || 0);
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

// 额度面板（照 Claude 用量弹层的样子）：来源一行；每个窗口一行，左标签右“剩余 xx%”；余额一行；底下是重置时间，游客多一个去登录的按钮
function UsagePanel({ credits, guest, onLogin }) {
  const source = credits.unlimited ? "管理员" : guest ? "游客试用" : "登录账号";
  if (credits.unlimited) {
    const used = usedBy(credits);
    return (
      <>
        <div className="ws-menu-note">额度来源：{source}（不限）</div>
        <div className="ws-usage-rows">
          <div className="ws-usage-row"><span>今天已用</span><b>{used.day} credits</b></div>
          <div className="ws-usage-row"><span>这周已用</span><b>{used.week} credits</b></div>
        </div>
        <div className="ws-menu-note">每天 0 点、每周一 0 点重新计（北京时间）</div>
      </>
    );
  }
  const ws = credits.windows.filter((w) => w.kind === "day" || w.kind === "week");
  const left = creditsLeft(credits);
  const pct = (w) => Math.max(0, Math.floor((1 - w.used / w.limit) * 100));
  return (
    <>
      <div className="ws-menu-note">额度来源：{source}</div>
      <div className="ws-usage-rows">
        {ws.map((w) => (
          <div key={w.kind} className={"ws-usage-row" + (w.used >= w.limit ? " out" : w.used >= w.limit * (credits.warn_at ?? 0.8) ? " warn" : "")}>
            <span>{w.kind === "week" ? "每周限额" : "每日限额"}</span><b>剩余 {pct(w)}%</b>
          </div>
        ))}
        <div className="ws-usage-row"><span>余额</span><b>{Math.floor(left.remaining)} credits</b></div>
      </div>
      <div className="ws-menu-note">{ws.some((w) => w.kind === "week") ? "每天 0 点、每周一 0 点重置" : "每天 0 点重置"}（北京时间）</div>
      {guest && onLogin && <button onClick={onLogin}><IconLock size={14} />登录后每天 10000 credits</button>}
    </>
  );
}

function Avatar({ me }) {
  const [broken, setBroken] = useState(false);
  if (me.avatar && !broken) return <img className="ws-avatar" src={me.avatar} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />;
  return <span className="ws-avatar">{(me.name || me.email || "?").trim()[0].toUpperCase()}</span>;
}

// 登录弹窗。验证码换登录的那几秒不能关（关了这次验证码就作废，用户还以为在登录）
// crowded：是因为领不到游客身份弹出来的，标题和说明换成“人多、请先登录”
export function LoginDialog({ open, onClose, crowded }) {
  const [view, setView] = useState("methods");
  const [going, setGoing] = useState(null); // 正在跳去的 provider
  const [email, setEmail] = useState(null); // 邮箱验证码能不能用：null 还没问
  const [critical, setCritical] = useState(false);
  const [error, setError] = useState(null);
  const criticalRef = useRef(false);
  criticalRef.current = critical;
  useEffect(() => {
    if (!open) return;
    setView("methods");
    setGoing(null);
    setError(null);
    emailLoginAvailable().then(setEmail);
    const key = (e) => e.key === "Escape" && !criticalRef.current && onClose();
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  if (!open) return null;
  const go = (provider) => {
    setGoing(provider);
    setError(null);
    signIn(provider).catch((e) => { setGoing(null); setError(e?.message || "没跳过去，再试一次"); });
  };
  return (
    <div className="ws-overlay" onMouseDown={(e) => e.target === e.currentTarget && !critical && onClose()}>
      <div className="ws-dialog" role="dialog" aria-modal="true" aria-labelledby="ws-login-title">
        {!critical && <button className="ws-dialog-close" aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>}
        {view === "email" ? (
          <EmailPanel onBack={() => setView("methods")} onDone={onClose} setCritical={setCritical} />
        ) : (
          <>
            <h2 id="ws-login-title" className="ws-dialog-title">{crowded ? "当前访问人数过多" : "登录灵稿"}</h2>
            <p className="ws-dialog-desc">{crowded ? "请先登录，登录后可以马上继续聊天；对话和记忆也会按账号保存。" : "登录后能用全部模型，对话和记忆按账号保存。现在这些对话会一起并进账号。"}</p>
            <div className="ws-login-methods">
              <button className="ws-login-btn" disabled={!!going} onClick={() => go("github")}>{going === "github" ? <Spinner size={15} /> : <GitHubMark />}用 GitHub 登录</button>
              <button className="ws-login-btn" disabled={!!going} onClick={() => go("google")}>{going === "google" ? <Spinner size={15} /> : <GoogleMark />}用 Google 登录</button>
              {email && <button className="ws-login-btn" disabled={!!going} onClick={() => setView("email")}><IconLock size={16} />用邮箱验证码登录</button>}
            </div>
            {error && <div className="ws-login-err" role="alert">{error}</div>}
          </>
        )}
      </div>
    </div>
  );
}

// 邮箱验证码：填邮箱 → 发码 → 填码。重发有间隔；验证码对了换登录的那几秒告诉弹窗别关
function EmailPanel({ onBack, onDone, setCritical }) {
  const [phase, setPhase] = useState("email");
  const [addr, setAddr] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(null); // {masked, codeLength, resendAt}
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now);
  const flow = useRef(null);
  const ctl = useRef(null);
  const input = useRef(null);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(t);
      ctl.current?.abort();
      flow.current?.cancel();
    };
  }, []);
  useEffect(() => { input.current?.focus(); }, [phase]);
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
      setError(e?.message || "出错了，再试一次");
      if (["interaction_expired", "interaction_consumed", "invalid_interaction", "flow_expired", "too_many_attempts"].includes(e?.code)) {
        setPhase("email");
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
    if (!valid) return setError("邮箱地址不对");
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
      onDone();
      location.reload(); // 身份变了，整页按新身份重取
    }, true);
  };
  const wait = sent ? Math.max(0, Math.ceil((sent.resendAt - now) / 1000)) : 0;
  if (phase === "email") {
    return (
      <form onSubmit={start}>
        <h2 id="ws-login-title" className="ws-dialog-title">用邮箱登录</h2>
        <p className="ws-dialog-desc">填邮箱，我们发一个验证码过去。没有账号会直接注册。</p>
        <input ref={input} className="ws-field" type="email" autoComplete="email" placeholder="邮箱" value={addr} disabled={busy} onChange={(e) => setAddr(e.target.value)} />
        {error && <div className="ws-login-err" role="alert">{error}</div>}
        <div className="ws-dialog-actions">
          <button type="button" className="ws-btn ghost" disabled={busy} onClick={onBack}>换个方式</button>
          <button type="submit" className="ws-btn primary" disabled={busy || !addr.trim()}>{busy && <Spinner size={14} />}发送验证码</button>
        </div>
      </form>
    );
  }
  return (
    <form onSubmit={verify}>
      <h2 id="ws-login-title" className="ws-dialog-title">输入验证码</h2>
      <p className="ws-dialog-desc">验证码已发到 {sent.masked}{sent.expiresIn > 0 && `，${Math.max(1, Math.round(sent.expiresIn / 60))} 分钟内有效`}。</p>
      <input ref={input} className="ws-field code" inputMode="numeric" autoComplete="one-time-code" placeholder={"·".repeat(sent.codeLength)} maxLength={sent.codeLength}
        value={code} disabled={busy} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
      {error && <div className="ws-login-err" role="alert">{error}</div>}
      <div className="ws-dialog-actions">
        <button type="button" className="ws-btn ghost" disabled={busy} onClick={() => { setPhase("email"); setCode(""); setError(null); }}>换个邮箱</button>
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
    completeCallback().then((path) => location.replace(path)).catch((e) => setError(e?.message || "登录没成功"));
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
