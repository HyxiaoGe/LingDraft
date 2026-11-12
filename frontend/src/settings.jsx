// 设置（2026-11-07 Sean 定）：头像菜单里的“设置”弹出窗口，照豆包的设置窗口做成大的两栏：左边分类，右边一组组的行，说明写在组下面。
// 点窗口外、按 Esc、点右上角的关闭都能关；后面开着的对话不动（不换地址、不断开）。只放这几项，一类一项，不加别的：
// - 通用：新对话默认开不开深度思考，存本机，默认开；只管对话，工作任务一直开着（2026-11-12 Sean 定，说明里写明）；
// - 外观：主题，跟随系统 / 浅色 / 深色，存本机（prefs.js），默认跟随系统；
// - 通知：答完通知，现在浏览器允不允许、开没开，能开能关；浏览器拒绝了说清去哪改（评审第 46 条的尾巴）。开关只在这里（等待行旁边的“答完通知我”2026-11-07 去掉了），记在 alert.jsx；
// - 记忆：现有的记忆列表（memory.jsx），游客那里写着登录了才有；没有登录功能的部署谁都没有记忆，不放这一类。
// 知识库 11-09 放在这里，11-10 搬到左栏（藏在设置里没人知道有），同一天改成自己一页、叫资料库（knowledge.jsx 的 LibraryPage）。
// 游客也能打开，主题、深度思考对游客一样生效。手机上从底部升起：先是分类列表，点进去一类，左上角“设置”回到列表
import { useEffect, useId, useRef, useState } from "react";
import { permission, setWants, wants } from "./alert.jsx";
import { IconBell, IconBook, IconCheck, IconChevron, IconClose, IconSliders, IconSun } from "./icons.jsx";
import { opener, trapTab, useLayer } from "./layers.js";
import { Avatar } from "./login.jsx";
import { MemoryPanel } from "./memory.jsx";
import { THEMES } from "./prefs.js";
import dialog from "./dialog.module.css";
import styles from "./settings.module.css";

const SECTIONS = [
  { key: "general", label: "通用", Icon: IconSliders },
  { key: "look", label: "外观", Icon: IconSun },
  { key: "notify", label: "通知", Icon: IconBell },
  { key: "memory", label: "记忆", Icon: IconBook },
];
const ACCOUNT = new Set(["memory"]); // 按账号分的：有登录功能的部署才放

