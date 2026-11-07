// 记忆页：灵稿每天在后台把对话整理成关于用户的记忆（见后端 graph/research/memory.py），回答时放进系统提示词。
// 记忆按账号分（2026-11-06）：游客的对话不整理，登录后才有。
// 能改、能删（评审第 15 条，2026-11-07 Sean 定，推翻 11-03 的“只能看”）：改就在那一条上改（最多 200 字）；删了 5 秒内能撤销（见 undo.jsx），
// 页面上写明删了以后不再参考、但之后的对话里再说起可能又记下。每条写着从哪段对话记下的，点了打开那段对话。不能手动新加
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { deleteMemory, editMemory, getMemory } from "./api.js";
import { errorText } from "./errors.js";
import { IconEdit, IconTrash } from "./icons.jsx";
import { HeadBar } from "./nav.jsx";
import { sayWhen, stamp } from "./research.js";

const TEXT_MAX = 200; // 后端 memory.TEXT_MAX：超过了 400
const tidy = (s) => s.replace(/\s+/g, " ").trim(); // 后端存之前也这样收拾

// 上次整理：只写时间。后端记的看了几段、新记几条是那次整理所有人合起来的数，放在个人页面上会被当成自己的，不写（2026-11-07 Sean 定，待确认第 25 条）
function lastText(d, now) {
  if (!d) return "还没整理过。";
  const s = stamp(d.started_at, now);
  const at = s ? sayWhen(s) : "";
  return d.ended_at ? `${at} 整理过。` : `${at} 开始的那次整理没有做完，下次接着整理。`;
}

