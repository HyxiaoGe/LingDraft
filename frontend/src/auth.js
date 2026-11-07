// 身份：这个浏览器是谁（见后端 app/identity.py）。每个对话接口都带 Authorization: Bearer。
// 没登录是游客：后端 /api/auth/guest 发一个游客令牌，存在 localStorage，对话归它；登录以后把游客期间的对话并进账号（/api/auth/claim），游客令牌就丢掉。
// 登录走 Sean 的 auth 服务（auth-service），用它的浏览器 SDK（auth-client-web）：PKCE 跳转登录、/auth/callback 换 token、到期自动刷新、
// 跨应用一处登出处处登出。SDK 的配置（auth 地址、client_id）从后端 /api/auth/config 取，后端没配就没有登录（login 为 false）。
// 身份一变（登录成功、别处退出、换了账号）整页重载：页面上所有东西都是按人取的，重载最稳。
import {
  cancelAuthorization, clearLocalSessionIfCurrent, completeAuthorization, configure, createTokenStore, getAccessToken, handleCallback, login as sdkLogin,
  logout as sdkLogout, prepareAuthorization, reconcileSession, refresh, resumeSession, tokenStore,
} from "auth-client-web";
import { clearDrafts } from "./drafts.js";

export const CALLBACK_PATH = "/auth/callback";
const GUEST_KEY = "lingdraft.guest";
const STORAGE_KEYS = { accessToken: "lingdraft.access", refreshToken: "lingdraft.refresh", expiresAt: "lingdraft.expires", user: "lingdraft.user" };

let cfg = null; // {login, auth_url, client_id}
let me = null; // 登录用户 {sub, tier, email, name, avatar}；游客是 null
let done = false; // init() 做完了
let configLoading = null; // loadConfig() 的 promise
let ready = null; // init() 的 promise
let newGuestInFlight = null;
const listeners = new Set();

// ---- 页面用 ----

// 当前身份的快照：ready 是初始化做完了（这之前不显示登录入口）；login 是有没有登录功能；me 见上
// crowded 是领游客身份被拒了（同一网络一天领得太多）：页面弹登录框说清为什么、登录后会怎样。
// 是后端 429 的 detail（{per_ip, per_ask, login}，见后端 api.main 的 auth_guest），老后端只给一句话时是 {}；没被拒是 false
// 本机记着的是谁，不等网络（欢迎语用：一打开就是带名字的那句，不先空着等认人）。有本地会话是登录名字（没填是空字符串），
// 没有是 null（游客，或者没有登录功能的部署）。和 init 认出来的不一样时（比如会话在别处失效了），页面跟着 snapshot 改
export function localWho() {
  try {
    const store = createTokenStore(STORAGE_KEYS);
    return store.getSessionSnapshot() ? store.getUser()?.name || "" : null;
  } catch {
    return null;
  }
}

export const snapshot = () => ({ ready: done, login: !!cfg?.login, me, crowded: !me && crowded });
let crowded = false;
export function subscribeAuth(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const notify = () => listeners.forEach((fn) => fn(snapshot()));

const storage = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 隐私模式等：这次会话里每次都重新领 */ } },
  del: (k) => { try { localStorage.removeItem(k); } catch { /* 同上 */ } },
};

// 初始化：取配置、配 SDK、认出现在是谁。页面一起来就调；接口请求只等配置（loadConfig），不等认人（认人本身要发请求）
export function init() {
  if (!ready) ready = doInit();
  return ready;
}

function loadConfig() {
  if (!configLoading) {
    configLoading = (async () => {
      try {
        const resp = await fetch("/api/auth/config");
        cfg = resp.ok ? await resp.json() : { login: false };
      } catch {
        cfg = { login: false };
      }
      if (cfg.login) {
        configure({ authUrl: cfg.auth_url, clientId: cfg.client_id, redirectUri: location.origin + CALLBACK_PATH, storageKeys: STORAGE_KEYS });
        // 别的标签页登录、退出了（SDK 的会话存在 localStorage，各标签页共用；storage 事件只在别的标签页里响）：这里跟着重载
        addEventListener("storage", (e) => {
          if (e.key !== STORAGE_KEYS.accessToken || location.pathname === CALLBACK_PATH) return;
          if ((!!e.newValue) !== !!me) location.reload();
        });
      }
    })();
  }
  return configLoading;
}

