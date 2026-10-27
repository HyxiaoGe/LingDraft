import React, { useState } from "react";
import { createThread, resumeThread } from "./api.js";

const STATUS_LABEL = { draft: "草稿", confirmed: "已确认", user_edited: "手工编辑" };
const KIND_LABEL = {
  headline: "标题", subhead: "副标题", offer: "优惠", date: "活动时间", cta: "行动号召",
  product_image: "产品图", background: "背景", logo: "Logo", decor: "装饰",
};

export default function App() {
  const [state, setState] = useState(null); // 后端 state_view
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const onEvent = (ev, data) => {
    if (ev === "error") setError(data.message);
    if (ev === "node") {
      const bits = [data.node];
      if (data.version) bits.push(`v${data.version} ${data.note}`);
      if (data.changed && data.changed.length) bits.push(`改动: ${data.changed.join(",")}`);
      if (data.notes) bits.push(data.notes.join("；"));
      if (data.checks) bits.push(data.checks.map((c) => `${c.name}${c.passed ? "✓" : "✗"}`).join(" "));
      setLog((l) => [...l, bits.join("  ")]);
    }
    if (ev === "interrupt") setLog((l) => [...l, `⏸ 等待输入: ${data.type}`]);
  };

  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      const st = await fn(onEvent);
      if (st) setState(st);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const start = (request, image) => run((cb) => createThread(request, image, cb));
  const resume = (payload) => run((cb) => resumeThread(state.thread_id, payload, cb));

  const interrupt = state?.interrupt;
  return (
    <div className="app">
      <h1>灵稿 · 黑五海报 PoC</h1>
      <p className="sub">关键事实先确认，模型只写文案；改一处不动其余；每轮一个版本。</p>
      {error && <div className="card err">出错：{error}</div>}
      <div className="layout">
        <div>
          {!state && <NewThread onStart={start} busy={busy} />}
          {state && interrupt?.type === "confirm_facts" && <FactsForm payload={interrupt} onSubmit={(values) => resume({ values })} busy={busy} />}
          {state && interrupt?.type === "choose_style" && <StyleChooser payload={interrupt} onSubmit={resume} busy={busy} />}
          {state && interrupt?.type === "review" && <ReviewPanel payload={interrupt} state={state} onSubmit={resume} busy={busy} />}
          {state && state.exported && <Exported exported={state.exported} onReset={() => { setState(null); setLog([]); }} />}
          {state && !interrupt && !state.exported && <div className="card">运行中… 下一步：{state.next.join(", ") || "无"}</div>}
          <div className="card">
            <h2>流程日志 {state && <span className="badge">thread {state.thread_id}</span>}</h2>
            <div className="log">{log.length ? log.join("\n") : "还没有开始。"}</div>
          </div>
        </div>
        <div>
          <Preview state={state} />
        </div>
      </div>
    </div>
  );
}

function NewThread({ onStart, busy }) {
  const [request, setRequest] = useState("某品牌蓝牙耳机黑五预热，5 折，11.20 到 11.27");
  const [image, setImage] = useState(null);
  return (
    <div className="card">
      <h2>1. 说需求</h2>
      <label>一句话需求（价格、折扣、日期只会从这里提取，不会编造）</label>
      <textarea value={request} onChange={(e) => setRequest(e.target.value)} />
      <label>产品图（可选，png / jpg / webp）</label>
      <input type="file" accept="image/*" onChange={(e) => setImage(e.target.files?.[0] || null)} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy || !request.trim()} onClick={() => onStart(request.trim(), image)}>开始</button>
      </div>
    </div>
  );
}

function FactsForm({ payload, onSubmit, busy }) {
  const [values, setValues] = useState(() => Object.fromEntries(payload.fields.map((f) => [f.key, f.value || ""])));
  const missing = payload.fields.filter((f) => f.required && !values[f.key]?.trim());
  return (
    <div className="card">
      <h2>2. 确认关键事实</h2>
      <p className="sub">{payload.message}</p>
      {payload.fields.map((f) => (
        <div key={f.key}>
          <label>
            {f.label} {f.required && <span className="req">*</span>}{" "}
            {f.source === "inferred" && f.value && <span className="badge">从需求中提取</span>}
          </label>
          <input type="text" value={values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
        </div>
      ))}
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy || missing.length > 0} onClick={() => onSubmit(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim() || null])))}>
          确认事实，继续
        </button>
        {missing.length > 0 && <span className="req">还缺：{missing.map((f) => f.label).join("、")}</span>}
      </div>
    </div>
  );
}

function StyleChooser({ payload, onSubmit, busy }) {
  const [sel, setSel] = useState(payload.options[0]?.id || null);
  const [custom, setCustom] = useState("");
  return (
    <div className="card">
      <h2>3. 选视觉方向</h2>
      <p className="sub">{payload.message}</p>
      <div className="styles">
        {payload.options.map((o) => (
          <div key={o.id} className={"style" + (sel === o.id && !custom ? " sel" : "")} onClick={() => { setSel(o.id); setCustom(""); }}>
            <div className="swatches">{o.palette.map((c) => <i key={c} style={{ background: c }} title={c} />)}</div>
            <div><b>{o.name}</b> <span className="badge">{o.mood}</span></div>
            <div style={{ color: "var(--muted)", fontSize: 12 }}>{o.description}</div>
          </div>
        ))}
      </div>
      <label>或者描述你想要的风格</label>
      <input type="text" value={custom} placeholder="例如：复古胶片感，暖棕色" onChange={(e) => setCustom(e.target.value)} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy || (!sel && !custom.trim())} onClick={() => onSubmit(custom.trim() ? { custom: custom.trim() } : { style_id: sel })}>
          用这个风格生成
        </button>
      </div>
    </div>
  );
}

