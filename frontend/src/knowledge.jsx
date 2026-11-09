// 知识库（2026-11-09 Sean 定，后端见 app/knowledge）：每人一个，游客那里写着登录了才有。
// 入口 11-10 起在左栏“新对话”下面（Sean：放在设置里，不点开设置不知道有），点开是自己一个窗口，和设置一样大（手机上从底部升起）。
// 传上来的文件后台解析、建索引，好了以后对话里模型自己判断查不查，查到的段落当来源（点了在右栏翻到那一段，见 workspace-files.jsx）。
// 这里能传（一次能选几个）、看每个处理到哪了、点开在右栏看、删（5 秒内能撤销，同记忆）。上限照后端给的写（limits）。
// 不讲后台怎么处理（解析、建索引都叫“处理中”），照记忆页只说能做什么
import { useEffect, useId, useRef, useState } from "react";
import { deleteKnowledgeFile, getKnowledge, getKnowledgeFile, uploadKnowledge } from "./api.js";
import { errorText } from "./errors.js";
import { Badge, fileFacts } from "./files.jsx";
import { IconArrowUp, IconClose, IconTrash, Spinner } from "./icons.jsx";
import { opener, trapTab, useLayer } from "./layers.js";
import { fileErrorText, isImage, tooBig } from "./upload.js";
import dialog from "./dialog.module.css";
import styles from "./knowledge.module.css";

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let slots = 0;

// 知识库窗口：上面标题和关闭，下面是知识库（Knowledge）。点窗口外、Esc、关闭都能关，后面的对话不动。
// 关上以后焦点回到左栏的“知识库”；手机上那一行在收起的抽屉里看不见，就回到开抽屉的钮（同搜对话）。
// onLogin：游客去登录（先关这个窗口）；undo：对话页的撤销；onOpenFile(f)：关窗口、在右栏看这个文件
export function KnowledgeDialog({ onClose, auth, onLogin, undo, onOpenFile }) {
  const box = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  const titleId = useId();
  useEffect(() => {
    const back = opener();
    box.current?.focus({ preventScroll: true });
    return () => {
      const ok = back?.isConnected && back.checkVisibility?.({ visibilityProperty: true }) !== false;
      (ok ? back : document.querySelector('[data-nav="drawer"]'))?.focus({ preventScroll: true });
    };
  }, []);
  useLayer(true, () => closing.current());
  return (
    <div className={`${dialog.overlay} ${dialog.large}`} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} className={`${dialog.dialog} ${dialog.large} ${dialog.narrow} ${dialog.side} flex flex-col`}
        role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onKeyDown={(e) => trapTab(e, box.current)}>
        <div className={styles.top}>
          <h2 id={titleId} className={styles.title}>知识库</h2>
          <button className={`${dialog.close} ${dialog.inline}`} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>
        </div>
        <div className={styles.body}>
          <Knowledge auth={auth} onLogin={onLogin} undo={undo} onOpenFile={onOpenFile} />
        </div>
      </div>
    </div>
  );
}

