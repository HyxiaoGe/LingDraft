import { loginLine } from "./research.js";

// 页面上的报错：按出了什么错转写成“发生了什么 + 能做什么”，不把后端、浏览器的原话直接显示出来（前端施工规范第二节）。
// e 是 api.js 的 httpError（有 status，额度用完时 detail 是 {code, kind, resets_at, guest…}），或者连不上时 fetch 抛的 TypeError（没有 status）。
// 看的只是状态码和后端给的结构化字段，不看报错文字

// 出了什么错 → [原因, 能做什么]
function reason(e) {
  const s = e?.status;
  if (!s) return ["连不上服务", "检查网络后再试一次"];
  if (s === 404) return ["这个对话已经不在了，可能被删掉了", "从左栏换一个对话，或者开个新对话"];
  if (s === 409) return ["这个对话正在回答，或者上一轮还没做完", "等这一轮做完再试"];
  if (s === 401 || s === 403) return ["登录状态变了", "刷新页面后再试一次"];
  if (s === 429) return ["现在用的人太多", "稍后再试，或者登录后再用"];
  if (s >= 500) return ["服务暂时出了问题", "稍后再试一次"];
  return ["这次的请求没被接受", "改一下再试，或者刷新页面"];
}

// what：没做成的事，比如“没能打开这个对话”“这句话没发出去，已经放回输入框”；todo：这件事上用户能做的，不给按出错的种类说
export function errorText(what, e, todo) {
  const [why, generic] = reason(e);
  return `${what}：${why}，${todo || generic}。`;
}

// 额度用完了（429，detail.code 是 credits_exhausted，见后端 app.quota.Exhausted）：哪个额度、什么时候恢复（说法和 research.js 的 creditsLine 一样），
// 游客再说登录后会怎样（detail.login、per_ask，见 loginLine）。所有游客合用的那份用完了要说清是为什么——不是这个人用多了（评审第 26 条）
export function quotaText(d) {
  const when = new Date(d.resets_at);
  const day = Number.isNaN(when.getTime()) ? " " : `（${when.getMonth() + 1}月${when.getDate()}日）`;
  const after = d.guest ? loginLine(d) : null;
  if (d.kind === "guests") {
    return `今天所有游客合用的额度用完了：游客共用一份每日总量，用来控制成本，不是你用多了。明天${day}0 点恢复。`
      + `登录后用自己的额度，${after ? `${after.replace(/^登录后/, "")}，` : ""}不受这个影响。`;
  }
  const span = d.kind === "week" ? "这周" : "今天";
  const reset = d.kind === "week" ? `下周一${day}0 点恢复` : `明天${day}0 点恢复`;
  return `${span}的额度用完了，${reset}。${after ? `${after}。` : d.guest ? "登录后额度更多。" : ""}`;
}

// 这一轮没跑完（状态 failed）：按后端给的类别（error_kind，见 app.api.research_runner 的 _failure）说原因和能做什么。
// 模型服务不可用、内容审核的说法是后端写给用户看的，原样用；其余的后端已经换成通用说法，这里也不用它的原话
export function failureText(st) {
  const kept = "已经查到的内容都还在";
  switch (st?.error_kind) {
    case "model":
      return `${st.error}。${kept}，到时点重试，会从断开的地方接着做。`;
    case "blocked":
      return `${st.error}，这一轮停在这里。可以换个说法，开个新对话再问。`;
    case "cut_short":
      return `服务中途重启过，这一轮没做完。${kept}，点重试会从断开的地方接着做。`;
    case "tool":
      return `查资料、地图或画图用的服务出了问题，这一轮停在这里。${kept}，可以稍后点重试；一直不行，可以开个新对话换个问法。`;
    default:
      return `处理这一轮时出了意外的错误。${kept}，可以点重试；一直不行，可以开个新对话。`;
  }
}

// 事件流里的 error 事件（这一轮出错、或者答完以后收尾时出错，后端已经分类转写过，见 _error_event）：
// 这一轮没跑完的看失败卡；没算失败的（答完以后取状态、起“接着问”出错）只说收尾出了问题
export const AFTER_RUN_ERROR = "这一轮已经答完，但收尾时出了点问题：刷新页面能看到最新的样子。";
