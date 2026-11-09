// 资料库页（/library）。2026-11-09 先做了“知识库”，在设置里；11-10 Sean 定：照 WorkBuddy 做成自己一页、叫资料库，照 Fusion 一个人能建几个，
// 几个都自动能查（方案 A，不在输入框里选）。后端见 app/knowledge。
// 左边是资料库列表和“新建资料库”，右边是选中的那个：名字、说明、⋯（修改名字和说明、删除），上传（也能拖进来），文件一个一行（处理中、能用、没成）。
// 新建只问名字和说明（说明选填，模型拿它判断查哪个）。删资料库连里面的文件一起删，删文件也一样，5 秒内能撤销（同删对话）。
// 点能用的文件在右栏看（KnowledgePanel，见 workspace-files.jsx）。页面窄（手机、桌面上开着右栏）时一次只放一边：先是列表，点进去一个，左上角“资料库”回到列表。
// 游客那里写着登录了才有。不讲后台怎么处理（解析、建索引都叫“处理中”），只说能做什么
import { Plus } from "lucide-react";
import { useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createLibrary, deleteKnowledgeFile, deleteLibrary, getKnowledge, getKnowledgeFile, updateLibrary, uploadKnowledge } from "./api.js";
import { errorText } from "./errors.js";
import { Badge, fileFacts } from "./files.jsx";
import { IconArrowUp, IconChevron, IconClose, IconEdit, IconFolder, IconMore, IconTrash, Spinner } from "./icons.jsx";
import { opener, trapTab, useLayer } from "./layers.js";
import { ActionMenu } from "./menu.jsx";
import { HeadBar, NavContext } from "./nav.jsx";
import { fileErrorText, isImage, tooBig } from "./upload.js";
import dialog from "./dialog.module.css";
import styles from "./knowledge.module.css";

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const cx = (...names) => names.filter(Boolean).join(" ");
const NARROW = 640; // 页面比这窄：列表、详情一次只放一边
const ABOUT = "把常用的资料放进来，灵稿回答时会按需查找，引用的地方标出出处。只有你自己看得到。";
let slots = 0;
let lastLib = null; // 上次看的是哪个资料库：去对话再回来（左栏、后退）还停在它上面