// onLogin：游客去登录；undo：对话页的撤销（同一条提示）；onOpenFile(f)：在右栏看这个文件（f 是 {id, name, type, size, pages}）
function Knowledge({ auth, onLogin, undo, onOpenFile }) {
  const [data, setData] = useState(null); // getKnowledge 回的
  const [error, setError] = useState(null);
  const [local, setLocal] = useState([]); // 还没传上去的、没传上去的：{key, name, size, status: uploading / failed, reason, detail}
  const [failed, setFailed] = useState(null); // 删没删成：一句话
  const input = useRef(null);
  const alive = useRef(true);
  const guest = auth?.ready && !auth.me;

  const load = () => {
    setError(null);
    getKnowledge().then(setData).catch((e) => setError(errorText("没取到知识库", e, "可以再试一次")));
  };
  useEffect(() => {
    alive.current = true;
    if (!guest) load();
    return () => { alive.current = false; undo?.flush("knowledge"); }; // 关掉窗口：删了的不等了
  }, [guest]); // eslint-disable-line react-hooks/exhaustive-deps
  const put = (fn) => setData((d) => (d ? { ...d, files: fn(d.files) } : d));
  const patch = (id, v) => put((fs) => fs.map((x) => (x.id === id ? { ...x, ...v } : x)));

  // 处理中的隔一会儿问一次（开头勤、后面慢），好了、没成就停；删掉了、关了窗口也停
  const track = async (id) => {
    for (let i = 0, errs = 0; alive.current; i++) {
      await wait(i < 10 ? 1200 : 3000);
      if (!alive.current) return;
      try {
        const v = await getKnowledgeFile(id);
        errs = 0;
        if (v.status !== "parsing") return patch(id, v);
      } catch (e) {
        if (e.status === 404) return put((fs) => fs.filter((x) => x.id !== id));
        if (++errs >= 20) return;
      }
    }
  };
  // 打开时还在处理的（上次传的、别的设备传的）也接着问
  const tracked = useRef(new Set());
  useEffect(() => {
    for (const f of data?.files || []) {
      if (f.status === "parsing" && !tracked.current.has(f.id)) {
        tracked.current.add(f.id);
        track(f.id);
      }
    }
  });

  const upload = async (key, file) => {
    try {
      const v = await uploadKnowledge(file);
      if (!alive.current) return;
      setLocal((ls) => ls.filter((x) => x.key !== key));
      put((fs) => [...fs, { ...v, created_at: new Date().toISOString() }]);
    } catch (e) {
      let reason = e.detail?.reason || (!e.status ? "offline" : e.status === 413 ? "too_large" : e.status === 429 || e.status >= 500 ? "busy" : "failed");
      if (reason === "image") reason = "kb_image"; // 对话里的说法是“作为图片发”，知识库里没这回事
      setLocal((ls) => ls.map((x) => (x.key === key ? { ...x, status: "failed", reason, detail: e.detail || undefined } : x)));
    }
  };
  // 选好的文件：明显传不了的（太大、空的、图片）直接标出来，别的一个个传（一起传后端也是排着解析）
  const pick = async (files) => {
    const add = files.map((f) => {
      const bad = tooBig(f) || (isImage(f) ? "kb_image" : null);
      return { key: ++slots, name: f.name || "未命名文件", size: f.size, src: f, status: bad ? "failed" : "uploading", reason: bad };
    });
    setLocal((ls) => [...add.map(({ src, ...d }) => d), ...ls]);
    for (const d of add) if (d.status === "uploading") await upload(d.key, d.src);
  };

  const remove = (f) => {
    setFailed(null);
    const at = data.files.findIndex((x) => x.id === f.id);
    put((fs) => fs.filter((x) => x.id !== f.id));
    const back = () => put((fs) => (fs.some((x) => x.id === f.id) ? fs : [...fs.slice(0, at), f, ...fs.slice(at)]));
    undo.push({
      kind: "knowledge",
      text: `已从知识库删除“${f.name}”`,
      commit: ({ keepalive }) => deleteKnowledgeFile(f.id, { keepalive }).catch((e) => {
        back();
        setFailed(errorText("那个文件没删成", e));
      }),
      undo: back,
    });
  };

  const files = data ? [...data.files].reverse() : []; // 后传的在上面
  const n = data?.files.length ?? 0;
  const limits = data?.limits;
  const full = limits && n >= limits.files;
  const busy = local.some((x) => x.status === "uploading");
  return (
    <div className={styles.kb}>
      <div className={styles.head}>
        {guest ? <b>登录后才能用知识库</b> : data && <b>{n ? `${n} 个文件` : "还没有文件"}</b>}
        <p>
          把常用的资料放进来，灵稿回答时会按需查找，引用的地方标出出处。只有你自己看得到。
          {limits && `最多 ${limits.files} 个文件，每个不超过 ${limits.mb} MB。`}
        </p>
        {guest ? onLogin && <button className="ws-btn sm" onClick={onLogin}>登录</button> : data && (
          <>
            <button className="ws-btn primary sm" disabled={full} onClick={() => input.current?.click()} aria-busy={busy || undefined}>
              {busy ? <Spinner size={13} /> : <IconArrowUp size={14} />}上传文件
            </button>
            {full && <span className={styles.full}>已经放满了，删掉一些再传</span>}
            <input ref={input} type="file" multiple hidden onChange={(e) => { pick([...e.target.files]); e.target.value = ""; }} />
          </>
        )}
      </div>
      {error && (
        <div className={styles.status}>
          {error} <button className="ws-btn sm" onClick={load}>再试一次</button>
        </div>
      )}
      {!guest && !error && !data && <div className={styles.status}>正在取知识库…</div>}
      {failed && <div className="ws-error" role="alert">{failed}</div>}
      {!guest && (local.length > 0 || files.length > 0) && (
        <ul className={styles.list} aria-label="知识库里的文件">
          {local.map((d) => (
            <li key={d.key} className={styles.item}>
              <span className={styles.main}>
                {d.status === "uploading" ? <span className={styles.spin}><Spinner size={14} /></span> : <Badge name={d.name} />}
                <span className={styles.text}>
                  <span className={styles.name}>{d.name}</span>
                  <span className={styles.sub + (d.status === "failed" ? " " + styles.bad : "")}>{d.status === "uploading" ? "上传中…" : fileErrorText(d.reason, d.detail)}</span>
                </span>
              </span>
              {d.status === "failed" && (
                <button className="ws-icon-btn" aria-label={`去掉 ${d.name}`} title="去掉" onClick={() => setLocal((ls) => ls.filter((x) => x.key !== d.key))}><IconClose size={15} /></button>
              )}
            </li>
          ))}
          {files.map((f) => <FileRow key={f.id} f={f} onOpen={onOpenFile} onDelete={() => remove(f)} />)}
        </ul>
      )}
    </div>
  );
}

// 一个文件：好了的点开在右栏看；处理中的转圈；没成的写原因。右边删
function FileRow({ f, onOpen, onDelete }) {
  const ready = f.status === "ready";
  const sub = ready ? fileFacts(f.file) : f.status === "parsing" ? "处理中…" : fileErrorText(f.reason);
  const body = (
    <>
      {f.status === "parsing" ? <span className={styles.spin}><Spinner size={14} /></span> : <Badge name={f.name} />}
      <span className={styles.text}>
        <span className={styles.name}>{f.name}</span>
        <span className={styles.sub + (f.status === "failed" ? " " + styles.bad : "")}>{sub}</span>
      </span>
    </>
  );
  return (
    <li className={styles.item}>
      {ready ? <button className={styles.main} aria-label={`打开 ${f.name}`} title={f.name} onClick={() => onOpen(f.file)}>{body}</button>
        : <span className={styles.main} title={f.name}>{body}</span>}
      <button className="ws-icon-btn danger" aria-label={`从知识库删除 ${f.name}`} title="删" onClick={onDelete}><IconTrash size={15} /></button>
    </li>
  );
}
