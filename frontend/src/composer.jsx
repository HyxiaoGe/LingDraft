// 输入框那一块（样式在 composer.module.css 和元素上的 Tailwind 类）：
// 输入框本身（写字、加图和文件、发送 / 停止、这一轮答完再发），右下角的深度思考和选模型，贴在上沿的“还没发出去的话”，
// 上面的额度提醒和“回到底部”，还有拖图、拖文件进来时盖在对话区上的那层。
import { Sparkles } from "lucide-react";
import { useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { getFile, removeFile, uploadFile } from "./api.js";
import { getDraft, setDraft } from "./drafts.js";
import { FileCard, fileFacts } from "./files.jsx";
import { IconArrowDown, IconArrowUp, IconBulb, IconCheck, IconChevron, IconClip, IconClose, IconInsert, IconLock, IconStop, IconUndo, IconWarn, Spinner } from "./icons.jsx";
import { Viewer } from "./images.jsx";
import { useLayer } from "./layers.js";
import { NavContext, byKeyboard, focusInput, touch, typeNext } from "./nav.jsx";
import { QUEUED, creditsLeft, creditsLine, loginLine } from "./research.js";
import { ImageError, MAX_FILES, MAX_IMAGES, fileErrorText, fileShortError, isImage, prepareImage, tooBig } from "./upload.js";
import styles from "./composer.module.css";
import menu from "./menu.module.css";
import sheet from "./sheet.module.css";

export { focusInput, typeNext } from "./nav.jsx"; // 光标回输入框（见 nav.jsx），输入框自己听 ws-focus-input

// 把图、文件交给输入框，当成选了这些（拖进对话区松手；图上的“改这张”见 images.jsx，发的是同一个事件，detail 是一个或一组）
const attachImage = (files) => dispatchEvent(new CustomEvent("ws-attach-image", { detail: files }));

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
      onMouseDown={(e) => e.preventDefault()} onClick={onClick}><IconArrowDown size={18} />{more && <span>有新内容</span>}</button>
  );
}

// 拖图、拖文件进来（2026-11-07 Sean 定）：拖到对话区（box）哪儿都行，盖一层“松手添加图片或文件”，松手交给输入框（图和别的文件在那里分开）。
// 拖到对话区外面（左栏）松手什么也不做，不让浏览器把文件打开、离开页面。
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
      if (inside(e) && e.dataTransfer.files.length) attachImage([...e.dataTransfer.files]);
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

// 拖着图、文件在对话区上面时盖的那层（不挡鼠标，落点还是下面的东西）
export function DropHint() {
  return <div className={styles.drop} aria-hidden="true"><IconClip size={22} /><span>松手添加图片或文件</span></div>;
}

