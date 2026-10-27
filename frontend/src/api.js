// 后端两个"推进"接口都返回 SSE；这里把流解析成事件回调，最后一个 state 事件是完整状态。
async function readSse(resp, onEvent) {
  if (!resp.ok) throw new Error(await resp.text());
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