async function doInit() {
  await loadConfig();
  if (cfg.login) {
    if (tokenStore().getSessionSnapshot()) {
      await loadMe();
    } else if (location.pathname !== CALLBACK_PATH) {
      // 没有本地会话：看看 auth 服务那边是不是已经登录了（比如刚在 Fusion 登过），是就不用再登一次。要和 auth 同站的 Cookie，本机开发会是 no_session
      await resumeIfSignedInCentrally();
    }
  }
  done = true;
  notify();
  if (cfg.login && !me) emailLoginAvailable(); // 邮箱验证码能不能用先问好，打开登录框时邮箱那栏直接在（第 29 条）
}

async function loadMe() {
  try {
    const resp = await authFetch("/api/auth/me");
    if (!resp.ok) return;
    const got = await resp.json();
    if (got.guest) return;
    const u = tokenStore().getUser() || {};
    me = { sub: got.sub, tier: got.tier, email: got.email || u.email || "", name: u.name || "", avatar: u.avatarUrl || "" };
  } catch { /* 网络不通：先按游客显示，请求时再说 */ }
}

async function resumeIfSignedInCentrally() {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 4000);
    const r = await resumeSession({ signal: ctl.signal }).finally(() => clearTimeout(t));
    if (r.status !== "resumed") return;
    await adopt();
    // 页面等不及：对话列表、模型已经按游客取过了（2026-11-06 dev 上看到列表空、模型锁着）。本地会话存好了就整页重载，按登录用户重取；
    // 存不下（隐私模式等）就不重载，不然每次都 resume、每次都重载
    if (tokenStore().getSessionSnapshot()) location.reload();
  } catch { /* 探测不到就算了 */ }
}