const PLACEHOLDER = {
  new: "想问什么？",
  opening: "正在打开对话…",
  idle: "接着问…",
  busy: "可以先写下一句", // 回答时打了字，旁边会出“这一轮答完再发”（2026-11-07 Sean 定，不再在这里讲排队的规矩）
  queued: QUEUED,
  failed: "这一轮没有完成，点上面的“重试”",
  asking: "写下你的回答…", // 模型停下来问了：这里写的就是回答
};
const MAX_ANSWER = 2000; // 回答问题最多这么多字（后端 app.tools.ask_user 的 MAX_ANSWER）
const HELD = "上面那句发出去以后才能再发，也可以撤回它一起改";
const WITH_IMAGE = "问问图里的内容，或者说说要怎么改";
const WITH_FILE = "问问文件里的内容，或者说说要拿它做什么";
const WITH_BOTH = "问问图和文件里的内容";
const waits = (what) => `带${what}的话要等这一轮答完才能发`;
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
// 撤回就放回输入框，可以改了再发。按钮按下去不抢光标，输入框里正写着的接着写。
// asking：这一轮停下来问用户了，这时插不进去（没有在跑的），排着的照样等这一轮答完
export function Pending({ st, act, asking }) {
  const rows = [
    st?.guided_message && { key: "guided", text: st.guided_message.text, note: "做完手上这一步就插进这一轮", onWithdraw: act.unguide },
    st?.queued_message && { key: "queued", text: st.queued_message.text, note: "这一轮答完就发", onGuide: asking ? null : act.guide, onWithdraw: act.unqueue },
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

// 每个对话选好、还没发的图（[{key, file, url}]，见 upload.js；还在处理的是 {key, pending: true}）：存不进本机，只在这次打开的页面里留着——
// 切对话再回来还在（处理到一半的，处理好了放进这里），刷新就没了
const attachments = new Map();
const keepImages = (thread, list) => (list?.length ? attachments.set(thread, list) : attachments.delete(thread));
let slots = 0; // 每张选进来的图一个记号：处理好了按它放回原位，处理时去掉了的不要

// 每个对话选好、还没发的文件（[{key, name, size, status, id?, reason?, detail?, file?}]），和图一样只在这次打开的页面里留着。
// status：uploading（传着）、parsing（传上去了，后台解析中，隔一会儿问一次）、ready（file 是随消息发的那份）、
// failed（reason 是原因，见 upload.js 的 fileErrorText；要去掉才能发）。
// 选好就传（见 pickFiles），不等发送：传和解析都在后台，不挡着打字、切对话；换了对话，传完、解析完的照样记到原来那个对话名下
const picked = new Map();
const watchers = new Set();
const NO_FILES = [];
const filesOf = (thread) => picked.get(thread) || NO_FILES;
const setFiles = (thread, list) => {
  if (list.length) picked.set(thread, list);
  else picked.delete(thread);
  watchers.forEach((f) => f());
};
const watchFiles = (f) => {
  watchers.add(f);
  return () => watchers.delete(f);
};
const findFile = (key) => {
  for (const [thread, list] of picked) {
    const f = list.find((d) => d.key === key);
    if (f) return { thread, f };
  }
  return null;
};
// 改一个文件的状态；已经去掉了（或者发出去了）返回 false
const patchFile = (key, patch) => {
  const at = findFile(key);
  if (!at) return false;
  setFiles(at.thread, filesOf(at.thread).map((d) => (d.key === key ? { ...d, ...patch } : d)));
  return true;
};
const uploads = new Map(); // 传着的：key → AbortController，去掉时断开
let fileSlots = 0;
const viewed = (v) => ({ id: v.id, status: v.status, reason: v.reason, file: v.file });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// 选进来的文件接在后面，先挡下传之前就知道传不了的（太大、空的），别的马上传
function pickFiles(thread, files) {
  const add = files.map((f) => ({ key: ++fileSlots, name: f.name || "未命名文件", size: f.size, src: f, status: tooBig(f) ? "failed" : "uploading", reason: tooBig(f) }));
  setFiles(thread, [...filesOf(thread), ...add.map(({ src, ...d }) => d)]);
  for (const d of add) if (d.status === "uploading") upload(d.key, d.src);
}

async function upload(key, file) {
  const ctl = new AbortController();
  uploads.set(key, ctl);
  try {
    const v = await uploadFile(file, ctl.signal);
    uploads.delete(key);
    if (!patchFile(key, viewed(v))) return removeFile(v.id); // 传的时候去掉了
    if (v.status === "parsing") track(key, v.id);
  } catch (e) {
    uploads.delete(key);
    if (ctl.signal.aborted) return;
    const reason = e.detail?.reason || (!e.status ? "offline" : e.status === 413 ? "too_large" : e.status === 429 || e.status >= 500 ? "busy" : "failed");
    patchFile(key, { status: "failed", reason, detail: e.detail || undefined });
  }
}

// 解析中的隔一会儿问一次（开头问得勤，十几秒以后慢下来），好了、读不了就停；去掉了、发出去了也停。
// 问不到（网络断了）接着问，连着一分多钟都问不到算没传成
async function track(key, id) {
  for (let i = 0, errs = 0; ; i++) {
    await wait(i < 15 ? 800 : 2500);
    const at = findFile(key);
    if (!at || at.f.id !== id || at.f.status !== "parsing") return;
    try {
      const v = await getFile(id);
      errs = 0;
      if (v.status !== "parsing") return patchFile(key, viewed(v));
    } catch (e) {
      if (e.status === 404) return patchFile(key, { status: "failed", reason: "expired" });
      if (++errs >= 30) return patchFile(key, { status: "failed", reason: "offline" });
    }
  }
}

// 没发出去、放回输入框的文件：再问一次现在的状态（可能过期清掉了）
function recheck(list) {
  for (const d of list) {
    if (!d.id || d.status !== "ready") continue;
    getFile(d.id).then((v) => patchFile(d.key, viewed(v)), (e) => e.status === 404 && patchFile(d.key, { status: "failed", reason: "expired" }));
  }
}

// 去掉一个：传着的断开，传上去了的让后端也删掉
function dropFile(thread, key) {
  const d = filesOf(thread).find((x) => x.key === key);
  if (!d) return;
  uploads.get(key)?.abort();
  if (d.id) removeFile(d.id);
  setFiles(thread, filesOf(thread).filter((x) => x.key !== key));
}

// 回答时（queueing）也能打字：停止键一直在原位，不变成别的；打了字，旁边另外出一个“这一轮答完再发”，话排着，这一轮答完就发。Esc 停止回答。
// held：已经有一句没发出去（排着或等着插进这一轮）。没发出去的话最多一句：这时还能打字，但发不出去，要先撤回那句、改好一起发。
// 带图、带文件的话排不了（排着的话只有字），要等这一轮答完再发。
// 图和文件：点左下角的回形针选（能多选，图和别的文件一起选也行）、粘贴、拖进对话区都行（拖的见 useImageDrop），到这里再分开：
// - 图一句话最多 MAX_IMAGES 张，大图在本机先压到 4 MB 以内（见 upload.js），随消息一起传；
// - 文件一句话最多 MAX_FILES 个，选好就传、后台解析（见 pickFiles），都解析好了才能发；读不了的标出来，去掉才能发。
// 没发出去的字每个对话各存一份在本机（见 drafts.js）：切走、刷新、登录回来都在，发出去就清掉；图和文件只在这次打开的页面里留着（见 attachments、picked）。
// carry：新对话刚发出去、拿到了 id，thread 从 "new" 换成 id，还是同一个对话
// putBack：放回输入框的话（撤回的、没发出去的，可能带着图）：放在已经写着的字前面、空一行，放回来的那段选中，看得出哪段是放回来的
//   （被拒的那句还原样留着就换掉它，不叠加）；不是正在看的对话，放进那个对话的草稿
// onTakeBack：有没发出去的话时，光标在最前面（输入框空着也算）按 ↑ 把它们取回来。光标在最前面时 ↑ 本来什么也不做，不和挪光标抢
// 模型停下来问用户时（mode 是 asking）：这里写的就是回答，只发字（最多 MAX_ANSWER 个），选着的图和文件留着下一句用
export function Composer({ thread, carry, mode, queueing, held, stopping, putBack, onSend, onStop, above, onTakeBack, lead, tools }) {
  const [text, setText] = useState(() => getDraft(thread));
  const [images, setImages] = useState(() => attachments.get(thread) || []); // 见 attachments
  const [attachError, setAttachError] = useState(null); // 选图、选文件时说的一句（超过张数、图读不出来）
  const docs = useSyncExternalStore(watchFiles, () => filesOf(thread)); // 见 picked
  const ref = useRef(null);
  const fileRef = useRef(null);
  const shown = useRef(thread); // text、images 是哪个对话的
  const textRef = useRef(text);
  textRef.current = text;
  const imagesRef = useRef(images);
  imagesRef.current = images;
  // 改选着的图都走这里：imagesRef 马上跟上（小图处理得快，没等重新渲染就回来了，要看得到刚占的位置）
  const putImages = (next) => {
    imagesRef.current = next;
    setImages(next);
  };
  const selectNext = useRef(null); // 放回来的话要选中的那段：[起, 止]，字放进去以后再选
  const sendNow = mode === "new" || mode === "idle";
  const canStop = mode === "busy" || mode === "queued";
  const asking = mode === "asking";
  const hasText = !!text.trim();
  const tooLong = asking && Array.from(text.trim()).length > MAX_ANSWER;
  const canQueue = queueing && !held && !images.length && !docs.length;
  const docsBusy = docs.some((d) => d.status === "uploading" || d.status === "parsing");
  const docFailed = docs.find((d) => d.status === "failed");
  const processing = images.some((i) => i.pending) || docsBusy || !!docFailed || docs.length > MAX_FILES;
  const enabled = sendNow || asking || (queueing && !held); // 能说话了（光标该回来了）
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
  }, [text]);
  useEffect(() => {
    addEventListener("pagehide", save);
    return () => {
      removeEventListener("pagehide", save);
      save();
      keepImages(shown.current, imagesRef.current);
    };
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
    if (was === "new" && carry) { // 同一个对话：等回答时写的字、选的图和文件留着，光标也不动；草稿挪到这个对话名下
      setDraft("new", "");
      setDraft(thread, textRef.current);
      if (filesOf("new").length) {
        setFiles(thread, [...filesOf(thread), ...filesOf("new")]);
        setFiles("new", []);
      }
      return;
    }
    setDraft(was, textRef.current);
    keepImages(was, imagesRef.current);
    setText(getDraft(thread));
    putImages(attachments.get(thread) || []);
    setAttachError(null);
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
    if (putBack.docs?.length) { // 放回来的文件排在已经选着的前面
      const keys = new Set(putBack.docs.map((d) => d.key));
      setFiles(putBack.thread, [...putBack.docs, ...filesOf(putBack.thread).filter((d) => !keys.has(d.key))]);
      recheck(putBack.docs);
    }
    if (putBack.thread !== shown.current) {
      setDraft(putBack.thread, join(getDraft(putBack.thread)));
      if (putBack.images?.length) keepImages(putBack.thread, putBack.images);
      return;
    }
    const cur = textRef.current;
    const next = join(cur);
    selectNext.current = joined(cur) ? [0, putBack.text.length] : [next.length, next.length];
    setText(next);
    if (putBack.images?.length) putImages(putBack.images);
    focus(true);
  }, [putBack]);
  useEffect(() => {
    // 图上点了“改这张”（见 images.jsx）、拖进对话区松手（见 useImageDrop）：当成选了这些图，桌面上光标进输入框好接着说
    const take = (e) => {
      if (!e.detail) return setAttachError("没取到这张图，再点一次“改这张”");
      attachRef.current(e.detail);
      focus(true);
    };
    // 右栏文件里选中一段点了“引用”（见 artifact.jsx 的 quoteText）：放在已经写着的字前面（它自己带着空行），光标停在它后面，接着说怎么改
    const quote = (e) => {
      const q = e.detail;
      if (!q) return;
      const cur = textRef.current;
      const next = cur.trim() ? q + cur : q;
      selectNext.current = [q.length, q.length];
      setText(next);
      focus(true);
    };
    addEventListener("ws-attach-image", take);
    addEventListener("ws-quote", quote);
    return () => {
      removeEventListener("ws-attach-image", take);
      removeEventListener("ws-quote", quote);
    };
  }, []);
  useEffect(() => {
    const back = (e) => focus(!!e.detail);
    const here = () => ref.current?.focus({ preventScroll: true }); // 点了“自己写”（见 nav.jsx 的 typeHere）：触屏上也放
    // 切去别的窗口、标签页再回来（2026-11-07 Sean 要的）：光标没在哪儿，或者只是点了正文停在某一轮上，就放回输入框，回来接着打字。
    // 选着字、开着弹窗、光标在别的输入框或按钮链接上时不动；触屏不放（免得自己弹出键盘，见 focus）。等浏览器先把原来的焦点还回去再看
    const ret = () => setTimeout(() => {
      if (document.hidden || !document.hasFocus() || getSelection()?.toString() || document.querySelector('[aria-modal="true"]')) return;
      const at = document.activeElement;
      if (at && at !== document.body && (at.tabIndex >= 0 || at.isContentEditable || at.closest('[role="dialog"], [role="menu"]'))) return;
      focus(true);
    });
    const shown = () => !document.hidden && ret();
    // 光标在输入框里、或者没在哪儿（点了正文）时才停；在改名、菜单、角标这些地方，Esc 归它们自己。
    // 在捕获阶段看：等它们处理完，改名框已经撤掉，光标就不在那儿了
    const esc = (e) => {
      const at = document.activeElement;
      if (e.key !== "Escape" || e.isComposing || !stopNow.current) return;
      if (at && at !== document.body && at !== ref.current) return;
      stopNow.current();
    };
    addEventListener("ws-focus-input", back); // 见 focusInput
    addEventListener("ws-type-here", here);
    addEventListener("keydown", esc, true);
    addEventListener("focus", ret);
    document.addEventListener("visibilitychange", shown);
    return () => {
      removeEventListener("ws-focus-input", back);
      removeEventListener("ws-type-here", here);
      removeEventListener("keydown", esc, true);
      removeEventListener("focus", ret);
      document.removeEventListener("visibilitychange", shown);
    };
  }, []);

  // 选了、粘贴了、拖进来的：图和别的文件分开（见 Composer 上面的说明），文件超过 MAX_FILES 个的不要，说一声
  const attach = (files) => {
    const list = [].concat(files || []).filter(Boolean);
    if (!list.length) return;
    setAttachError(null);
    const imgs = list.filter(isImage);
    const others = list.filter((f) => !isImage(f));
    if (imgs.length) attachImages(imgs);
    if (others.length) {
      const room = MAX_FILES - filesOf(shown.current).length;
      if (others.length > room) setAttachError(`一句话最多带 ${MAX_FILES} 个文件`);
      if (room > 0) pickFiles(shown.current, others.slice(0, room));
    }
  };
  // 图：接在已经选了的后面，先占上位置转圈，处理好（大图压缩）再显示；超过 MAX_IMAGES 张的不要，说一声。
  // 处理的时候换了对话，处理好的图归原来那个对话；处理的时候去掉了的，处理好了也不要
  const attachImages = (list) => {
    const thread = shown.current;
    const room = MAX_IMAGES - imagesRef.current.length;
    if (list.length > room) setAttachError(`一句话最多带 ${MAX_IMAGES} 张图`);
    const take = list.slice(0, Math.max(0, room)).map((file) => ({ file, key: ++slots }));
    if (!take.length) return;
    putImages([...imagesRef.current, ...take.map(({ key }) => ({ key, pending: true }))]);
    for (const { file, key } of take) {
      prepareImage(file).then(
        (f) => place(thread, key, { key, file: f, url: URL.createObjectURL(f) }),
        (e) => place(thread, key, null, e instanceof ImageError ? e.message : "这张图读不出来，换一张再试"),
      );
    }
  };
  // 处理好的一张放回它占的位置（没处理成就去掉、说原因）
  const place = (thread, key, att, error) => {
    const swap = (list) => (att ? list.map((i) => (i.key === key ? att : i)) : list.filter((i) => i.key !== key));
    const here = shown.current === thread;
    const list = here ? imagesRef.current : attachments.get(thread) || [];
    if (!list.some((i) => i.key === key)) {
      if (att) URL.revokeObjectURL(att.url);
      return;
    }
    if (here) {
      putImages(swap(list));
      if (error) setAttachError(error);
    } else keepImages(thread, swap(list));
  };
  const attachRef = useRef(attach);
  attachRef.current = attach;
  const removeImage = (key) => {
    putImages(imagesRef.current.filter((i) => i.key !== key));
    setAttachError(null);
    focus(true);
  };
  const removeDoc = (key) => {
    dropFile(shown.current, key);
    setAttachError(null);
    focus(true);
  };
  // 粘贴：剪贴板里有图以外的文件（在访达、资源管理器里复制的文件）就当文件，连着一起复制的图也带上；
  // 只有图时，没带着字的（截图、网页上“复制图片”）才当图，带着字的（从文档里复制的常常也带一张图）照常粘贴文字
  const onPaste = (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const all = [...cd.files];
    const files = all.some((f) => !isImage(f)) ? all : cd.types.includes("text/plain") ? [] : all.filter(isImage);
    if (!files.length) return;
    e.preventDefault();
    attach(files);
  };

  const send = () => {
    const t = text.trim();
    if (!t) return;
    if (asking) {
      if (tooLong) return;
      onSend(t, [], []);
    } else if (processing) return;
    else if (sendNow) {
      onSend(t, images, docs);
      putImages([]);
      setFiles(shown.current, []); // 发出去了：后端绑到这段对话，不删
    } else if (canQueue) onSend(t, [], []);
    else return;
    setText("");
    setAttachError(null);
    clearTimeout(saveTimer.current);
    setDraft(shown.current, "");
  };
  // 输入框下面那一句：选图、选文件时说的 > 回答问题时太长、带着图和文件 > 读不了的文件（去掉才能发）> 文件太多 > 回答时打了字发不出去的原因 > 文件还没解析好
  const both = images.length && docs.length;
  const note = attachError ? { text: attachError, warn: true }
    : tooLong ? { text: `回答最多 ${MAX_ANSWER.toLocaleString("en-US")} 个字，删短一些再发`, warn: true }
    : asking && hasText && (images.length || docs.length) ? { text: `回答只发文字，${both ? "图和文件" : docs.length ? "文件" : "图"}留到下一句` }
    : docFailed ? { text: `「${docFailed.name}」${fileErrorText(docFailed.reason, docFailed.detail)}，去掉它才能发`, warn: true }
      : docs.length > MAX_FILES ? { text: `一句话最多带 ${MAX_FILES} 个文件，去掉几个再发`, warn: true }
        : queueing && hasText && held ? { text: HELD }
          : queueing && hasText && (images.length || docs.length) ? { text: waits(both ? "图和文件" : docs.length ? "文件" : "图") }
            : sendNow && hasText && docsBusy ? { text: "文件解析好了就能发" } : null;
  const withWhat = both ? WITH_BOTH : docs.length ? WITH_FILE : images.length ? WITH_IMAGE : null;
  const full = images.length >= MAX_IMAGES && docs.length >= MAX_FILES;
  const count = text.length > COUNT_FROM ? Array.from(text).length : 0;
  const keep = (e) => e.preventDefault(); // 按下去不抢光标
  return (
    <div className="px-7 py-2.5 phone:px-4 phone:pt-2 phone:pb-[calc(8px+env(safe-area-inset-bottom))]">
      {above}
      {/* 点输入框这一块的空白处（四周留白、按钮之间，标着 data-blank）也把光标放进去：可点的是整块，不只是一行高的 textarea（第 31 条） */}
      <div className={styles.box} onClick={(e) => { if (e.target === e.currentTarget || e.target.hasAttribute("data-blank")) ref.current?.focus(); }}>
        {(images.length > 0 || docs.length > 0) && <Attachments list={images} docs={docs} onRemove={removeImage} onRemoveDoc={removeDoc} />}
        <textarea ref={ref} rows={1} value={text} aria-label="输入"
          placeholder={queueing && held ? HELD : withWhat && sendNow ? withWhat : PLACEHOLDER[mode] || PLACEHOLDER.busy}
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
          {/* 不限类型：图在这边分开，别的文件后端认，认不出的说一声（手机上点开能选照片、拍照或文件） */}
          <button className={styles.attach} aria-label="添加图片或文件" title={full ? `一句话最多带 ${MAX_IMAGES} 张图、${MAX_FILES} 个文件` : "添加图片或文件"}
            disabled={mode === "opening" || full} onMouseDown={keep}
            onClick={() => fileRef.current?.click()}><IconClip size={16} /></button>
          <input ref={fileRef} type="file" multiple hidden tabIndex={-1}
            onChange={(e) => { attach([...(e.target.files || [])]); e.target.value = ""; }} />
          {/* 手机上回答时打了字：这一行放不下了（多出“这一轮答完再发”），Chat / Work 先收起，照之前选着的发；清掉字又出来 */}
          {lead && <span className={canStop && hasText ? "contents phone:hidden" : "contents"}>{lead}</span>}
          {count > COUNT_FROM && <span className="flex-none text-[12px] whitespace-nowrap text-muted tabular-nums">{count.toLocaleString("en-US")} 字</span>}
          <div className="ml-auto flex min-w-0 items-center" data-blank>{tools}</div>
          {canStop && hasText && (
            <button className={"ws-btn sm " + styles.later} disabled={!canQueue} onMouseDown={keep} onClick={send}><IconArrowUp size={13} />这一轮答完再发</button>
          )}
          {canStop
            ? <button className={styles.send + " " + styles.stop} aria-label={stopping ? "正在停止" : "停止回答"} title={stopping ? "正在停止" : "停止回答（Esc）"} disabled={stopping} onClick={onStop}>
              {stopping ? <Spinner size={14} /> : <IconStop size={13} />}
            </button>
            : <button className={styles.send} aria-label={asking ? "发送回答" : "发送"} disabled={asking ? !hasText || tooLong : !sendNow || !hasText || processing} onClick={send}><IconArrowUp size={15} /></button>}
        </div>
      </div>
      <div className={styles.ai}>内容由 AI 生成，请仔细甄别</div>
    </div>
  );
}

