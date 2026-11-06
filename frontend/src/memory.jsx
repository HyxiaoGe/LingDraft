// 记忆页：灵稿每天在后台把对话整理成关于用户的记忆（见后端 graph/research/memory.py），回答时放进系统提示词。
// 现在只能看，不能改、删（2026-11-03 Sean 定先展示）。记忆按账号分（2026-11-06）：游客的对话不整理，登录后才有。
import { useEffect, useState } from "react";
import { getMemory } from "./api.js";
import { HeadBar } from "./nav.jsx";
import { stamp } from "./research.js";

// 上次整理的结果，一句话
function lastText(d, now) {
  if (!d) return "还没整理过。";
  const s = stamp(d.started_at, now);
  const at = s ? (s.day ? s.text : `今天 ${s.hm}`) : "";
  if (!d.ended_at) return `${at} 开始的那次整理没有做完，下次接着整理。`;
  const counts = [["新记", d.added], ["改了", d.updated], ["删了", d.deleted]].filter(([, n]) => n).map(([w, n]) => `${w} ${n} 条`);
  const did = d.threads ? `看了 ${d.threads} 段对话，${counts.length ? counts.join("，") : "没有要记的"}` : "没有新的对话";
  return `${at} 整理过：${did}${d.failed ? `；有 ${d.failed} 段没整理成，下次再试` : ""}。`;
}

export function MemoryPage({ auth, onLogin }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const load = () => {
    setError(null);
    getMemory().then(setData).catch((e) => setError(e.message || String(e)));
  };
  useEffect(load, []);
  const now = Date.now();
  const next = data?.next_dream ? stamp(data.next_dream, now) : null;
  const guest = auth?.ready && !auth.me; // 游客：没有记忆，说明登录了才有
  return (
    <section className="ws-main" aria-label="记忆">
      <HeadBar title="记忆" />
      <div className="ws-scroll">
        <div className="ws-flow mem">
          <p className="mem-intro">
            灵稿每天{next ? ` ${next.hm} ` : ""}在后台把有新内容的对话整理一遍，记下关于你的事，之后回答时会参考。记忆按账号分，只有你自己看得到。
          </p>
          {guest && (
            <div className="mem-status">
              没登录的对话不整理，登录后才有记忆。{onLogin && <button className="ws-btn sm" onClick={onLogin}>登录</button>}
            </div>
          )}
          {error && (
            <div className="mem-status">
              没取到记忆：{error} <button className="ws-btn sm" onClick={load}>再试一次</button>
            </div>
          )}
          {!error && !data && <div className="mem-status">正在取记忆…</div>}
          {data && (
            <>
              <div className="mem-status">
                {lastText(data.last_dream, now)}
                {next ? ` 下次整理：${next.day ? next.text : `今天 ${next.hm}`}。` : " 按时整理没有开。"}
              </div>
              {data.memories.length === 0 ? <div className="mem-empty">{guest ? "登录后这里会有记忆。" : "还没有记忆。"}</div> : (
                <ul className="mem-list">
                  {data.memories.map((m) => {
                    const changed = m.updated_at && m.updated_at !== m.created_at;
                    const t = stamp(changed ? m.updated_at : m.created_at, now);
                    return (
                      <li key={m.id}>
                        <span className="text">{m.text}</span>
                        <span className="meta" title={t?.full}>{t ? `${t.day || "今天"}${changed ? "改过" : "记下"}` : ""}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