// onLogin：游客去登录；undo：对话页的撤销（同一条提示）；onOpenFile(f)：在右栏看这个文件（f 是 {id, name, type, size, pages}）；
// openFileId：右栏正开着的那个（那一行标出来）
export function LibraryPage({ auth, onLogin, undo, onOpenFile, openFileId }) {
  const nav = useContext(NavContext);
  const [data, setData] = useState(null); // getKnowledge 回的
  const [error, setError] = useState(null);
  const [sel, setSel] = useState(() => lastLib); // 选中的资料库；窄的时候 null 是列表
  const [local, setLocal] = useState([]); // 还没传上去的、没传上去的：{key, lib, name, size, status: uploading / failed, reason, detail}
  const [failed, setFailed] = useState(null); // 删没删成：一句话
  const [form, setForm] = useState(null); // 新建资料库是 {}，修改是 {lib}
  const [menu, setMenu] = useState(null); // ⋯ 开着时是那个按钮
  const [gone, setGone] = useState(() => new Set()); // 删了、还在等撤销的资料库：不显示
  const [narrow, setNarrow] = useState(!!nav?.mobile);
  const [dropping, setDropping] = useState(false);
  const page = useRef(null);
  const input = useRef(null);
  const head = useRef(null);
  const alive = useRef(true);
  const narrowRef = useRef(narrow);
  narrowRef.current = narrow;
  const guest = auth?.ready && !auth.me;

  useEffect(() => {
    lastLib = sel;
  }, [sel]);
  useLayoutEffect(() => {
    const el = page.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setNarrow(el.clientWidth < NARROW));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const load = () => {
    setError(null);
    getKnowledge().then((d) => {
      setData(d);
      setSel((s) => s ?? (narrowRef.current ? null : d.libraries[0]?.id ?? null)); // 宽的时候先选第一个
    }).catch((e) => setError(errorText("没取到资料库", e, "可以再试一次")));
  };
  useEffect(() => {
    alive.current = true;
    if (!guest) load();
    return () => { alive.current = false; undo?.flush("knowledge"); undo?.flush("library"); }; // 离开这一页：删了的不等了
  }, [guest]); // eslint-disable-line react-hooks/exhaustive-deps

  const libs = data ? data.libraries.filter((l) => !gone.has(l.id)) : [];
  const cur = libs.find((l) => l.id === sel) || (narrow ? null : libs[0]) || null;
  const mapLibs = (fn) => setData((d) => (d ? { ...d, libraries: d.libraries.map(fn) } : d));
  const putFiles = (libId, fn) => mapLibs((l) => (l.id === libId ? { ...l, files: fn(l.files) } : l));
  const patchFile = (id, v) => mapLibs((l) => (l.files.some((x) => x.id === id) ? { ...l, files: l.files.map((x) => (x.id === id ? { ...x, ...v } : x)) } : l));
  const dropFile = (id) => mapLibs((l) => ({ ...l, files: l.files.filter((x) => x.id !== id) }));

  // 窄的时候换了一边：进去焦点落在资料库名字上（读屏念出来），回到列表落在刚才那一个上
  const last = useRef(null);
  const shown = narrow ? cur?.id ?? null : null;
  useEffect(() => {
    if (!narrow) return;
    if (shown) {
      last.current = shown;
      head.current?.focus({ preventScroll: true });
    } else if (last.current) page.current?.querySelector(`[data-lib="${CSS.escape(last.current)}"]`)?.focus({ preventScroll: true });
  }, [shown]); // eslint-disable-line react-hooks/exhaustive-deps

  // 处理中的隔一会儿问一次（开头勤、后面慢），好了、没成就停；删掉了、离开这一页也停
  const track = async (id) => {
    for (let i = 0, errs = 0; alive.current; i++) {
      await wait(i < 10 ? 1200 : 3000);
      if (!alive.current) return;
      try {
        const v = await getKnowledgeFile(id);
        errs = 0;
        if (v.status !== "parsing") return patchFile(id, v);
      } catch (e) {
        if (e.status === 404) return dropFile(id);
        if (++errs >= 20) return;
      }
    }
  };
  // 打开时还在处理的（上次传的、别的设备传的）也接着问
  const tracked = useRef(new Set());
  useEffect(() => {
    for (const l of data?.libraries || []) {
      for (const f of l.files) {
        if (f.status === "parsing" && !tracked.current.has(f.id)) {
          tracked.current.add(f.id);
          track(f.id);
        }
      }
    }
  });

  const upload = async (key, libId, file) => {
    try {
      const v = await uploadKnowledge(libId, file);
      if (!alive.current) return;
      setLocal((ls) => ls.filter((x) => x.key !== key));
      putFiles(libId, (fs) => [...fs, { ...v, created_at: new Date().toISOString() }]);
    } catch (e) {
      let reason = e.detail?.reason || (!e.status ? "offline" : e.status === 413 ? "too_large" : e.status === 429 || e.status >= 500 ? "busy" : "failed");
      if (reason === "image") reason = "kb_image"; // 对话里的说法是“作为图片发”，资料库里没这回事
      setLocal((ls) => ls.map((x) => (x.key === key ? { ...x, status: "failed", reason, detail: e.detail || undefined } : x)));
    }
  };
  // 选好的、拖进来的文件：明显传不了的（太大、空的、图片）直接标出来，别的一个个传（一起传后端也是排着解析）
  const pick = async (files, libId) => {
    const add = files.map((f) => {
      const bad = tooBig(f) || (isImage(f) ? "kb_image" : null);
      return { key: ++slots, lib: libId, name: f.name || "未命名文件", size: f.size, src: f, status: bad ? "failed" : "uploading", reason: bad };
    });
    setLocal((ls) => [...add.map(({ src, ...d }) => d), ...ls]);
    for (const d of add) if (d.status === "uploading") await upload(d.key, libId, d.src);
  };

  const removeFile = (f, libId) => {
    setFailed(null);
    const at = data.libraries.find((l) => l.id === libId)?.files.findIndex((x) => x.id === f.id) ?? -1;
    dropFile(f.id);
    const back = () => putFiles(libId, (fs) => (fs.some((x) => x.id === f.id) ? fs : [...fs.slice(0, at), f, ...fs.slice(at)]));
    undo.push({
      kind: "knowledge",
      text: `已从资料库删除“${f.name}”`,
      commit: ({ keepalive }) => deleteKnowledgeFile(f.id, { keepalive }).catch((e) => {
        back();
        setFailed(errorText("那个文件没删成", e));
      }),
      undo: back,
    });
  };

  // 删资料库：先从列表里拿掉（等撤销），选中挪到下一个（窄的时候回到列表）
  const removeLibrary = (lib) => {
    setFailed(null);
    const i = libs.findIndex((l) => l.id === lib.id);
    const next = libs[i + 1] || libs[i - 1];
    const hide = (on) => setGone((g) => {
      const n = new Set(g);
      if (on) n.add(lib.id); else n.delete(lib.id);
      return n;
    });
    hide(true);
    setSel(narrow ? null : next?.id ?? null);
    last.current = null;
    requestAnimationFrame(() => page.current?.querySelector(next ? `[data-lib="${CSS.escape(next.id)}"]` : "[data-new-lib]")?.focus({ preventScroll: true }));
    const back = () => {
      hide(false);
      setSel(lib.id);
    };
    const n = lib.files.length;
    undo.push({
      kind: "library",
      text: `已删除资料库“${lib.name}”${n ? `（${n} 个文件）` : ""}`,
      commit: ({ keepalive }) => deleteLibrary(lib.id, { keepalive }).then(() => {
        setData((d) => (d ? { ...d, libraries: d.libraries.filter((l) => l.id !== lib.id) } : d));
        hide(false);
      }).catch((e) => {
        back();
        setFailed(errorText("那个资料库没删成", e));
      }),
      undo: back,
    });
  };

  const saved = (lib) => {
    if (form?.lib) mapLibs((l) => (l.id === lib.id ? { ...l, ...lib } : l));
    else {
      setData((d) => (d ? { ...d, libraries: [...d.libraries, { ...lib, files: [] }] } : d));
      setSel(lib.id);
    }
    setForm(null);
  };

  const limits = data?.limits;
  const n = libs.reduce((a, l) => a + l.files.length, 0); // 文件个数按人算（几个资料库加起来）
  const full = limits && n >= limits.files;
  const manyLibs = limits && libs.length >= limits.libraries;
  const busy = local.some((x) => x.status === "uploading" && x.lib === cur?.id);
  const newButton = (
    <button className={cx("ws-btn", styles.newLib)} data-new-lib disabled={manyLibs} title={manyLibs ? `最多建 ${limits.libraries} 个资料库` : undefined}
      onClick={() => setForm({})}>
      <Plus size={16} strokeWidth={1.8} />新建资料库
    </button>
  );

  let body;
  if (guest) {
    body = (
      <div className={styles.empty}>
        <IconFolder size={28} className={styles.emptyIcon} />
        <b>登录后才能用资料库</b>
        <p>{ABOUT}</p>
        {onLogin && <button className="ws-btn primary" onClick={onLogin}>登录</button>}
      </div>
    );
  } else if (error) {
    body = <div className={styles.status}>{error} <button className="ws-btn sm" onClick={load}>再试一次</button></div>;
  } else if (!data) {
    body = <div className={styles.status} role="status"><Spinner size={14} />正在取资料库…</div>;
  } else if (!libs.length) {
    body = (
      <div className={styles.empty}>
        {failed && <div className="ws-error" role="alert">{failed}</div>}
        <IconFolder size={28} className={styles.emptyIcon} />
        <b>还没有资料库</b>
        <p>{ABOUT}可以按用途分开放，比如工作、学习、旅行。</p>
        {newButton}
      </div>
    );
  } else {
    const files = cur ? [...cur.files].reverse() : []; // 后传的在上面
    const mine = cur ? local.filter((x) => x.lib === cur.id) : [];
    body = (
      <div className={cx(styles.wrap, narrow && styles.narrow)}>
        {(!narrow || !cur) && (
          <nav className={styles.libs} aria-label="资料库列表">
            {newButton}
            <div className={styles.libList}>
              {libs.map((l) => {
                const working = l.files.some((f) => f.status === "parsing") || local.some((x) => x.lib === l.id && x.status === "uploading");
                return (
                  <button key={l.id} data-lib={l.id} className={styles.lib} aria-current={!narrow && cur?.id === l.id ? "page" : undefined} onClick={() => setSel(l.id)}>
                    <IconFolder size={16} className={styles.libIcon} />
                    <span className={styles.libText}>
                      <span className={styles.libName}>{l.name}</span>
                      <span className={styles.libSub}>{l.files.length ? `${l.files.length} 个文件` : "还没有文件"}</span>
                    </span>
                    {working && <Spinner size={13} />}
                    {narrow && <IconChevron size={16} className={styles.libChev} />}
                  </button>
                );
              })}
            </div>
          </nav>
        )}
        {cur && (
          <section className={cx(styles.detail, dropping && styles.dropping)} aria-labelledby={`lib-${cur.id}`}
            onDragOver={(e) => {
              if (!e.dataTransfer.types.includes("Files")) return;
              e.preventDefault();
              setDropping(true);
            }}
            onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDropping(false); }}
            onDrop={(e) => {
              if (!e.dataTransfer.files.length) return;
              e.preventDefault();
              setDropping(false);
              pick([...e.dataTransfer.files], cur.id);
            }}>
            <div className={styles.dhead}>
              {narrow && (
                <button className={cx("ws-btn ghost sm", styles.back)} aria-label="返回资料库列表" onClick={() => setSel(null)}>
                  <IconChevron size={14} className={styles.backIcon} />资料库
                </button>
              )}
              <h2 id={`lib-${cur.id}`} ref={head} tabIndex={-1} className={styles.dtitle} title={cur.name}>{cur.name}</h2>
              <button className="ws-icon-btn" aria-label={`“${cur.name}”的更多操作`} aria-haspopup={nav?.mobile ? "dialog" : "menu"} aria-expanded={!!menu}
                onClick={(e) => setMenu(menu ? null : e.currentTarget)}>
                <IconMore size={16} />
              </button>
            </div>
            {cur.description && <p className={styles.desc}>{cur.description}</p>}
            <div className={styles.actions}>
              <button className="ws-btn primary" disabled={full} onClick={() => input.current?.click()} aria-busy={busy || undefined}>
                {busy ? <Spinner size={13} /> : <IconArrowUp size={14} />}上传文件
              </button>
              <span className={styles.note}>
                {full ? "文件已经放满了，删掉一些再传" : `每个文件不超过 ${limits.mb} MB，所有资料库加起来最多 ${limits.files} 个`}
              </span>
              <input ref={input} type="file" multiple hidden onChange={(e) => { pick([...e.target.files], cur.id); e.target.value = ""; }} />
            </div>
            {failed && <div className="ws-error" role="alert">{failed}</div>}
            {mine.length > 0 || files.length > 0 ? (
              <ul className={styles.list} aria-label={`“${cur.name}”里的文件`}>
                {mine.map((d) => (
                  <li key={d.key} className={styles.item}>
                    <span className={styles.main}>
                      {d.status === "uploading" ? <span className={styles.spin}><Spinner size={14} /></span> : <Badge name={d.name} />}
                      <span className={styles.text}>
                        <span className={styles.name}>{d.name}</span>
                        <span className={cx(styles.sub, d.status === "failed" && styles.bad)}>{d.status === "uploading" ? "上传中…" : fileErrorText(d.reason, d.detail)}</span>
                      </span>
                    </span>
                    {d.status === "failed" && (
                      <button className="ws-icon-btn" aria-label={`去掉 ${d.name}`} title="去掉" onClick={() => setLocal((ls) => ls.filter((x) => x.key !== d.key))}><IconClose size={15} /></button>
                    )}
                  </li>
                ))}
                {files.map((f) => <FileRow key={f.id} f={f} on={f.id === openFileId} onOpen={onOpenFile} onDelete={() => removeFile(f, cur.id)} />)}
              </ul>
            ) : (
              <div className={styles.none}>
                <b>还没有文件</b>
                <p>{ABOUT}{nav?.mobile ? "" : "也可以把文件拖到这里。"}</p>
              </div>
            )}
          </section>
        )}
      </div>
    );
  }

  return (
    <section ref={page} className={styles.page} aria-label="资料库">
      <HeadBar title="资料库" />
      <div className={styles.scroll}>{body}</div>
      {menu && cur && (
        <ActionMenu label={`“${cur.name}”的更多操作`} anchor={menu} mobile={nav?.mobile} onClose={() => setMenu(null)} items={[
          { key: "edit", icon: <IconEdit size={15} />, text: "修改名字和说明", onClick: () => { setMenu(null); setForm({ lib: cur }); } },
          { key: "delete", icon: <IconTrash size={15} />, text: "删除资料库", danger: true, onClick: () => { setMenu(null); removeLibrary(cur); } },
        ]} />
      )}
      {form && limits && <LibraryForm lib={form.lib} limits={limits} onClose={() => setForm(null)} onSaved={saved} />}
    </section>
  );
}