// 选好要发的图和文件：图是一排缩略图（点开看大图），大图还在本机压缩时那张先转圈；文件是小卡片（见 files.jsx），
// 传着、解析中转圈，读不了的标出来（原因写在输入框下面）。每个右上角去掉
function Attachments({ list, docs, onRemove, onRemoveDoc }) {
  const [viewing, setViewing] = useState(null); // {url, back}
  const pending = list.some((i) => i.pending);
  const sub = (d) => (d.status === "uploading" ? "上传中…" : d.status === "parsing" ? "解析中…" : d.status === "ready" ? fileFacts(d.file) : fileShortError(d.reason));
  return (
    // 手机上排成一行、左右滑（5 个文件加 4 张图一行行往下排，键盘弹出来时输入框会顶出屏幕）；上面留出去掉按钮探出去的那块
    <div className="mb-2 flex flex-wrap items-center gap-2 phone:-mt-2 phone:flex-nowrap phone:overflow-x-auto phone:pr-2 phone:pt-3">
      {list.map((att, k) => (
        <div key={att.key} className="relative flex size-12 flex-none items-center justify-center rounded-sm border border-line bg-side text-muted">
          {att.url
            ? <button className={styles.open} aria-label={`看第 ${k + 1} 张大图`} onClick={(e) => setViewing({ url: att.url, back: e.currentTarget })}><img src={att.url} alt={`要发的第 ${k + 1} 张图`} /></button>
            : <Spinner size={16} />}
          <button className={styles.remove} aria-label={`去掉第 ${k + 1} 张图`} onMouseDown={(e) => e.preventDefault()} onClick={() => onRemove(att.key)}><IconClose size={12} /></button>
        </div>
      ))}
      {docs.map((d) => (
        <FileCard key={d.key} name={d.name} sub={sub(d)} busy={d.status === "uploading" || d.status === "parsing"} bad={d.status === "failed"}
          onRemove={() => onRemoveDoc(d.key)} />
      ))}
      {pending && <span className="min-w-0 text-[12px] leading-[1.5] text-muted phone:flex-none phone:whitespace-nowrap" role="status">正在处理图片…</span>}
      {viewing && <Viewer img={{ alt: "要发的图" }} src={viewing.url} onClose={() => { typeNext(viewing.back); setViewing(null); }} />}
    </div>
  );
}

