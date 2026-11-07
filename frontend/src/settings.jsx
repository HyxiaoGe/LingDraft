// 设置页（2026-11-07 Sean 定）：头像菜单里的“设置”打开，和记忆页同级（/settings）。只放这几项，不加别的：
// - 主题：跟随系统 / 浅色 / 深色，存本机（prefs.js），默认跟随系统；
// - 答完通知：现在浏览器允不允许、开没开，能开能关；浏览器拒绝了说清去哪改（评审第 46 条的尾巴）。和等待行旁边的“答完通知我”是同一个开关（alert.jsx）；
// - 深度思考：新对话默认开还是关，存本机，默认开；
// - 记忆：一行摘要，点进去是记忆页。游客写现在那句登录说明；没有登录功能的部署谁都没有记忆，不放。
// 游客也能进来，主题、深度思考对游客一样生效
import { useEffect, useId, useState } from "react";
import { permission, setWants, wants } from "./alert.jsx";
import { getMemory } from "./api.js";
import { IconChevron } from "./icons.jsx";
import { HeadBar } from "./nav.jsx";
import { THEMES } from "./prefs.js";
import { sayWhen, stamp } from "./research.js";

// theme、onTheme：主题（见 prefs.useTheme）；think、onThink：新对话默认开不开深度思考；onMemory：打开记忆页
export function SettingsPage({ auth, onLogin, theme, onTheme, think, onThink, onMemory }) {
  return (
    <section className="ws-main" aria-label="设置">
      <HeadBar title="设置" />
      <div className="ws-scroll">
        <div className="ws-flow set">
          <div className="set-list">
            <Row name="主题" desc="存在这台设备上。" stack>
              <div className="ws-seg" role="radiogroup" aria-label="主题">
                {THEMES.map(([v, label]) => (
                  <button key={v} className={"ws-seg-opt" + (theme === v ? " on" : "")} role="radio" aria-checked={theme === v} onClick={() => onTheme(v)}>{label}</button>
                ))}
              </div>
            </Row>
            <NotifyRow />
            <Row name="深度思考" desc={`新对话默认${think ? "开" : "关"}，对话里随时能切；只对能开关思考的模型有效。存在这台设备上。`}>
              {(ids) => <Switch on={think} onChange={onThink} ids={ids} />}
            </Row>
          </div>
          {(auth.login || auth.me) && <MemoryCard auth={auth} onLogin={onLogin} onOpen={onMemory} />}
        </div>
      </div>
    </section>
  );
}

// 一行设置：左边名字和说明，右边开关、选项。stack：手机上选项放到说明下面（三个选项一行放不下）。
// children 是函数时把名字、说明的 id 给它，开关用来念出“深度思考，开关，开”和说明
function Row({ name, desc, stack, children }) {
  const id = useId();
  const ids = { name: `${id}n`, desc: `${id}d` };
  return (
    <div className={"set-row" + (stack ? " stack" : "")}>
      <div className="set-text">
        <div className="set-name" id={ids.name}>{name}</div>
        <div className="set-desc" id={ids.desc}>{desc}</div>
      </div>
      <div className="set-ctl">{typeof children === "function" ? children(ids) : children}</div>
    </div>
  );
}

function Switch({ on, onChange, ids, disabled }) {
  return (
    <button className="set-switch" role="switch" aria-checked={on} aria-labelledby={ids.name} aria-describedby={ids.desc} disabled={disabled} onClick={() => onChange(!on)}>
      <span className={"ws-switch" + (on ? " on" : "")} aria-hidden="true" />
    </button>
  );
}

// 答完通知：浏览器的允许状态（default、granted、denied、unsupported）和本机记的开关合起来说。
// 回到这个页面时再问一次浏览器：用户可能刚去浏览器设置里改过
function NotifyRow() {
  const [perm, setPerm] = useState(permission);
  const [on, setOn] = useState(wants);
  useEffect(() => {
    const again = () => setPerm(permission());
    let status = null;
    navigator.permissions?.query({ name: "notifications" }).then((s) => { status = s; s.onchange = again; }).catch(() => {});
    document.addEventListener("visibilitychange", again);
    addEventListener("focus", again);
    return () => {
      if (status) status.onchange = null;
      document.removeEventListener("visibilitychange", again);
      removeEventListener("focus", again);
    };
  }, []);
  const active = on && perm === "granted";
  const change = async (want) => {
    if (!want) {
      setWants(false);
      setOn(false);
      return;
    }
    const p = perm === "granted" ? perm : await Notification.requestPermission();
    setPerm(p);
    if (p === "granted") {
      setWants(true);
      setOn(true);
    }
  };
  const desc = perm === "unsupported" ? "这个浏览器发不了系统通知。答完时标签页的标题照样会提示。"
    : perm === "denied" ? "浏览器没允许灵稿发通知。要开的话：Chrome、Edge 点地址栏左边的图标，把“通知”改成允许；Safari 在“设置 → 网站 → 通知”里改。改好回到这里打开。"
      : active ? "已开启：你在别的标签页或应用里时，答完会发一条系统通知。"
        : "没开。开了以后，你在别的标签页或应用里时，答完会发一条系统通知；不开的话，标签页的标题照样会提示。";
  return (
    <Row name="答完通知" desc={desc}>
      {(ids) => <Switch on={active} onChange={change} ids={ids} disabled={perm === "unsupported" || perm === "denied"} />}
    </Row>
  );
}

// 记忆：登录了写“已记住 N 条 · 上次整理：今天 03:00”，点进去是记忆页；游客写现在记忆页上那句登录说明
function MemoryCard({ auth, onLogin, onOpen }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);
  const guest = !auth.me;
  useEffect(() => {
    if (guest) return;
    getMemory().then(setData).catch(() => setFailed(true));
  }, [guest]);
  if (guest) {
    return (
      <div className="set-card">
        <div className="set-text">
          <div className="set-name">记忆</div>
          <div className="set-desc">没登录的对话不整理，登录后才有记忆。</div>
        </div>
        {onLogin && <button className="ws-btn sm" onClick={onLogin}>登录</button>}
      </div>
    );
  }
  const last = data?.last_dream ? stamp(data.last_dream.started_at) : null;
  const line = failed ? "没取到记忆的情况，点进去再试"
    : !data ? "正在取…"
      : `已记住 ${data.memories.length.toLocaleString("en-US")} 条 · ${last ? `上次整理：${sayWhen(last)}` : "还没整理过"}`;
  return (
    <button className="set-card link" onClick={onOpen}>
      <span className="set-text">
        <span className="set-name">记忆</span>
        <span className="set-desc">{line}</span>
      </span>
      <IconChevron size={16} className="chev" />
    </button>
  );
}
