// 请求被拒（4xx/5xx）时抛出的错误带上状态码：页面据此区分“回答不对 / 卡已过期”和“连接断了”
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

// 后端两个"推进"接口都返回 SSE；这里把流解析成事件回调，最后一个 state 事件是完整状态。
// 流中途断开时返回 null（没收到 state），或者抛出不带 status 的网络错误。
async function readSse(resp, onEvent) {
  if (!resp.ok) throw await httpError(resp);
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let last = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let ev = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) ev = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const parsed = data ? JSON.parse(data) : null;
      onEvent(ev, parsed);
      if (ev === "state") last = parsed;
    }
  }
  return last;
}

export async function createThread(request, imageFile, onEvent) {
  const fd = new FormData();
  fd.append("request", request);
  if (imageFile) fd.append("image", imageFile);
  return readSse(await fetch("/api/threads", { method: "POST", body: fd }), onEvent);
}

export async function resumeThread(threadId, payload, onEvent) {
  const resp = await fetch(`/api/threads/${threadId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload }),
  });
  return readSse(resp, onEvent);
}

export async function getState(threadId) {
  const resp = await fetch(`/api/threads/${threadId}/state`);
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

// ---- 调研场景 ----

export async function createResearchThread(request, onEvent) {
  const resp = await fetch("/api/research/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request }),
  });
  return readSse(resp, onEvent);
}

export async function getResearchState(threadId) {
  const resp = await fetch(`/api/research/threads/${threadId}/state`);
  if (!resp.ok) throw await httpError(resp);
  return resp.json();
}

// interruptId 是这张卡的中断 id：线程已经往前走了（卡过期）或正在处理，后端返回 409
export async function resumeResearchThread(threadId, interruptId, payload, onEvent) {
  const resp = await fetch(`/api/research/threads/${threadId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ interrupt_id: interruptId, payload }),
  });
  return readSse(resp, onEvent);
}
