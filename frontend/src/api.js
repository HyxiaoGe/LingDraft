import { authFetch } from "./auth.js";

// 请求被拒（4xx/5xx）时抛出的错误带上状态码：页面据此区分“请求被拒”和“连接断了”
// detail 是对象时（额度用完的 429：{code, message, guest, kind, resets_at…}）原样放在 err.detail 上，message 用它的 message
async function httpError(resp) {
  let msg = await resp.text();
  let detail = null;
  try {
    const d = JSON.parse(msg).detail;
    if (d && typeof d === "object") detail = d;
    msg = typeof d === "string" ? d : d?.message || JSON.stringify(d);
  } catch { /* 不是 JSON，用原文 */ }
  const err = new Error(msg);
  err.status = resp.status;
  err.detail = detail;
  return err;
}

// 后端安静 15 秒就发一行保活；这么久什么都没收到，说明连接已经死了（代理没把后端断开传过来、电脑休眠唤醒等），主动断开
const IDLE_MS = 45000;

// 后端"推进"接口都返回 SSE；这里把流解析成事件回调 onEvent(event, data, id)，最后一个 state 事件是完整状态。
// id 是事件在这次执行里的编号，断线后凭它接着读。流中途断开时返回 null（没收到 state），或者抛出不带 status 的网络错误。
// signal：页面切到别的线程时主动断开，抛出的错误带 aborted（服务器上的执行不受影响）
async function sse(url, init, onEvent, signal) {
  const ctl = new AbortController();
  let timer;
  const alive = () => { clearTimeout(timer); timer = setTimeout(() => ctl.abort(), IDLE_MS); };
  const stop = () => ctl.abort();
  signal?.addEventListener("abort", stop);
  alive();
  try {
    if (signal?.aborted) ctl.abort();
    const get = url.startsWith("/api/research/") ? authFetch : fetch; // 对话接口带身份（见 auth.js），海报的不带
    return await readSse(await get(url, { ...init, signal: ctl.signal }), onEvent, alive);
  } catch (e) {
    if (signal?.aborted) throw Object.assign(new Error("已切到别的对话"), { aborted: true });
    if (ctl.signal.aborted) throw new Error("连接长时间没有数据，已断开");
    throw e;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", stop);
  }
}

async function readSse(resp, onEvent, alive) {
  if (!resp.ok) throw await httpError(resp);
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let last = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    alive();
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let ev = "message";
      let data = "";
      let id = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) ev = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
        else if (line.startsWith("id: ")) id = line.slice(4);
      }
      if (!data) continue; // 保活的注释行
      const parsed = JSON.parse(data);
      onEvent(ev, parsed, id);
      if (ev === "state") last = parsed;
    }
  }
  return last;
}

const postJson = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
// 带图的话（images 是 File 的列表，见 upload.js）发 multipart：body 是这句话的 JSON，每张图一个 image 字段，按发的先后（见后端 api.main 的 _incoming）。
// 图不收时回 413（超过 4 MB）、415（格式不对）、502（传不上去），这时对话里什么都没多出来
const message = ({ images, ...body }) => {
  if (!images?.length) return postJson(body);
  const fd = new FormData();
  fd.append("body", JSON.stringify(body));
  for (const f of images) fd.append("image", f);
  return { method: "POST", body: fd };
};

export async function createThread(request, imageFile, onEvent) {
  const fd = new FormData();
  fd.append("request", request);
  if (imageFile) fd.append("image", imageFile);
  return sse("/api/threads", { method: "POST", body: fd }, onEvent);
}

export async function resumeThread(threadId, payload, onEvent) {
  return sse(`/api/threads/${threadId}/resume`, postJson({ payload }), onEvent);
}


// ---- 对话 ----

export async function listResearchThreads() {
  const resp = await authFetch("/api/research/threads");
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).threads;
}