// Chat / Work（2026-11-10 Sean 定）：附件按钮右边的两段开关，这句话发的时候选着哪个就按哪个（见 workspace.jsx 的 workPick）。
// Work：计划、调研、写报告这类任务，模型先用问题卡问清楚再动手；Chat 不出问题卡。每句话都能换，按下去不抢光标；键盘移上去以后左右方向键换。
// 桌面悬停、键盘移上去时在上方说明两种的差别（和思考开关的说明一个样子）；手机没有悬停，不说明。
// loading：打开的对话状态还没回来（不知道它上一轮是哪种），先放一块占位
const MODE_CHAT = "Chat：直接回答";
const MODE_WORK = "Work：做计划、调研、写报告这类任务，先问清楚再动手";
export function WorkSwitch({ on, onChange, loading }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const [tip, setTip] = useState(false);
  const chat = useRef(null);
  const workBtn = useRef(null);
  useLayer(tip, () => setTip(false)); // 说明浮着时 Esc 先收说明，再按才停止回答
  if (loading) return <span className="inline-block h-7 w-[100px] flex-none rounded-full bg-hover" aria-hidden="true" />;
  const seg = (v, ref, label) => (
    <button ref={ref} role="radio" aria-checked={on === v} tabIndex={on === v ? 0 : -1} className={v ? styles.work : undefined}
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
      }}
      onFocus={(e) => { if (!mobile && e.currentTarget.matches(":focus-visible")) setTip(true); }} onBlur={() => setTip(false)}>{label}</button>
  );
  return (
    <span className="relative inline-flex flex-none" onMouseEnter={() => !mobile && setTip(true)} onMouseLeave={() => setTip(false)}>
      <span className={styles.mode} role="radiogroup" aria-label="模式" aria-describedby="ws-mode-tip">
        {seg(false, chat, "Chat")}
        {seg(true, workBtn, "Work")}
      </span>
      <span id="ws-mode-tip" role="tooltip" className={styles.tip + " " + styles.tipLeft + (tip ? " " + styles.show : "")}>
        <span className={on ? undefined : styles.cur}>{MODE_CHAT}</span>
        <span className={on ? styles.cur : undefined}>{MODE_WORK}</span>
      </span>
    </span>
  );
}

