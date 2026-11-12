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

// 新对话页的“今日灵感”（后端从 Fusion 代取）：{questions, tasks}，tasks 是其中的调研题，页面切到“工作”时用；取不到抛错，页面用固定的例子
export async function listInspirations() {
  const resp = await fetch("/api/inspirations");
  if (!resp.ok) throw await httpError(resp);
  const { questions, tasks } = await resp.json();
  return { questions: questions || [], tasks: tasks || [] };
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
// thinking 是这句话的思考开关（不给按模型的默认；工作任务不看，一直开）；work 是开的是不是工作任务（新页面上选着“工作”，不给是对话），定下就不变，只有这里带。
// 下面几个的 opts.thinking 一样。opts.images：这句话带的图（见 message）；
// opts.files：带的文件的编号（先 uploadFile、解析好了），不能发时 400，detail 是 {code: "files_not_sendable", reason}
export async function createResearchThread(text, opts, onEvent, signal) {
  return sse("/api/research/threads", message({ text, ...opts }), onEvent, signal);
}

// 接着说，opts.images 同上。运行中、上一轮没跑完时后端返回 409
export async function sayResearch(threadId, text, opts, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/messages`, message({ text, ...opts }), onEvent, signal);
}

// ---- 用户发的文件（见后端 app/files）：选好就传，后台解析，问到解析好了，发消息时带上编号（opts.files） ----

// 传一个文件，马上回 {id, name, type, size, status, reason?, file?}（status 多半是 parsing）。
// 不收时抛 httpError：413 太大、415 不支持、429 今天传满了，detail 是 {code: "file_rejected", reason, …}（见 upload.js 的 fileErrorText）
export async function uploadFile(file, signal) {
  const fd = new FormData();
  fd.append("file", file);
  const resp = await authFetch("/api/files", { method: "POST", body: fd, signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 解析好了没有，同 uploadFile 回的；过期清掉了 404
export async function getFile(id, signal) {
  const resp = await authFetch(`/api/files/${encodeURIComponent(id)}`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 去掉还没发出去的文件（输入框里点叉）：去不掉也不要紧，没发出去的一天后自己清掉
export async function removeFile(id) {
  await authFetch(`/api/files/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
}

// 对话里发过的文件的原件（blob）：要带登录信息。下载、右栏看 PDF 都用它
export async function threadFileBlob(threadId, id, signal) {
  const resp = await authFetch(`/api/research/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(id)}`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.blob();
}

// 下载对话里发过的文件（原件）：取成 blob 再存，文件名用原来的
export async function downloadThreadFile(threadId, f) {
  saveBlob(await threadFileBlob(threadId, f.id), f.name);
}

// 模型写的文档转成 PDF / Word（format: "pdf" | "docx"），这段对话里的 PPT 排成 PDF（只有 "pdf"，一张一页）：
// 后端现转一份（转过的直接给），不经过模型、不扣额度。没转成时 err.detail.code 是原因（见后端 app/api/export.py），页面按它说话
export async function threadFileExport(threadId, id, format, signal) {
  const resp = await authFetch(`/api/research/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(id)}/export?format=${format}`, { method: "POST", signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.blob();
}

// 右栏的下载菜单：转好了存下来
export async function exportThreadFile(threadId, f, format) {
  saveBlob(await threadFileExport(threadId, f.id, format), `${f.name.replace(/\.(md|pptx)$/i, "")}.${format}`);
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 模型写的文件某一版的全文（右栏显示、复制、下载用）：和用户发的文件同一个接口，要带登录信息
export async function threadFileText(threadId, id, signal) {
  const resp = await authFetch(`/api/research/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(id)}`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.text();
}

// 右栏预览用户发的文件：抽出来的文字，{name, type, parts: [{title, text}], more}（太长的只有前面一部分，more 为 true）
export async function threadFileParts(threadId, id, signal) {
  const resp = await authFetch(`/api/research/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(id)}/parts`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// ---- 资料库（见后端 app/knowledge，以前叫知识库）：一个人能有几个，游客没有。传上去后台解析、建索引，问到 ready 才查得到 ----

// {available, libraries: [{id, name, description, created_at, files: [同 getKnowledgeFile 回的 + created_at]}],
//  limits: {files, chars, mb, libraries, name, description}, used: {files, chars}}；游客 available 是 false。文件个数、字数按人算
export async function getKnowledge(signal) {
  const resp = await authFetch("/api/knowledge", { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 建一个资料库，回 {id, name, description, created_at}。不收时抛 httpError，detail 是 {code: "library_rejected", reason, …}：
// 403 login_required、422 bad_name / bad_description（带 limit）、409 libraries_full（带 limit）/ name_taken
export async function createLibrary({ name, description }) {
  const resp = await authFetch("/api/knowledge/libraries", postJson({ name, description }));
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 改名字、说明（没给的不改），回改好的。不收的同 createLibrary；删了的 404
export async function updateLibrary(id, { name, description }) {
  const resp = await authFetch(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, description }) });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 删一个资料库，里面的文件一起删。keepalive：关页面时撤销还没过期，照样发出去
export async function deleteLibrary(id, { keepalive = false } = {}) {
  const resp = await authFetch(`/api/knowledge/libraries/${encodeURIComponent(id)}`, { method: "DELETE", keepalive });
  if (!resp.ok && resp.status !== 404) throw await httpError(resp);
}

// 传进一个资料库，马上回 {id, name, type, size, status, reason?, file?}（status 是 parsing，建好索引才是 ready）。
// 不收时抛 httpError，detail 是 {code: "file_rejected", reason, …}：403 login_required、404 no_library、409 kb_full（带 limit）、413、415
export async function uploadKnowledge(libraryId, file, signal) {
  const fd = new FormData();
  fd.append("file", file);
  const resp = await authFetch(`/api/knowledge/libraries/${encodeURIComponent(libraryId)}/files`, { method: "POST", body: fd, signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 处理好了没有；failed 的 reason 多了 kb_chars、index_failed、no_text。删了的 404
export async function getKnowledgeFile(id, signal) {
  const resp = await authFetch(`/api/knowledge/files/${encodeURIComponent(id)}`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 删一个文件，删了马上查不到。keepalive：关页面时撤销还没过期，照样发出去
export async function deleteKnowledgeFile(id, { keepalive = false } = {}) {
  const resp = await authFetch(`/api/knowledge/files/${encodeURIComponent(id)}`, { method: "DELETE", keepalive });
  if (!resp.ok && resp.status !== 404) throw await httpError(resp);
}

// 右栏看资料库里的文件：同 threadFileParts
export async function knowledgeFileParts(id, signal) {
  const resp = await authFetch(`/api/knowledge/files/${encodeURIComponent(id)}/parts`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 资料库里文件的原件（blob），下载、右栏看 PDF 用
export async function knowledgeFileBlob(id, signal) {
  const resp = await authFetch(`/api/knowledge/files/${encodeURIComponent(id)}/original`, { signal });
  if (!resp.ok) throw await httpError(resp);
  return resp.blob();
}

export async function downloadKnowledgeFile(f) {
  saveBlob(await knowledgeFileBlob(f.id), f.name);
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

// 回答模型停下来问的问题（状态 asking 时的 state.ask），这一轮接着跑。answer：{id: 问题的 id, text: 回答} 或 {id, skip: true}（跳过），
// 或 {id, mute: true}（别再问了：跳过，并且这段对话之后不再出问题卡，状态里的 asks_off 变成 true）。
// 等着的是要位置的（state.ask.kind 是 location）：{id, location: {longitude, latitude, accuracy_m}}（浏览器给的位置，后端只放在内存里、留一小段时间），
// 或 {id, unavailable: true}（取不到）、{id, skip: true}（不给）、{id, text}（没给位置，直接说了个地方）。
// 这个问题已经不用回答了（答过了、这一轮停了）返回 409；回答是空的、太长返回 400
export async function answerResearchThread(threadId, answer, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/answer`, postJson(answer), onEvent, signal);
}

// 这段对话 Work 的轮还出不出问题卡：点过“别再问了”的（state.asks_off）发 on=true 恢复。返回对话的状态。正在回答、没跑完、问着的时候返回 409
export async function setResearchAsks(threadId, on) {
  const resp = await authFetch(`/api/research/threads/${threadId}/asks`, { ...postJson({ on }), method: "PUT" });
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 接上线程当前这次执行：after 是收到的最后一个事件 id（没有就从头补发）；执行早就结束了就只给当前状态
export async function followResearchThread(threadId, after, onEvent, signal) {
  const q = after ? `?after=${encodeURIComponent(after)}` : "";
  return sse(`/api/research/threads/${threadId}/events${q}`, {}, onEvent, signal);
}