// 一个文件：好了的点开在右栏看（on：右栏正开着它）；处理中的转圈；没成的写原因。右边删
function FileRow({ f, on, onOpen, onDelete }) {
  const ready = f.status === "ready";
  const sub = ready ? fileFacts(f.file) : f.status === "parsing" ? "处理中…" : fileErrorText(f.reason);
  const body = (
    <>
      {f.status === "parsing" ? <span className={styles.spin}><Spinner size={14} /></span> : <Badge name={f.name} />}
      <span className={styles.text}>
        <span className={styles.name}>{f.name}</span>
        <span className={cx(styles.sub, f.status === "failed" && styles.bad)}>{sub}</span>
      </span>
    </>
  );
  return (
    <li className={cx(styles.item, on && styles.on)}>
      {ready ? <button className={styles.main} aria-label={`打开 ${f.name}`} title={f.name} onClick={() => onOpen(f.file)}>{body}</button>
        : <span className={styles.main} title={f.name}>{body}</span>}
      <button className="ws-icon-btn danger" aria-label={`从资料库删除 ${f.name}`} title="删" onClick={onDelete}><IconTrash size={15} /></button>
    </li>
  );
}

// 建、改资料库没成：后端给原因（见 app/knowledge/service.py 的 LibraryRejected），说人话
function libraryErrorText(e, editing) {
  const d = e?.detail;
  if (d?.code === "library_rejected") {
    if (d.reason === "name_taken") return "已经有叫这个名字的资料库了，换一个名字";
    if (d.reason === "libraries_full") return `最多建 ${d.limit} 个资料库，删掉一些再建`;
    if (d.reason === "bad_name") return `名字最多 ${d.limit} 个字`;
    if (d.reason === "bad_description") return `说明最多 ${d.limit} 个字`;
    if (d.reason === "login_required") return "登录后才能用资料库";
  }
  if (e?.status === 404) return "这个资料库已经删掉了";
  return errorText(editing ? "没改成" : "没建成", e, "可以再试一次");
}