// 输入框里发送按钮左边：深度思考开关（选的模型能开关才有）和选模型。新对话随便选；对话开始后模型定下不换，
// 模型按钮点不开（2026-11-09 Sean 定：点开了又选不了，体验不好），桌面悬停、键盘移上去时在上方说一句（LOCKED_NOTE），去掉下拉箭头；
// 手机没有悬停：模型能开关思考的，点开弹层只有思考开关和这句话（思考开关在手机上只在这里），不能开关的点了浮出这句话、几秒后收起。
// 思考开关每句话都能改，按下去不抢光标（输入框里的字和光标都留着）。
// 模型分两组：可用（“自动”排第一个，只写名字，不写它现在用哪个、不加说明：2026-11-07 Sean 定）、登录后可用（游客锁着的，默认收起）。
// 每个模型前面是厂商的小图标（见 ModelIcon），输入框里的模型按钮上也有。
// 桌面是菜单：往放得下的那边开（新对话的输入框在页面中间，往下开），高度按那边剩下的空间，再多就在里面滚，第一项不会被切掉。
// 窄屏是底部弹层：最高 70dvh、里面滚，深度思考的开关和说明也在里面（输入框里只放模型名，能开关思考的名字前有个灯泡，亮着是开）。
// 菜单、弹层打开时光标在里面：Esc 只收起它们，不会停掉正在跑的这一轮（见 Composer 的 Esc）；点外面、关闭钮也能收起。
// locked：对话开始了，模型不能换。游客看到的要登录的模型（m.locked）点了去登录（onLogin），登录了还锁着的（只给管理员的）不列，见 ModelList。
// loading：模型列表还没取到、或者打开的对话状态还没回来，先放一块占位（输入框照常能打字、能发，按 Auto）
const LOCKED_NOTE = "对话开始后不能换模型，想换模型请开新对话";
export function ModelTools({ models, loading, current, locked, onPick, thinking, onThinking, onLogin }) {
  const mobile = !!useContext(NavContext)?.mobile;
  const [open, setOpen] = useState(false);
  const [tip, setTip] = useState(false); // 定下以后那句说明浮着
  const box = useRef(null);
  const btn = useRef(null);
  useLayer(tip, () => setTip(false)); // 说明浮着时 Esc 先收说明，再按才停止回答（和思考开关的说明一样）
  useEffect(() => {
    if (!tip || !mobile) return undefined;
    const t = setTimeout(() => setTip(false), 3000);
    return () => clearTimeout(t);
  }, [tip, mobile]);
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
  }, [open]);
  useLayer(open, close);
  useEffect(() => { setOpen(false); }, [mobile]); // 窗口拉宽、变窄：菜单和弹层不是一个东西
  useEffect(() => { setOpen(false); setTip(false); }, [locked]); // 开着菜单时发出了第一句、换到别的对话
  const opt = models?.find((m) => m.id === current);
  if (loading) return <span className="inline-block h-7 w-[120px] rounded-full bg-hover" aria-hidden="true" />;
  if (!models?.length || !current) return null;
  const toggle = opt?.thinking === "toggle";
  const name = opt?.name || current;
  const opens = !locked || (mobile && toggle); // 定下以后只有手机上能开关思考的还点得开（弹层里只有思考开关）
  const hint = locked && !opens;
  const list = !locked && (
    <ModelList models={models} current={current} onLogin={onLogin} onChoose={(m) => { close(); if (m.locked) onLogin?.(m); else onPick(m.id); }} />
  );
  return (
    <div className="relative flex min-w-0 items-center gap-1" ref={box}>
      {toggle && !mobile && <ThinkToggle on={thinking} onChange={onThinking} />}
      <button ref={btn} className={styles.modelBtn} aria-haspopup={opens ? (mobile ? "dialog" : "menu") : undefined} aria-expanded={opens ? open : undefined}
        aria-disabled={hint || undefined} aria-describedby={hint ? "ws-model-tip" : undefined}
        aria-label={mobile && toggle ? `${name}，深度思考${thinking ? "已开" : "已关"}` : undefined}
        title={mobile || locked ? undefined : "选模型"}
        onClick={() => (hint ? mobile && setTip(!tip) : open ? close() : setOpen(true))}
        onMouseEnter={() => hint && !mobile && setTip(true)} onMouseLeave={() => hint && !mobile && setTip(false)}
        onFocus={(e) => { if (hint && !mobile && e.currentTarget.matches(":focus-visible")) setTip(true); }} onBlur={() => hint && setTip(false)}>
        {mobile && toggle && <IconBulb size={14} className={styles.bulb + (thinking ? " " + styles.on : "")} />}
        {opt && <ModelIcon m={opt} auto={opt === models[0]} />}
        <span className="min-w-0 truncate">{name}</span>{opens && <IconChevron size={13} className={styles.caret} />}
      </button>
      {hint && <span id="ws-model-tip" role="tooltip" className={styles.tip + " " + styles.tipLine + (tip ? " " + styles.show : "")}>{LOCKED_NOTE}</span>}
      {open && !mobile && !locked && (
        <ModelMenu anchor={box} note="开始对话后不能换模型" onLeave={() => setOpen(false)}>{list}</ModelMenu>
      )}
      {open && mobile && (
        <ModelSheet title={locked ? name : "选模型"} onClose={close}>
          {toggle && <ThinkSwitch on={thinking} onChange={onThinking} />}
          <div className={menu.note}>{locked ? LOCKED_NOTE : "开始对话后不能换模型"}</div>
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

// 模型前面的厂商小图标：白底小方块（深色模式下也看得清黑色的标志），按 model 的厂商前缀对图标（public/providers，都来自 lobe-icons，MIT：
// 七个照搬 Fusion；智谱用 Z.ai 的“Z”、混元用彩色版，都是 @lobehub/icons-static-svg 1.102.0 的）；以后接了没有图标的厂商，写名字的第一个字。
// “自动”是一颗星。大小看放在哪（菜单、弹层、按钮），写在样式里；别的地方用（回答开头，见 timeline.jsx）给 size

const PROVIDER_ICONS = { deepseek: "deepseek", ark: "volcengine", moonshot: "moonshot", xai: "xai", dashscope: "qwen", mimo: "xiaomi", minimax: "minimax",
  zhipuai: "zai", hunyuan: "hunyuan" };
export function ModelIcon({ m, auto, size }) {
  const box = size ? { width: size, height: size, borderRadius: Math.round(size * 0.28), fontSize: Math.round(size * 0.5) } : undefined;
  if (auto) return <span className={styles.mark + " " + styles.auto} style={box} aria-hidden="true"><Sparkles strokeWidth={2} /></span>;
  const key = PROVIDER_ICONS[m.icon || (m.model || "").split(":")[0]]; // icon：托管在别家的（百炼上的 Kimi）按模型自己的厂商
  return <span className={styles.mark} style={box} aria-hidden="true">{key ? <img src={`/providers/${key}.svg`} alt="" /> : m.name.slice(0, 1)}</span>;
}

// 模型列表（菜单和弹层共用）：可用（“自动”第一个）/ 登录后可用（N，默认收起；正在用的在里面就展开）。只有一组时不写组名。↑↓ 在能点的之间挪。
// 锁着的只给能登录的游客看（点了开登录弹窗，说登录后就能用它）；登录了、或者没有登录功能，锁着的就是怎么都选不了，不列（第 28 条：原来列成“暂不开放”）
function ModelList({ models, current, onLogin, onChoose }) {
  const auto = models[0];
  const free = models.filter((m) => !m.locked);
  const shut = onLogin ? models.filter((m) => m.locked) : [];
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
  const row = (m, note) => (
    <button key={m.id} role="menuitemradio" aria-checked={m.id === current} className={styles.row + (m.locked ? " " + styles.locked : "")}
      disabled={m.locked && !onLogin} onClick={() => onChoose(m)}>
      <ModelIcon m={m} auto={m === auto} />
      <span className="flex min-w-0 flex-1 flex-col"><b className={styles.name}>{m.name}</b>{note && <small className={styles.desc}>{note}</small>}</span>
      {m.id === current ? <IconCheck size={15} /> : m.locked && <IconLock size={13} className={styles.lock} />}
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
      <div role="group" aria-label="可用">
        {shut.length > 0 && <div className={styles.group} aria-hidden="true">可用</div>}
        {free.map((m) => row(m, m === auto ? null : m.description))}
      </div>
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