// 长期记忆：{memories: [{id, text, created_at, updated_at, thread_id}], last_dream, next_dream}（见后端 ResearchRunner.memory_view）。
// thread_id 是从哪段对话记下的（很早的几条没有）
export async function getMemory() {
  const resp = await authFetch("/api/memory");
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 改一条记忆的文字，返回改过的这一条。空的、超过 200 字 400；不是自己的、没有这一条 404
export async function editMemory(id, text) {
  const resp = await authFetch(`/api/memory/${encodeURIComponent(id)}`, { ...postJson({ text }), method: "PATCH" });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 删一条记忆。keepalive：关页面时撤销还没过期，照样发出去
export async function deleteMemory(id, { keepalive = false } = {}) {
  const resp = await authFetch(`/api/memory/${encodeURIComponent(id)}`, { method: "DELETE", keepalive });
  if (!resp.ok) throw await httpError(resp);
}

// 新对话页的“今日灵感”（后端从 Fusion 代取）；取不到抛错，页面用固定的例子
export async function listInspirations() {
  const resp = await fetch("/api/inspirations");
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).questions;
}

// 新对话能选的模型：[{id, name, description, thinking, model, locked}]，Auto 在第一个。locked 是这个人这一档选不了的（游客看到的要登录的模型）。
// thinking 是 toggle 的显示思考开关（always、never 不显示）；model 是它用的“厂商:模型”，按它给每轮记下的模型找名字
// 额度：{unlimited, warn_at, windows: [{kind: "day"|"week"|"guests", limit, used, resets_at}]}（见后端 app.quota）
export async function getCredits() {
  const resp = await authFetch("/api/credits");
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 能选的模型（models，Auto 在第一个）和选项以外、回答开头可能出现的模型（names：Auto 用的免费额度，"厂商:模型" → {name, icon}）
export async function listModels() {
  const resp = await authFetch("/api/models");
  if (!resp.ok) throw await httpError(resp);
  const { models, names } = await resp.json();
  return { models, names: names || {} };
}

// 开新对话。opts：model 是选的模型（listModels 里的 id，不给是 Auto；没有了返回 400），定下就不换；
// thinking 是这句话的思考开关（不给按模型的默认）。下面几个的 opts.thinking 一样。opts.images：这句话带的图（见 message）
export async function createResearchThread(text, opts, onEvent, signal) {
  return sse("/api/research/threads", message({ text, ...opts }), onEvent, signal);
}

// 接着说，opts.images 同上。运行中、上一轮没跑完时后端返回 409
export async function sayResearch(threadId, text, opts, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/messages`, message({ text, ...opts }), onEvent, signal);
}

export async function getResearchState(threadId) {
  const resp = await authFetch(`/api/research/threads/${threadId}/state`);
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 让正在跑（或排队）的这一轮停下：只是提出请求，真正停下以后，原来那条事件流照常收到最终状态。没在跑的返回 409
export async function stopResearchThread(threadId) {
  const resp = await authFetch(`/api/research/threads/${threadId}/stop`, { method: "POST" });
  if (!resp.ok) throw await httpError(resp);
}

// 正在回答时又说的话：排着，这一轮答完或停下就接着发。返回排着的那句（{text, created_at, updated_at}）；
// 这一轮刚好答完了，后端直接发出去，返回 null，页面去接事件流。已经有一句没发出去、或上一轮没跑完时返回 409
export async function queueResearch(threadId, text, opts) {
  const resp = await authFetch(`/api/research/threads/${threadId}/queue`, postJson({ text, ...opts }));
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).queued;
}

// 撤回排着的话，返回原文（放回输入框）；已经发出去了或没有，返回 null
export async function unqueueResearch(threadId) {
  const resp = await authFetch(`/api/research/threads/${threadId}/queue`, { method: "DELETE" });
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).text;
}

// 排着的话不等这一轮答完，插进这一轮：模型做完手上这一步、下一次调模型之前看到它。返回等着插进去的那句；
// 已经没在跑了（排着的话已经发出去了）返回 null
export async function guideResearch(threadId) {
  const resp = await authFetch(`/api/research/threads/${threadId}/guide`, { method: "POST" });
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).guided;
}

// 撤回等着插进这一轮的话，返回原文（放回输入框）；已经插进去了或没有，返回 null
export async function unguideResearch(threadId) {
  const resp = await authFetch(`/api/research/threads/${threadId}/guide`, { method: "DELETE" });
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).text;
}

// 最后一轮重新回答：去掉这一轮的回答，从用户那句话重新跑。运行中、上一轮没跑完时后端返回 409
export async function regenerateResearchThread(threadId, opts, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/regenerate`, postJson({ ...opts }), onEvent, signal);
}

// 改了最后那句话，按新的话重新回答。运行中、上一轮没跑完时后端返回 409
export async function editResearchThread(threadId, text, opts, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/edit`, postJson({ text, ...opts }), onEvent, signal);
}

// 改标题，返回列表里的这一行。运行中、上一轮没跑完时返回 409
export async function renameResearchThread(threadId, title) {
  const resp = await authFetch(`/api/research/threads/${threadId}`, { ...postJson({ title }), method: "PATCH" });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 删掉这段对话，找不回来（页面上先给 5 秒撤销，过了才调这里，见 undo.jsx）。运行中返回 409。keepalive：关页面时照样发出去
export async function deleteResearchThread(threadId, { keepalive = false } = {}) {
  const resp = await authFetch(`/api/research/threads/${threadId}`, { method: "DELETE", keepalive });
  if (!resp.ok) throw await httpError(resp);
}

// 置顶、取消置顶，返回列表里的这一行（pinned_at 是置顶的时间，取消了是 null）。存在后端，换设备也在
export async function pinResearchThread(threadId, pinned) {
  const resp = await authFetch(`/api/research/threads/${threadId}/pin`, { method: pinned ? "POST" : "DELETE" });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 失败的线程从断开的那一步接着跑；没失败的返回 409
export async function retryResearchThread(threadId, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/retry`, { method: "POST" }, onEvent, signal);
}

// 接上线程当前这次执行：after 是收到的最后一个事件 id（没有就从头补发）；执行早就结束了就只给当前状态
export async function followResearchThread(threadId, after, onEvent, signal) {
  const q = after ? `?after=${encodeURIComponent(after)}` : "";
  return sse(`/api/research/threads/${threadId}/events${q}`, {}, onEvent, signal);
}