// view：打开的是哪一类（上面的 key）；"settings" 是刚打开（桌面上是第一类，手机上是分类列表）。onView 换；onClose 关窗口。
// mobile：窄屏。theme、onTheme：主题（见 prefs.useTheme）；think、onThink：新对话默认开不开深度思考；
// onLogin：游客去登录（先关这个窗口再开登录窗口）；undo、threads、onOpen：给记忆列表（删了能撤销、对出出处、点出处关窗口打开那段对话）
export function SettingsDialog({ view, onView, onClose, mobile, auth, onLogin, theme, onTheme, think, onThink, undo, threads, onOpen }) {
  const sections = SECTIONS.filter((x) => !ACCOUNT.has(x.key) || auth.login || auth.me);
  const cur = sections.find((x) => x.key === view) || (mobile ? null : sections[0]); // 手机上 null 是分类列表
  const box = useRef(null);
  const body = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  const paneId = useId();
  const thinkId = useId();
  useEffect(() => {
    const back = opener(); // 关上以后焦点回去（Safari 点按钮不给按钮焦点：用刚按下的那个按钮）
    box.current?.focus({ preventScroll: true });
    return () => { if (back?.isConnected) back.focus({ preventScroll: true }); };
  }, []);
  useLayer(true, () => closing.current());
  // 换一类：右边滚回顶上。手机上整页换掉：进去焦点落在窗口上（读屏念新标题），回到列表落在刚才那一类上
  const first = useRef(true);
  const last = useRef(null);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    if (body.current) body.current.scrollTop = 0;
    if (!mobile) return;
    if (cur) {
      last.current = cur.key;
      box.current?.focus({ preventScroll: true });
    } else box.current?.querySelector(`[data-key="${last.current}"]`)?.focus({ preventScroll: true });
  }, [cur?.key]); // eslint-disable-line react-hooks/exhaustive-deps
  const close = <button className={`${dialog.close} ${dialog.inline} ${styles.close}`} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>;
  return (
    <div className={`${dialog.overlay} ${dialog.large}`} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} className={`${dialog.dialog} ${dialog.large} ${dialog.side} flex phone:flex-col`} role="dialog" aria-modal="true" aria-label="设置" tabIndex={-1} onKeyDown={(e) => trapTab(e, box.current)}>
        {(!mobile || !cur) && (
          <nav className={styles.nav} aria-label="设置分类">
            <div className={styles.head}><h2 className={styles.title}>设置</h2>{mobile && close}</div>
            <Me auth={auth} onLogin={onLogin} />
            <div className={styles.navList}>
              {sections.map(({ key, label, Icon }) => (
                <button key={key} data-key={key} className={styles.navItem} aria-current={!mobile && cur?.key === key ? "page" : undefined} onClick={() => onView(key)}>
                  <Icon size={16} />{label}{mobile && <IconChevron size={16} className={styles.chev} />}
                </button>
              ))}
            </div>
          </nav>
        )}
        {cur && (
          <section className={styles.pane} aria-labelledby={paneId}>
            <div className={styles.head}>
              {mobile && <button className={"ws-btn ghost sm " + styles.back} aria-label="返回设置" onClick={() => onView("settings")}><IconChevron size={14} className={styles.backIcon} />设置</button>}
              <h2 id={paneId} className={styles.title}>{cur.label}</h2>
              {close}
            </div>
            <div className={styles.body} ref={body}>
              {cur.key === "general" && (
                <Group label="新对话" footId={thinkId}
                  foot={`新对话默认${think ? "开" : "关"}深度思考。对话里随时能切，只管那一段对话；只对能开关思考的模型有效。这个开关只对对话有效：为了保证质量，工作任务里深度思考一直开着，不能关。存在这台设备上。`}>
                  <SwitchRow name="深度思考" on={think} onChange={onThink} desc={thinkId} />
                </Group>
              )}
              {cur.key === "look" && (
                <Group label="主题" role="radiogroup" foot="跟随系统时，系统换深浅色这里跟着换。存在这台设备上。">
                  {THEMES.map(([v, label]) => (
                    <button key={v} className={styles.row} role="radio" aria-checked={theme === v} onClick={() => onTheme(v)}>
                      <span className={styles.name}>{label}</span>{theme === v && <IconCheck size={16} className={styles.check} />}
                    </button>
                  ))}
                </Group>
              )}
              {cur.key === "notify" && <NotifyGroup />}
              {cur.key === "memory" && <MemoryPanel auth={auth} onLogin={onLogin} undo={undo} threads={threads} onOpen={onOpen} />}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}

// 左栏最上面是谁：登录了是头像、名字、邮箱；游客是“游客”和登录。没有登录功能的部署不放
function Me({ auth, onLogin }) {
  if (!auth.login) return null;
  const { me } = auth;
  return (
    <div className={styles.me}>
      <Avatar me={me} large />
      <span className={styles.meText}>
        <b>{me ? me.name || me.email : "游客"}</b>
        {me?.name && me.email && <small>{me.email}</small>}
      </span>
      {!me && onLogin && <button className="ws-btn sm" onClick={onLogin}>登录</button>}
    </div>
  );
}

// 一组：上面一行小字是组名，中间是圆角框里的几行，下面是说明（footId 给开关念说明用）
function Group({ label, foot, footId, role, children }) {
  return (
    <div className={styles.group}>
      {label && <div className={styles.groupLabel}>{label}</div>}
      <div className={styles.rows} role={role} aria-label={role ? label : undefined}>{children}</div>
      {foot && <p className={styles.foot} id={footId}>{foot}</p>}
    </div>
  );
}

// 整行都是开关：读屏念“深度思考，开关，开”和下面的说明
function SwitchRow({ name, on, onChange, desc, disabled }) {
  return (
    <button className={styles.row} role="switch" aria-checked={on} aria-describedby={desc} disabled={disabled} onClick={() => onChange(!on)}>
      <span className={styles.name}>{name}</span>
      <span className={"ws-switch" + (on ? " on" : "")} aria-hidden="true" />
    </button>
  );
}

// 答完通知：浏览器的允许状态（default、granted、denied、unsupported）和本机记的开关合起来说。
// 回到这个页面时再问一次浏览器：用户可能刚去浏览器设置里改过
function NotifyGroup() {
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
  const id = useId();
  return (
    <Group foot={desc} footId={id}>
      <SwitchRow name="答完通知" on={active} onChange={change} desc={id} disabled={perm === "unsupported" || perm === "denied"} />
    </Group>
  );
}