// 新建、修改资料库的小窗口：名字（必填）、说明（选填）。回车（在名字框里）或点按钮保存；点窗口外、Esc、取消都不存
function LibraryForm({ lib, limits, onClose, onSaved }) {
  const [name, setName] = useState(lib?.name || "");
  const [desc, setDesc] = useState(lib?.description || "");
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const box = useRef(null);
  const nameRef = useRef(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  const titleId = useId();
  const nameId = useId();
  const descId = useId();
  const errId = useId();
  useEffect(() => {
    const back = opener();
    nameRef.current?.focus();
    return () => { if (back?.isConnected) back.focus({ preventScroll: true }); };
  }, []);
  useLayer(true, () => closing.current());
  const submit = async (e) => {
    e.preventDefault();
    const v = { name: name.trim(), description: desc.trim() };
    if (!v.name || saving) return;
    setSaving(true);
    setErr(null);
    try {
      onSaved(lib ? await updateLibrary(lib.id, v) : await createLibrary(v));
    } catch (x) {
      setSaving(false);
      setErr(libraryErrorText(x, !!lib));
      nameRef.current?.focus();
    }
  };
  return (
    <div className={dialog.overlay} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form ref={box} className={cx(dialog.dialog, styles.form)} role="dialog" aria-modal="true" aria-labelledby={titleId} onSubmit={submit}
        onKeyDown={(e) => trapTab(e, box.current)}>
        <button type="button" className={dialog.close} aria-label="关闭" onClick={onClose}><IconClose size={16} /></button>
        <h2 id={titleId} className={styles.formTitle}>{lib ? "修改资料库" : "新建资料库"}</h2>
        <div className={styles.fieldHead}>
          <label htmlFor={nameId}>名字</label>
          <span className={cx("ws-count", name.length >= limits.name && "full")}>{name.length}/{limits.name}</span>
        </div>
        <input id={nameId} ref={nameRef} className={styles.field} value={name} maxLength={limits.name} autoComplete="off" placeholder="比如：工作合同"
          aria-invalid={err ? true : undefined} aria-describedby={err ? errId : undefined} onChange={(e) => setName(e.target.value)} />
        <div className={styles.fieldHead}>
          <label htmlFor={descId}>说明<span className={styles.optional}>（选填）</span></label>
          <span className={cx("ws-count", desc.length >= limits.description && "full")}>{desc.length}/{limits.description}</span>
        </div>
        <textarea id={descId} className={cx(styles.field, styles.area)} value={desc} maxLength={limits.description} rows={3}
          placeholder="放的是什么，比如：公司的合同、报价单和付款记录" onChange={(e) => setDesc(e.target.value)} />
        {err && <div id={errId} className="ws-error" role="alert">{err}</div>}
        <div className={styles.formActions}>
          <button type="button" className="ws-btn" onClick={onClose}>取消</button>
          <button type="submit" className="ws-btn primary" disabled={!name.trim() || saving} aria-busy={saving || undefined}>
            {saving && <Spinner size={13} />}{lib ? "保存" : "创建"}
          </button>
        </div>
      </form>
    </div>
  );
}