// undo：对话页的撤销（同一条提示，见 undo.jsx）；threads：对话列表（对出每条记忆是从哪段对话记下的）；onOpen(id)：打开那段对话
export function MemoryPage({ auth, onLogin, undo, threads, onOpen }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null); // 正在改的那一条的 id
  const [failed, setFailed] = useState(null); // 删没删成：一句话
  const list = useRef(null);
  const load = () => {
    setError(null);
    getMemory().then(setData).catch((e) => setError(errorText("没取到记忆", e, "可以再试一次")));
  };
  useEffect(load, []);
  useEffect(() => () => undo?.flush("memory"), []); // eslint-disable-line react-hooks/exhaustive-deps -- 离开记忆页：删了的不等了
  const now = Date.now();
  const next = data?.next_dream ? stamp(data.next_dream, now) : null;
  const guest = auth?.ready && !auth.me; // 游客：没有记忆，说明登录了才有
  const put = (fn) => setData((d) => (d ? { ...d, memories: fn(d.memories) } : d));

  const remove = (m) => {
    // 焦点放到下一条（没有就上一条）的“改”上：删的按钮跟着这一条没了
    const btns = [...(list.current?.querySelectorAll(".mem-edit-btn") || [])];
    const i = btns.findIndex((b) => b.dataset.id === m.id);
    const at = data.memories.findIndex((x) => x.id === m.id);
    (btns[i + 1] || btns[i - 1])?.focus();
    setFailed(null);
    put((ms) => ms.filter((x) => x.id !== m.id));
    const back = () => put((ms) => (ms.some((x) => x.id === m.id) ? ms : [...ms.slice(0, at), m, ...ms.slice(at)]));
    undo.push({
      kind: "memory",
      text: "已删除一条记忆",
      commit: ({ keepalive }) => deleteMemory(m.id, { keepalive }).catch((e) => {
        back();
        setFailed(errorText("那条记忆没删成", e));
      }),
      undo: back,
    });
  };

  return (
    <section className="ws-main" aria-label="记忆">
      <HeadBar title="记忆" />
      <div className="ws-scroll">
        <div className="ws-flow mem">
          <p className="mem-intro">
            灵稿每天{next ? ` ${next.hm} ` : ""}在后台把有新内容的对话整理一遍，记下关于你的事，之后回答时会参考。记忆按账号分，只有你自己看得到。
          </p>
          <p className="mem-intro sub">
            记得不对的可以改，不想让它记的可以删。删掉的以后回答时不再参考，但之后的对话里再说起，整理时可能又会记下。
          </p>
          {guest && (
            <div className="mem-status">
              没登录的对话不整理，登录后才有记忆。{onLogin && <button className="ws-btn sm" onClick={onLogin}>登录</button>}
            </div>
          )}
          {error && (
            <div className="mem-status">
              {error} <button className="ws-btn sm" onClick={load}>再试一次</button>
            </div>
          )}
          {!error && !data && <div className="mem-status">正在取记忆…</div>}
          {data && (
            <>
              <div className="mem-status">
                {lastText(data.last_dream, now)}
                {next ? ` 下次整理：${sayWhen(next)}。` : " 按时整理没有开。"}
              </div>
              {failed && <div className="ws-error" role="alert">{failed}</div>}
              {data.memories.length === 0 ? <div className="mem-empty">{guest ? "登录后这里会有记忆。" : "还没有记忆。"}</div> : (
                <ul className="mem-list" ref={list}>
                  {data.memories.map((m) => (
                    <MemoryItem key={m.id} m={m} now={now} threads={threads} onOpen={onOpen} editing={editing === m.id}
                      onEdit={() => setEditing(m.id)} onDone={() => setEditing(null)}
                      onSaved={(row) => put((ms) => ms.map((x) => (x.id === m.id ? { ...x, ...row } : x)))}
                      onDelete={() => remove(m)} />
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function MemoryItem({ m, now, threads, onOpen, editing, onEdit, onDone, onSaved, onDelete }) {
  const changed = m.updated_at && m.updated_at !== m.created_at;
  const t = stamp(changed ? m.updated_at : m.created_at, now);
  // 出处：对得上列表里的对话就能点开；对话已经删了就照实说；很早记下的没有出处，不写
  const from = m.thread_id && threads?.find((x) => x.thread_id === m.thread_id);
  const source = !m.thread_id || !threads ? null : from ? (
    <a className="mem-from" href={`/chat/${m.thread_id}`} title={from.title}
      onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); onOpen(m.thread_id); }}>
      来自“{from.title}”
    </a>
  ) : <span className="mem-from gone">来自的对话已删除</span>;
  if (editing) return <li className="editing"><MemoryEdit m={m} onDone={onDone} onSaved={onSaved} /></li>;
  return (
    <li>
      <span className="text">{m.text}</span>
      <span className="mem-foot">
        <span className="meta" title={t?.full}>{t ? `${t.day || "今天"}${changed ? "改过" : "记下"}` : ""}</span>
        {source}
        <span className="mem-acts">
          <button className="ws-icon-btn mem-edit-btn" data-id={m.id} aria-label="改这条记忆" title="改" onClick={onEdit}><IconEdit size={15} /></button>
          <button className="ws-icon-btn danger" aria-label="删掉这条记忆" title="删" onClick={onDelete}><IconTrash size={15} /></button>
        </span>
      </span>
    </li>
  );
}

// 改一条：回车保存（记忆是一句话，换行存的时候也会变成空格），Esc 不改；右下角写着字数和上限
function MemoryEdit({ m, onDone, onSaved }) {
  const [v, setV] = useState(m.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef(null);
  useLayoutEffect(() => {
    const el = ref.current;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [v]);
  useEffect(() => {
    const el = ref.current;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  const text = tidy(v);
  const back = () => requestAnimationFrame(() => document.querySelector(`.mem-edit-btn[data-id="${CSS.escape(m.id)}"]`)?.focus());
  const cancel = () => { onDone(); back(); };
  const save = async () => {
    if (busy || !text) return;
    if (text === m.text) return cancel();
    setBusy(true);
    setError(null);
    try {
      onSaved(await editMemory(m.id, text));
      onDone();
      back();
    } catch (e) {
      setBusy(false);
      setError(errorText("没改成", e));
    }
  };
  return (
    <div className="mem-edit">
      <textarea ref={ref} value={v} data-esc maxLength={TEXT_MAX} rows={1} aria-label="记忆内容" disabled={busy}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
          else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        }} />
      {error && <div className="mem-edit-error" role="alert">{error}</div>}
      <div className="mem-edit-bar">
        <span className={"ws-count" + (v.length >= TEXT_MAX ? " full" : "")}>{v.length}/{TEXT_MAX}</span>
        <button className="ws-btn ghost sm" disabled={busy} onClick={cancel}>取消</button>
        <button className="ws-btn primary sm" disabled={busy || !text} onClick={save}>保存</button>
      </div>
    </div>
  );
}
