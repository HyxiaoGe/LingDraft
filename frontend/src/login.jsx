// 登录：左栏下面的登录入口和用户菜单、登录弹窗（GitHub、Google、邮箱验证码）、/auth/callback 页。身份本身见 auth.js。
import { useEffect, useRef, useState } from "react";
import { EmailLogin, checkSession, completeCallback, emailLoginAvailable, init, signIn, signOut, snapshot, subscribeAuth } from "./auth.js";
import { IconClose, IconLock, Spinner } from "./icons.jsx";
import { creditsText } from "./research.js";

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

// 左栏下面：游客看到“登录”，登录了看到头像和邮箱，点开能退出
// credits 是额度（/api/credits），菜单里显示今天、这周还剩多少；onOpen 在打开菜单时调（重新取一次）
export function UserFoot({ auth, onLogin, credits, onOpen }) {
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
      <button ref={btn} className="ws-user-btn" aria-haspopup="menu" aria-expanded={open} title={me.email} onClick={() => { if (!open) onOpen?.(); setOpen(!open); }}>
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
          <CreditsLine credits={credits} />
          <button role="menuitem" disabled={leaving} onClick={() => { setLeaving(true); signOut().catch(() => setLeaving(false)); }}>
            {leaving ? <Spinner size={14} /> : <IconClose size={14} />}退出登录
          </button>
          <div className="ws-menu-note">退出后在 Fusion 等其他应用也会退出。</div>
        </div>
      )}
    </>
  );
}

// 今天、这周还剩多少 credits（1 credit = ¥0.001 的 API 成本）；管理员不限，没取到不显示
function CreditsLine({ credits }) {
  if (!credits) return null;
  if (credits.unlimited) return <div className="ws-menu-note ws-credits-line">额度不限</div>;
  const ws = credits.windows.filter((w) => w.kind === "day" || w.kind === "week");
  if (!ws.length) return null;
  return (
    <div className="ws-menu-note ws-credits-line">
      {ws.map((w) => <span key={w.kind}>{w.kind === "week" ? "这周" : "今天"}还剩 <b>{creditsText(Math.max(0, w.limit - w.used))}</b> / {w.limit}</span>)}
    </div>
  );
}

function Avatar({ me }) {
  const [broken, setBroken] = useState(false);
  if (me.avatar && !broken) return <img className="ws-avatar" src={me.avatar} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />;
  return <span className="ws-avatar">{(me.name || me.email || "?").trim()[0].toUpperCase()}</span>;
}

// 登录弹窗。验证码换登录的那几秒不能关（关了这次验证码就作废，用户还以为在登录）
export function LoginDialog({ open, onClose }) {
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
            <h2 id="ws-login-title" className="ws-dialog-title">登录灵稿</h2>
            <p className="ws-dialog-desc">登录后能用全部模型，对话和记忆按账号保存。现在这些对话会一起并进账号。</p>
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
        <input ref={input} className="ws-input" type="email" autoComplete="email" placeholder="邮箱" value={addr} disabled={busy} onChange={(e) => setAddr(e.target.value)} />
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
      <p className="ws-dialog-desc">验证码已发到 {sent.masked}，几分钟内有效。</p>
      <input ref={input} className="ws-input code" inputMode="numeric" autoComplete="one-time-code" placeholder={"·".repeat(sent.codeLength)} maxLength={sent.codeLength}
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
