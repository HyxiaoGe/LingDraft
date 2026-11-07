// 输入框那一块（2026-11-07 从 timeline.jsx 拆出来，样式从 workspace.css 迁到 composer.module.css + Tailwind）：
// 输入框本身（写字、加图、发送 / 停止、这一轮答完再发），右下角的深度思考和选模型，贴在上沿的“还没发出去的话”，
// 上面的额度提醒和“回到底部”，还有拖图进来时盖在对话区上的那层。
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { getDraft, setDraft } from "./drafts.js";
import { IconArrowDown, IconArrowUp, IconBulb, IconCheck, IconChevron, IconClose, IconImage, IconInsert, IconLock, IconStop, IconUndo, IconWarn, Spinner } from "./icons.jsx";
import { Viewer } from "./images.jsx";
import { useLayer } from "./layers.js";
import { NavContext, byKeyboard, touch } from "./nav.jsx";
import { QUEUED, creditsLeft, creditsLine, loginLine, modelName } from "./research.js";
import { IMAGE_ACCEPT, ImageError, prepareImage } from "./upload.js";
import styles from "./composer.module.css";
import menu from "./menu.module.css";
import sheet from "./sheet.module.css";

// 临时的输入框（改这句话、左栏改名）关掉以后，光标回到对话的输入框（等它从页面上撤掉再放）
export const focusInput = () => setTimeout(() => dispatchEvent(new Event("ws-focus-input")));

// 把一张图交给输入框，当成选了这张图（拖进对话区松手；图上的“改这张”见 images.jsx，发的是同一个事件）
const attachImage = (file) => dispatchEvent(new CustomEvent("ws-attach-image", { detail: file }));

// 输入框跟着字数长高，最高多高写在样式里（max-height：桌面窗口高度的四成，手机键盘占了一半、按四分之一；都不低于 160px），再多就在框里滚
export function useAutoHeight(ref, value) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [ref, value]);
}

// 回车：电脑上回车发送、Shift+回车换行；触屏键盘没有 Shift，回车就是换行，点按钮发送（2026-11-07 Sean 定）
export const enterSends = (e) => e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && !touch();

// 输入框那一块的外壳：撤销提示在手机上要躲开它（见 undo.jsx 找 data-dock）
export function Dock({ children }) {
  return <div className={styles.dock} data-dock>{children}</div>;
}

// 往上翻着看时，输入框上面浮一个“回到底部”；离开底部以后下面又有了新内容，写上字。按下去不抢光标：输入框里写着的字和光标都留着
export function JumpButton({ more, onClick }) {
  return (
    <button className={styles.jump + (more ? " " + styles.more : "")} aria-label={more ? "有新内容，回到底部" : "回到底部"} title="回到底部"
      onMouseDown={(e) => e.preventDefault()} onClick={onClick}><IconArrowDown size={16} />{more && <span>有新内容</span>}</button>
  );
}

// 拖图进来（2026-11-07 Sean 定）：拖到对话区（box）哪儿都行，盖一层“松手添加图片”，松手交给输入框。
// 拖到对话区外面（左栏）松手什么也不做，不让浏览器把图打开、离开页面。
// 拖出窗口：dragleave 没有下一个落点就收起；有的浏览器不给落点，靠 dragover 停了一会儿收起（拖着的时候它一直在发）
export function useImageDrop(box, enabled) {
  const [over, setOver] = useState(false);
  useEffect(() => {
    let timer;
    const later = (ms) => {
      clearTimeout(timer);
      timer = setTimeout(() => setOver(false), ms);
    };
    const files = (e) => !!e.dataTransfer?.types?.includes("Files");
    const inside = (e) => enabled && !!box.current?.contains(e.target);
    const onOver = (e) => {
      if (!files(e)) return;
      e.preventDefault();
      const ok = inside(e);
      e.dataTransfer.dropEffect = ok ? "copy" : "none";
      setOver(ok);
      later(600);
    };
    const onLeave = (e) => { if (files(e) && !e.relatedTarget) later(60); };
    const onDrop = (e) => {
      if (!files(e)) return;
      e.preventDefault();
      clearTimeout(timer);
      setOver(false);
      if (inside(e) && e.dataTransfer.files[0]) attachImage(e.dataTransfer.files[0]);
    };
    addEventListener("dragenter", onOver);
    addEventListener("dragover", onOver);
    addEventListener("dragleave", onLeave);
    addEventListener("drop", onDrop);
    return () => {
      clearTimeout(timer);
      removeEventListener("dragenter", onOver);
      removeEventListener("dragover", onOver);
      removeEventListener("dragleave", onLeave);
      removeEventListener("drop", onDrop);
    };
  }, [box, enabled]);
  return over;
}

