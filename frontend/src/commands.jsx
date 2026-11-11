// 在沙箱里跑命令（run_command，工作任务里才有，后端见 app/tools/run_command.py）：
// - 过程里的一步（CommandRow）：一行是命令的第一行和结果（用时、出错了、超时了…），点开看整条命令和它的输出。默认收着：输出往往很长。
// - 产出（outputsOf）：命令写在 outputs/ 里的文件。图在回答上面（images.jsx 的 ChartCards），别的在回答下面一个一张卡片（OutputFiles）：
//   抽得出文字的点开在右栏看，别的点了下载。一轮里同一个路径后来又写的，只留最后那个（前面的后端已经删了）。
import { SentFile } from "./files.jsx";
import { IconChevron, IconTerminal, Spinner } from "./icons.jsx";
import proc from "./process.module.css";
import { Fold, useFoldOpen } from "./sources.jsx";
import styles from "./commands.module.css";

// 这一轮跑命令产出的：charts 是图，files 是别的文件，都按最后一次写的先后
export function outputsOf(items) {
  const last = new Map();
  for (const i of items) {
    if (i.type !== "step" || i.kind !== "command" || !i.run) continue;
    for (const f of i.run.files) {
      last.delete(f.path);
      last.set(f.path, f);
    }
  }
  const all = [...last.values()];
  return { charts: all.filter((f) => f.kind === "image"), files: all.filter((f) => f.kind !== "image") };
}

const KILLED = { timeout: "超时了，已停下", memory: "内存不够，已停下", disk_quota: "工作目录写满了，已停下" };
const took = (ms) => (ms < 1000 ? "用时不到 1 秒" : `用时 ${Math.round(ms / 1000)} 秒`);

// 这一步的结果：跑了的按怎么结束的说；没跑成的是后端给的原因（history.RUN_FAILURES）
function statusOf(s) {
  const r = s.run;
  if (!r) return s.message || "没跑成";
  if (r.killed) return KILLED[r.killed] || "已停下";
  if (r.exit_code !== 0) return r.exit_code == null ? "出错了" : `出错了（退出码 ${r.exit_code}）`;
  return took(r.duration_ms);
}

// 过程里的一步。还没有结果、这一轮却不在跑了（出错停在这一步）：没有完成，和别的步骤一样说
export function CommandRow({ s, running }) {
  const [open, toggle, found] = useFoldOpen(`cmd:${s.id}`, false);
  const busy = running && s.pending;
  const lines = s.text.split("\n");
  const first = lines.find((l) => l.trim()) || "";
  const more = lines.filter((l) => l.trim()).length > 1;
  const sub = busy ? "运行中…" : s.pending ? "没有完成" : statusOf(s);
  const bad = !s.pending && !s.ok;
  const r = s.run;
  return (
    <div className={styles.cmd}>
      <button className={proc.tool + " " + styles.head + (busy ? " " + proc.pending : bad ? " " + proc.bad : "")} aria-expanded={open} onClick={toggle}>
        {busy ? <Spinner size={12} /> : <IconTerminal size={12} />}
        <span className={proc.main} title={first}>运行 <code className={styles.line}>{first}{more ? " …" : ""}</code></span>
        <span className={proc.sub} title={sub}>{sub}</span>
        <IconChevron size={13} className={"chev" + (open ? " up" : "")} />
      </button>
      <Fold open={open} onFound={found}>
        <div className={styles.detail}>
          <div className={styles.label}>命令</div>
          <pre className={styles.out}>{s.text}</pre>
          {r?.stdout && <><div className={styles.label}>输出</div><pre className={styles.out}>{r.stdout}</pre></>}
          {r?.stderr && <><div className={styles.label}>报错</div><pre className={styles.out + " " + styles.err}>{r.stderr}</pre></>}
          {r && !r.stdout && !r.stderr && <div className={styles.none}>没有输出</div>}
        </div>
      </Fold>
    </div>
  );
}

// 回答下面产出的文件。after：上面已经有模型写的文件卡片，接着排
export function OutputFiles({ files, tid, panel, after = false }) {
  if (!files.length) return null;
  return (
    <div className={styles.outputs + (after ? " " + styles.after : "")}>
      {files.map((f) => <SentFile key={f.id} f={f} tid={tid} panel={panel} wide />)}
    </div>
  );
}
