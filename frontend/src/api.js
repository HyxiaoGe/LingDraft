// 请求被拒（4xx/5xx）时抛出的错误带上状态码：页面据此区分“请求被拒”和“连接断了”
async function httpError(resp) {
  let msg = await resp.text();
  try {
    const d = JSON.parse(msg).detail;
    msg = typeof d === "string" ? d : JSON.stringify(d);
  } catch { /* 不是 JSON，用原文 */ }
  const err = new Error(msg);
  err.status = resp.status;
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
    return await readSse(await fetch(url, { ...init, signal: ctl.signal }), onEvent, alive);
  } catch (e) {
    if (signal?.aborted) throw Object.assign(new Error("已切到别的线程"), { aborted: true });
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

export async function createThread(request, imageFile, onEvent) {
  const fd = new FormData();
  fd.append("request", request);
  if (imageFile) fd.append("image", imageFile);
  return sse("/api/threads", { method: "POST", body: fd }, onEvent);
}

export async function resumeThread(threadId, payload, onEvent) {
  return sse(`/api/threads/${threadId}/resume`, postJson({ payload }), onEvent);
}

export async function getState(threadId) {
  const resp = await fetch(`/api/threads/${threadId}/state`);
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

// ---- 对话 ----

export async function listResearchThreads() {
  const resp = await fetch("/api/research/threads");
  if (!resp.ok) throw await httpError(resp);
  return (await resp.json()).threads;
}

export async function createResearchThread(text, onEvent, signal) {
  return sse("/api/research/threads", postJson({ text }), onEvent, signal);
}

// 接着说。运行中、上一轮没跑完时后端返回 409
export async function sayResearch(threadId, text, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/messages`, postJson({ text }), onEvent, signal);
}

export async function getResearchState(threadId) {
  const resp = await fetch(`/api/research/threads/${threadId}/state`);
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// 让正在跑（或排队）的这一轮停下：只是提出请求，真正停下以后，原来那条事件流照常收到最终状态。没在跑的返回 409
export async function stopResearchThread(threadId) {
  const resp = await fetch(`/api/research/threads/${threadId}/stop`, { method: "POST" });
  if (!resp.ok) throw await httpError(resp);
}

// 最后一轮重新回答：去掉这一轮的回答，从用户那句话重新跑。运行中、上一轮没跑完时后端返回 409
export async function regenerateResearchThread(threadId, onEvent, signal) {
  return sse(`/api/research/threads/${threadId}/regenerate`, { method: "POST" }, onEvent, signal);
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