// 拖着图在对话区上面时盖的那层（不挡鼠标，落点还是下面的东西）
export function DropHint() {
  return <div className={styles.drop} aria-hidden="true"><IconImage size={22} /><span>松手添加图片</span></div>;
}

const PLACEHOLDER = {
  new: "想问什么？",
  opening: "正在打开对话…",
  idle: "接着问…",
  busy: "可以先写下一句", // 回答时打了字，旁边会出“这一轮答完再发”（2026-11-07 Sean 定，不再在这里讲排队的规矩）
  queued: QUEUED,
  failed: "这一轮没有完成，点上面的“重试”",
};
const HELD = "上面那句发出去以后才能再发，也可以撤回它一起改";
const WITH_IMAGE = "说说要怎么改这张图，或照着它画什么";
const IMAGE_WAITS = "带图的话要等这一轮答完才能发";
const COUNT_FROM = 2000; // 超过这么多字在输入框里显示字数；不设上限（后端也没有），2026-11-06 Sean 定

// 额度快用完（用了八成）或用完了时，输入框上面提醒一句（见后端 app.quota，按最紧的那个额度）：和左下角额度面板同一种说法（creditsLine）；
// 游客再带上登录入口和登录后会怎样（loginLine）
export function CreditsNote({ credits, onLogin }) {
  const left = creditsLeft(credits);
  if (!left?.warn) return null;
  const after = onLogin && loginLine(credits, false);
  return (
    <div className={styles.credits + (left.exhausted ? " " + styles.out : "")} role="status">
      <IconWarn size={13} />
      <span>{left.exhausted ? "" : "额度快用完了："}{creditsLine(left)}{after && <>。<button className={styles.link} onClick={onLogin}>登录</button>{after}</>}</span>
    </div>
  );
}