// 登录成功之后：把游客期间的对话并进账号，游客令牌丢掉，认一下自己是谁
async function adopt() {
  const guest = storage.get(GUEST_KEY);
  if (guest) {
    try {
      const resp = await authFetch("/api/auth/claim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ guest_token: guest }) });
      if (resp.ok || resp.status === 400) storage.del(GUEST_KEY); // 400：游客令牌本来就不对，也不用留
    } catch { /* 没并成：游客令牌留着，下次登录再并 */ }
  }
  await loadMe();
  notify();
}

function becameGuest() {
  me = null;
  clearDrafts(); // 没发出去的字是这个账号的，别留给下一个用这台电脑的人
  notify();
  location.reload(); // 页面上是登录用户的对话，刷掉
}

// ---- 令牌 ----

async function guestToken() {
  const t = storage.get(GUEST_KEY);
  if (t) return t;
  return newGuest();
}

function newGuest() {
  if (!newGuestInFlight) {
    newGuestInFlight = (async () => {
      const resp = await fetch("/api/auth/guest", { method: "POST" });
      if (!resp.ok) {
        const d = await detail(resp);
        if (resp.status === 429 && !crowded) { crowded = d && typeof d === "object" ? d : {}; notify(); }
        throw Object.assign(new Error("没领到游客身份"), { status: resp.status }); // 页面按状态码说，不显示这句
      }
      const { token } = await resp.json();
      storage.set(GUEST_KEY, token);
      if (crowded) { crowded = false; notify(); }
      return token;
    })().finally(() => { newGuestInFlight = null; });
  }
  return newGuestInFlight;
}

async function detail(resp) {
  try { return JSON.parse(await resp.text()).detail ?? null; } catch { return null; }
}

// 现在该带的令牌：登录了带 access token（到期 SDK 自己刷新；刷新被定论拒绝了会清掉本地会话，就落回游客）；没登录带游客令牌
async function token() {
  await loadConfig();
  if (cfg.login && tokenStore().getSessionSnapshot()) {
    try {
      const t = await getAccessToken();
      if (t) return { value: t, login: true };
    } catch {
      const t = tokenStore().getAccessToken(); // 网络不通刷不了：先拿旧的试，后端 401 再说
      if (t) return { value: t, login: true };
    }
    if (me) becameGuest();
  }
  return { value: await guestToken(), login: false };
}

const withBearer = (init, t) => ({ ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${t}` } });

// 带身份的 fetch：401 只再试一次。登录的令牌被拒（别处退出了、过期）：强刷一次，刷不出来就清掉本地会话落回游客；游客令牌被拒：重新领一个
export async function authFetch(url, init = {}) {
  const first = await token();
  const resp = await fetch(url, withBearer(init, first.value));
  if (resp.status !== 401) return resp;
  let next;
  if (first.login) {
    let t = null;
    try { t = await refresh(); } catch { return resp; }
    if (!t) {
      await clearLocalSessionIfCurrent(first.value).catch(() => {});
      if (me) becameGuest();
      return resp;
    }
    next = t;
  } else {
    storage.del(GUEST_KEY);
    next = await newGuest();
  }
  return fetch(url, withBearer(init, next));
}

// ---- 登录、退出 ----

// 跳去 auth 服务登录（provider：github / google），回来落在现在这一页
export function signIn(provider) {
  return sdkLogin(provider, { redirectPath: location.pathname + location.search });
}

// /auth/callback 页：换 token，并入游客的对话，返回该回到哪一页。退出登录回跳（没有 code）也落在这里，返回首页
export async function completeCallback() {
  await loadConfig();
  if (!cfg.login) return "/";
  const r = await handleCallback();
  if (r.status === "authenticated") {
    await adopt();
    return r.redirectPath || "/";
  }
  if (r.status === "unauthenticated") throw Object.assign(new Error(r.error), { code: r.error === "access_denied" ? "denied" : "failed" }); // 页面上的说法见 login.jsx
  return "/";
}

// 退出：一处退出处处退出（auth 服务的会话一起结束，Fusion 等也退了），之后回到首页当游客
export async function signOut() {
  storage.del(GUEST_KEY); // 游客期间的对话已经并进账号了，退出后是个新游客
  clearDrafts(); // 没发出去的字一起清掉，公用电脑上不留给下一个人
  await sdkLogout({ global: true, postLogoutRedirectUri: location.origin + CALLBACK_PATH });
}

// 回到这个标签页时和 auth 服务对一下账：别处退出了就跟着退，换了账号就按新账号来。登录了才对；两次之间隔够 30 秒
let lastCheck = 0;
export async function checkSession() {
  await init();
  if (!cfg.login || !me || Date.now() - lastCheck < 30000) return;
  lastCheck = Date.now();
  try {
    const r = await reconcileSession();
    if (r.status === "no_session") {
      await clearLocalSessionIfCurrent(tokenStore().getAccessToken()).catch(() => {});
      becameGuest();
    } else if (r.status === "switched") {
      clearDrafts(); // 换了账号：上一个账号没发出去的字不留
      location.reload();
    }
  } catch { /* 网络不通：保持现状 */ }
}

// ---- 邮箱验证码登录（auth 服务的 headless 流程：和跳转登录一样是 PKCE，只是不离开页面） ----
// 要求页面和 auth 服务是同一个站（都在 seanfield.org 下、https），本机开发没有这一项

// 页面起来时就问（见 doInit），问到的记住：打开登录框时已经知道，邮箱那栏不会晚一步才冒出来。没问到（网络不通）不记，下次打开再问
let emailOk = null;
let emailAsking = null;
export const emailLoginKnown = () => emailOk;
export function emailLoginAvailable() {
  if (emailOk !== null) return Promise.resolve(emailOk);
  if (!emailAsking) emailAsking = askEmail().finally(() => { emailAsking = null; });
  return emailAsking;
}

async function askEmail() {
  await loadConfig();
  if (!cfg.login || location.protocol !== "https:") return (emailOk = false);
  try {
    const q = new URLSearchParams({ client_id: cfg.client_id, redirect_uri: location.origin + CALLBACK_PATH });
    const resp = await fetch(`${cfg.auth_url}/auth/capabilities?${q}`, { cache: "no-store" });
    if (!resp.ok) return false;
    return (emailOk = (await resp.json()).email_headless_login === true);
  } catch {
    return false;
  }
}

const EMAIL_ERRORS = {
  invalid_email: "邮箱地址不对", invalid_code: "验证码不对", expired_code: "验证码过期了，重新发一个", code_expired: "验证码过期了，重新发一个",
  too_many_attempts: "试错太多次了，重新发一个验证码", rate_limited: "发得太频繁了，稍等再试", invalid_interaction: "这次登录过期了，重新开始",
  interaction_expired: "这次登录过期了，重新开始", flow_expired: "这次登录过期了，重新开始", delivery_unavailable: "邮件现在发不出去，稍后再试", delivery_failed: "邮件现在发不出去，稍后再试",
};

async function authJson(path, init, csrf) {
  let resp;
  try {
    resp = await fetch(`${cfg.auth_url}${path}`, { ...init, credentials: "include", headers: { "content-type": "application/json", ...(csrf ? { "X-CSRF-Token": csrf } : {}) } });
  } catch (e) {
    if (e?.name === "AbortError") throw e;
    throw new Error("连不上登录服务");
  }
  let body = null;
  try { body = await resp.json(); } catch { /* 下面按状态码 */ }
  if (!resp.ok) {
    const code = body?.error;
    const err = new Error(EMAIL_ERRORS[code] || (resp.status === 429 ? EMAIL_ERRORS.rate_limited : "登录服务出错了，稍后再试"));
    err.code = code || (resp.status === 429 ? "rate_limited" : "server_error");
    err.retryAfter = Number(resp.headers.get("Retry-After") || body?.retry_after) || undefined;
    throw err;
  }
  return body;
}

// 一次邮箱登录：start 发验证码（返回 {masked, resendAfter, expiresIn, codeLength}），resend 再发，verify 用验证码换 token（成功后并入游客的对话），cancel 作废
export class EmailLogin {
  async start(email, signal) {
    await loadConfig();
    let auth;
    try {
      auth = await prepareAuthorization({ redirectPath: location.pathname + location.search });
    } catch { // SDK 的报错是英文，不直出
      throw Object.assign(new Error("登录服务出错了，稍后再试"), { code: "server_error" });
    }
    this.auth = auth;
    try {
      const s = await authJson("/auth/email/headless/start", {
        method: "POST", signal,
        body: JSON.stringify({ client_id: auth.clientId, redirect_uri: auth.redirectUri, response_type: auth.responseType, state: auth.state,
          code_challenge: auth.codeChallenge, code_challenge_method: auth.codeChallengeMethod }),
      });
      this.flow = s.flow_id;
      this.csrf = s.csrf_token;
      this.codeLength = s.code_length || 6;
      this.email = email;
      return this.send(signal);
    } catch (e) {
      this.cancel();
      throw e;
    }
  }

  async send(signal) {
    const r = await authJson("/auth/email/headless/send", { method: "POST", signal, body: JSON.stringify({ flow_id: this.flow, email: this.email }) }, this.csrf);
    return { masked: r.masked_destination, resendAfter: r.resend_after || 0, expiresIn: r.expires_in || 0, codeLength: this.codeLength };
  }

  resend(signal) {
    return this.send(signal).catch((e) => {
      if (["too_many_attempts", "interaction_expired", "invalid_interaction", "flow_expired"].includes(e.code)) this.cancel();
      throw e;
    });
  }

  async verify(code, signal) {
    let r;
    try {
      r = await authJson("/auth/email/headless/verify", { method: "POST", signal, body: JSON.stringify({ flow_id: this.flow, code }) }, this.csrf);
    } catch (e) {
      if (["too_many_attempts", "interaction_expired", "invalid_interaction", "flow_expired"].includes(e.code)) this.cancel();
      throw e;
    }
    if (r.state !== this.auth.state) {
      this.cancel();
      throw new Error("登录服务返回的不对，重新开始");
    }
    try {
      await completeAuthorization({ authorizationCode: r.code, state: r.state, signal });
    } catch {
      // 验证码已经用掉、授权码一次性的：不能停在验证码页重试
      this.auth = null;
      throw Object.assign(new Error("验证码对了，但换登录没成功，重新开始"), { code: "interaction_consumed" });
    }
    this.auth = null;
    await adopt();
  }

  cancel() {
    if (this.auth) cancelAuthorization(this.auth.state);
    this.auth = null;
  }
}