function ReviewPanel({ payload, state, onSubmit, busy }) {
  const [editing, setEditing] = useState(null); // {id, mode: "edit"|"manual", text}
  const [styleNote, setStyleNote] = useState("");
  const submitEdit = () => {
    if (!editing?.text.trim()) return;
    if (editing.mode === "edit") onSubmit({ action: "edit", target: editing.id, instruction: editing.text.trim() });
    else onSubmit({ action: "manual_edit", target: editing.id, content: editing.text.trim() });
    setEditing(null);
  };
  const failed = (payload.checks || []).filter((c) => !c.passed);
  return (
    <div className="card">
      <h2>4. 看初稿，提意见（第 {payload.version} 版）</h2>
      {payload.notes?.length > 0 && <div className="notes">{payload.notes.join("；")}</div>}
      {failed.length > 0 && (
        <div className="notes">自动校验未通过（已重试到上限，请人工处理）：{failed.map((c) => `${c.name} ${c.detail}`).join("；")}</div>
      )}
      <div className="checks" style={{ fontSize: 12, color: "var(--muted)" }}>
        {(payload.checks || []).map((c) => <span key={c.name}>{c.passed ? "✓" : "✗"} {c.name}</span>)}
      </div>
      {payload.elements.filter((e) => e.kind !== "background").map((e) => (
        <div className="el" key={e.id}>
          <div className="head">
            <div>
              <span className="badge">{KIND_LABEL[e.kind] || e.kind}</span>{" "}
              {e.bound_fact
                ? <span className="badge fact">事实绑定</span>
                : <span className={"badge " + e.status}>{STATUS_LABEL[e.status]}</span>}
              {e.version > 1 && <span className="badge">v{e.version}</span>}
            </div>
            <div className="row">
              {e.editable_by_model && <button disabled={busy} onClick={() => setEditing({ id: e.id, mode: "edit", text: "" })}>让模型改</button>}
              {!e.bound_fact && e.kind !== "product_image" && <button disabled={busy} onClick={() => setEditing({ id: e.id, mode: "manual", text: e.text || "" })}>我来改</button>}
              {e.bound_fact && <button disabled={busy} onClick={() => onSubmit({ action: "change_fact" })}>改事实</button>}
            </div>
          </div>
          <div className="text">{e.kind === "product_image" ? (e.text === null ? "（占位）" : "") : e.text || "（空）"}</div>
          {editing?.id === e.id && (
            <div className="row" style={{ marginTop: 6 }}>
              <input type="text" style={{ flex: 1 }} value={editing.text} autoFocus
                placeholder={editing.mode === "edit" ? "告诉模型怎么改，例如：更有紧迫感" : "直接输入新文字"}
                onChange={(ev) => setEditing({ ...editing, text: ev.target.value })}
                onKeyDown={(ev) => ev.key === "Enter" && submitEdit()} />
              <button className="primary" disabled={busy} onClick={submitEdit}>提交</button>
              <button disabled={busy} onClick={() => setEditing(null)}>取消</button>
            </div>
          )}
        </div>
      ))}
      <div className="el">
        <div className="head">
          <div><span className="badge">风格</span> <b>{payload.style?.name}</b></div>
          <div className="row">
            <input type="text" value={styleNote} placeholder="描述新风格（留空则重新挑选）" onChange={(e) => setStyleNote(e.target.value)} style={{ width: 220 }} />
            <button disabled={busy} onClick={() => onSubmit({ action: "change_style", instruction: styleNote.trim() || null })}>换风格</button>
          </div>
        </div>
        <div style={{ color: "var(--muted)", fontSize: 12 }}>换风格只换配色与背景，不动任何文案。</div>
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <button className="primary" disabled={busy} onClick={() => onSubmit({ action: "approve" })}>就这版，导出</button>
      </div>
    </div>
  );
}

function Exported({ exported, onReset }) {
  return (
    <div className="card">
      <h2>5. 已导出（第 {exported.version} 版）</h2>
      <div className="row">
        <a href={exported.png_url} target="_blank" rel="noreferrer">final.png</a>
        <a href={exported.html_url} target="_blank" rel="noreferrer">final.html</a>
        <a href={exported.brief_url} target="_blank" rel="noreferrer">brief.json</a>
        <a href={exported.zip_url}>打包下载 zip</a>
        <button onClick={onReset}>再做一张</button>
      </div>
    </div>
  );
}

function Preview({ state }) {
  const [sel, setSel] = useState(null);
  const versions = state?.versions || [];
  if (!versions.length) return <div className="card preview"><h2>预览</h2><div style={{ color: "var(--muted)" }}>生成后在这里显示海报。</div></div>;
  const cur = versions.find((v) => v.n === sel) || versions[versions.length - 1];
  return (
    <div className="card preview">
      <h2>预览 · v{cur.n} <span className="badge">{cur.note}</span></h2>
      <div className="versions" style={{ marginBottom: 10 }}>
        {versions.map((v) => <button key={v.n} className={v.n === cur.n ? "sel" : ""} onClick={() => setSel(v.n)} title={v.note}>v{v.n}</button>)}
      </div>
      <img src={cur.png_url + "?t=" + cur.n} alt={`v${cur.n}`} />
    </div>
  );
}