// 还没发出去的话（最多一句，见 Composer 的 held），贴在输入框上沿：排着的（这一轮答完就发，可以改成插进这一轮），或者等着插进这一轮的。
// 撤回就放回输入框，可以改了再发。按钮按下去不抢光标，输入框里正写着的接着写
export function Pending({ st, act }) {
  const rows = [
    st?.guided_message && { key: "guided", text: st.guided_message.text, note: "做完手上这一步就插进这一轮", onWithdraw: act.unguide },
    st?.queued_message && { key: "queued", text: st.queued_message.text, note: "这一轮答完就发", onGuide: act.guide, onWithdraw: act.unqueue },
  ].filter(Boolean);
  if (!rows.length) return null;
  const keep = (e) => e.preventDefault();
  const btn = "ws-btn ghost sm text-muted hover:text-ink";
  return (
    <div className={styles.tray} aria-label="还没发出去的话">
      {rows.map((r) => (
        <div key={r.key} className="flex items-center gap-2.5 py-1.5 pr-2 pl-3">
          <div className="min-w-0 flex-1">
            <div className={styles.trayNote}>{r.note}{!touch() && <span aria-hidden="true">，输入框最前面按 <kbd className="ws-kbd">↑</kbd>也能撤回</span>}</div>
            <div className="truncate text-[13px] leading-[1.5] text-ink2" title={r.text}>{r.text}</div>
          </div>
          <div className="flex flex-none gap-0.5">
            {r.onGuide && <button className={btn} title="不等这一轮答完：模型做完手上这一步就看到这句话，接着按它调整" onMouseDown={keep} onClick={r.onGuide}><IconInsert size={13} />插进这一轮</button>}
            <button className={btn} title="放回输入框，改了再发（光标在输入框最前面时按 ↑ 也行）" onMouseDown={keep} onClick={r.onWithdraw}><IconUndo size={13} />撤回</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// 每个对话选好、还没发的图（{file, url}，见 upload.js）：存不进本机，只在这次打开的页面里留着——切对话再回来还在，刷新就没了
const attachments = new Map();
const keepImage = (key, att) => (att?.url ? attachments.set(key, att) : attachments.delete(key));

// 回答时（queueing）也能打字：停止键一直在原位，不变成别的；打了字，旁边另外出一个“这一轮答完再发”，话排着，这一轮答完就发。Esc 停止回答。
// held：已经有一句没发出去（排着或等着插进这一轮）。没发出去的话最多一句：这时还能打字，但发不出去，要先撤回那句、改好一起发。
// 带图的话排不了（排着的话只有字），要等这一轮答完再发。
// 图：点左下角选、粘贴、拖进对话区都行（拖的见 useImageDrop），一次一张（再选换掉），大图在本机先压到 4 MB 以内（见 upload.js）。
// 没发出去的字每个对话各存一份在本机（见 drafts.js）：切走、刷新、登录回来都在，发出去就清掉；图只在这次打开的页面里留着（见 attachments）。
// carry：新对话刚发出去、拿到了 id，thread 从 "new" 换成 id，还是同一个对话
// putBack：放回输入框的话（撤回的、没发出去的，可能带着图）：放在已经写着的字前面、空一行，放回来的那段选中，看得出哪段是放回来的
//   （被拒的那句还原样留着就换掉它，不叠加）；不是正在看的对话，放进那个对话的草稿
// onTakeBack：有没发出去的话时，光标在最前面（输入框空着也算）按 ↑ 把它们取回来。光标在最前面时 ↑ 本来什么也不做，不和挪光标抢
export function Composer({ thread, carry, mode, queueing, held, stopping, putBack, onSend, onStop, above, onTakeBack, tools }) {
  const [text, setText] = useState(() => getDraft(thread));
  const [image, setImage] = useState(() => attachments.get(thread) || null); // {file, url}；还在处理是 {pending: true}
  const [imageError, setImageError] = useState(null);
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
    // 图上点了“改这张”（见 images.jsx）、拖进对话区松手（见 useImageDrop）：当成选了这张图，桌面上光标进输入框好接着说
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
    <div className="px-7 py-2.5 phone:px-4 phone:pt-2 phone:pb-[calc(8px+env(safe-area-inset-bottom))]">
      {above}
      {/* 点输入框这一块的空白处（四周留白、按钮之间，标着 data-blank）也把光标放进去：可点的是整块，不只是一行高的 textarea（第 31 条） */}
      <div className={styles.box} onClick={(e) => { if (e.target === e.currentTarget || e.target.hasAttribute("data-blank")) ref.current?.focus(); }}>
        {image && <Attachment att={image} onRemove={removeImage} />}
        <textarea ref={ref} rows={1} value={text} aria-label="输入"
          placeholder={queueing && held ? HELD : image && sendNow ? WITH_IMAGE : PLACEHOLDER[mode] || PLACEHOLDER.busy}
          onChange={(e) => setText(e.target.value)} onPaste={onPaste}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return;
            if (enterSends(e)) {
              e.preventDefault();
              send();
            } else if (e.key === "ArrowUp" && onTakeBack && !(e.shiftKey || e.altKey || e.metaKey || e.ctrlKey)
              && e.currentTarget.selectionStart === 0 && e.currentTarget.selectionEnd === 0) {
              e.preventDefault();
              onTakeBack();
            }
          }} />
        {note && <div className={styles.note + (note.warn ? " " + styles.warn : "")} role={note.warn ? "alert" : undefined}>{note.warn && <IconWarn size={13} />}<span>{note.text}</span></div>}
        <div className="mt-2 flex items-center gap-1 phone:gap-2" data-blank>
          <button className={styles.attach} aria-label="添加图片" title="添加一张图：让它改这张图，或照着画" disabled={mode === "opening"} onMouseDown={keep}
            onClick={() => fileRef.current?.click()}><IconImage size={16} /></button>
          <input ref={fileRef} type="file" accept={IMAGE_ACCEPT} hidden tabIndex={-1}
            onChange={(e) => { attach(e.target.files?.[0]); e.target.value = ""; }} />
          {count > COUNT_FROM && <span className="flex-none text-[12px] whitespace-nowrap text-muted tabular-nums">{count.toLocaleString("en-US")} 字</span>}
          <div className="ml-auto flex min-w-0 items-center" data-blank>{tools}</div>
          {canStop && hasText && (
            <button className={"ws-btn sm " + styles.later} disabled={!canQueue} onMouseDown={keep} onClick={send}><IconArrowUp size={13} />这一轮答完再发</button>
          )}
          {canStop
            ? <button className={styles.send + " " + styles.stop} aria-label={stopping ? "正在停止" : "停止回答"} title={stopping ? "正在停止" : "停止回答（Esc）"} disabled={stopping} onClick={onStop}>
              {stopping ? <Spinner size={14} /> : <IconStop size={13} />}
            </button>
            : <button className={styles.send} aria-label="发送" disabled={!sendNow || !hasText || !!image?.pending} onClick={send}><IconArrowUp size={15} /></button>}
        </div>
      </div>
      <div className={styles.ai}>内容由 AI 生成，请仔细甄别</div>
    </div>
  );
}

// 选好要发的图：缩略图（点开看大图）、能拿它做什么，右上角去掉。大图还在本机压缩时先转圈
function Attachment({ att, onRemove }) {
  const [viewing, setViewing] = useState(null);
  return (
    <div className="mb-2 flex items-center gap-3 phone:mt-1">
      <div className="relative flex size-12 flex-none items-center justify-center rounded-sm border border-line bg-side text-muted">
        {att.url
          ? <button className={styles.open} aria-label="看大图" onClick={(e) => setViewing(e.currentTarget)}><img src={att.url} alt="要发的图" /></button>
          : <Spinner size={16} />}
        <button className={styles.remove} aria-label="去掉这张图" onMouseDown={(e) => e.preventDefault()} onClick={onRemove}><IconClose size={12} /></button>
      </div>
      <span className="min-w-0 text-[12px] leading-[1.5] text-muted" role="status">{att.url ? "可以让它改这张图、照着它画；它看不到图里的内容" : "正在处理图片…"}</span>
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
export function ModelTools({ models, loading, current, locked, onPick, thinking, onThinking, onLogin }) {
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
  if (loading) return <span className="inline-block h-7 w-[120px] rounded-full bg-hover" aria-hidden="true" />;
  if (!models?.length || !current) return null;
  const toggle = opt?.thinking === "toggle";
  const name = opt?.name || current;
  const note = locked ? "对话开始后不能换模型，想换模型请开新对话" : "开始对话后不能换模型";
  const list = (
    <ModelList models={models} current={current} locked={locked} onLogin={onLogin}
      onChoose={(m) => { close(); if (m.locked) onLogin?.(m); else if (!locked) onPick(m.id); }} />
  );
  return (
    <div className="relative flex min-w-0 items-center gap-1" ref={box}>
      {toggle && !mobile && <ThinkToggle on={thinking} onChange={onThinking} />}
      <button ref={btn} className={styles.modelBtn} aria-haspopup={mobile ? "dialog" : "menu"} aria-expanded={open}
        aria-label={mobile && toggle ? `${name}，深度思考${thinking ? "已开" : "已关"}` : undefined}
        title={mobile ? undefined : locked ? "这段对话用的模型" : "选模型"} onClick={() => (open ? close() : setOpen(true))}>
        {mobile && toggle && <IconBulb size={14} className={styles.bulb + (thinking ? " " + styles.on : "")} />}
        <span className="min-w-0 truncate">{name}</span><IconChevron size={13} className={styles.caret} />
      </button>
      {open && !mobile && <ModelMenu anchor={box} note={note} onLeave={() => setOpen(false)}>{list}</ModelMenu>}
      {open && mobile && (
        <ModelSheet title={locked ? "这段对话的模型" : "选模型"} onClose={close}>
          {toggle && <ThinkSwitch on={thinking} onChange={onThinking} />}
          <div className={menu.note}>{note}</div>
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
    <span className="relative inline-flex" onMouseEnter={() => setTip(true)} onMouseLeave={() => setTip(false)}>
      <button className={styles.think + (on ? " " + styles.on : "")} aria-pressed={on} aria-describedby="ws-think-tip" onMouseDown={(e) => e.preventDefault()} onClick={() => onChange(!on)}
        onFocus={(e) => { if (e.currentTarget.matches(":focus-visible")) setTip(true); }} onBlur={() => setTip(false)}>
        <IconBulb size={14} />深度思考
      </button>
      <span id="ws-think-tip" role="tooltip" className={styles.tip + (tip ? " " + styles.show : "")}>
        <b>深度思考{on ? "已开" : "已关"}</b>
        <span className={on ? styles.cur : undefined}>{THINK_ON}</span>
        <span className={on ? undefined : styles.cur}>{THINK_OFF}</span>
      </span>
    </span>
  );
}

// 底部弹层里的思考开关：一整行都能点，下面写明开、关的差别
function ThinkSwitch({ on, onChange }) {
  return (
    <div className="mb-1 border-b border-line pb-3">
      <button className={styles.switchRow} role="switch" aria-checked={on} aria-describedby="ws-think-desc" onClick={() => onChange(!on)}>
        <IconBulb size={16} /><span className="flex-1">深度思考</span><span className={"ws-switch" + (on ? " on" : "")} aria-hidden="true" />
      </button>
      <p id="ws-think-desc" className="m-0 flex flex-col gap-1 px-3 text-[13px] leading-[1.6] text-muted"><span>{THINK_ON}</span><span>{THINK_OFF}</span></p>
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
    <div ref={ref} className={styles.menu} data-model-scroll style={place ? { [place.up ? "bottom" : "top"]: "calc(100% + 8px)", maxHeight: place.max } : { visibility: "hidden" }}
      onBlur={(e) => { if (e.relatedTarget && !anchor.current?.contains(e.relatedTarget)) onLeave(); }}>
      <div className={menu.note + " " + styles.menuNote}>{note}</div>
      {children}
    </div>
  );
}

// 手机上的底部弹层：盖一层暗底，点暗底、按 Esc（外接键盘）、点关闭都收起；打开时光标在里面、Tab 在里面转
function ModelSheet({ title, onClose, children }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    (el.querySelector('[aria-checked="true"]:not(:disabled):not([role="switch"])') || el.querySelector("[data-sheet-close]"))?.focus();
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
      <div className={sheet.scrim} onClick={onClose} aria-hidden="true" />
      <div ref={ref} className={sheet.sheet} data-sheet role="dialog" aria-modal="true" aria-label={title} onKeyDown={trap}>
        <div className={sheet.head}>
          <b>{title}</b>
          <button className="ws-icon-btn" data-sheet-close aria-label="关闭" onClick={onClose}><IconClose size={18} /></button>
        </div>
        <div className={sheet.body} data-model-scroll>{children}</div>
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
    const box = t?.closest("[data-model-scroll]");
    if (!box) return;
    const top = box.scrollTop + t.getBoundingClientRect().top - box.getBoundingClientRect().top - 4;
    box.scrollTo({ top, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [showShut]);
  const actual = auto.model ? modelName(models, auto.model, null) : null;
  const autoNote = [auto.description, actual && `现在用 ${actual}`].filter(Boolean).join("，");
  const row = (m, note) => (
    <button key={m.id} role="menuitemradio" aria-checked={m.id === current} className={styles.row + (m.locked ? " " + styles.locked : "")}
      disabled={m.locked ? !onLogin : locked && m.id !== current} onClick={() => onChoose(m)}>
      <span className="flex min-w-0 flex-col"><b className={styles.name}>{m.name}</b>{note && <small className={styles.desc}>{note}</small>}</span>
      {m.id === current ? <IconCheck size={14} /> : m.locked && <IconLock size={13} className={styles.lock} />}
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
    <div className="flex flex-col" role="menu" aria-label="选模型" onKeyDown={move}>
      <div role="group" aria-label="推荐">
        <div className={styles.group} aria-hidden="true">推荐</div>
        {row(auto, autoNote)}
      </div>
      {free.length > 0 && (
        <div role="group" aria-label="可用">
          <div className={styles.group} aria-hidden="true">可用</div>
          {free.map((m) => row(m, m.description))}
        </div>
      )}
      {shut.length > 0 && (
        <div role="group" aria-label="登录后可用">
          <button ref={shutRef} className={styles.group + " " + styles.toggle} role="menuitem" aria-expanded={showShut} onClick={() => { reveal.current = !showShut; setShowShut(!showShut); }}>
            <span>登录后可用（{shut.length}）</span><IconChevron size={13} className={"chev" + (showShut ? " up" : "")} />
          </button>
          {showShut && shut.map((m) => row(m, m.description))}
        </div>
      )}
    </div>
  );
}
