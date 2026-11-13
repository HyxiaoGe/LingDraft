// errors.js：报错转写成“发生了什么 + 能做什么”，不露后端的原话。
import { describe, expect, it } from "vitest";
import { errorText, failureText, quotaText } from "./errors.js";

describe("errorText：按状态码说原因和能做什么", () => {
  it("连不上（没有状态码）", () => {
    expect(errorText("没能打开这个对话", new TypeError("Failed to fetch"))).toBe("没能打开这个对话：连不上服务，检查网络后再试一次。");
  });
  it("各种状态码", () => {
    expect(errorText("没能打开这个对话", { status: 404 })).toBe("没能打开这个对话：这个对话已经不在了，可能被删掉了，从左栏换一个对话，或者开个新对话。");
    expect(errorText("没发出去", { status: 409 })).toBe("没发出去：这个对话正在回答，或者上一轮还没有完成，等这一轮完成再试。");
    expect(errorText("没发出去", { status: 401 })).toBe("没发出去：登录状态变了，刷新页面后再试一次。");
    expect(errorText("没发出去", { status: 403 })).toBe("没发出去：登录状态变了，刷新页面后再试一次。");
    expect(errorText("没发出去", { status: 429 })).toBe("没发出去：现在用的人太多，稍后再试，或者登录后再用。");
    expect(errorText("没发出去", { status: 502 })).toBe("没发出去：服务暂时出了问题，稍后再试一次。");
    expect(errorText("没发出去", { status: 422 })).toBe("没发出去：这次的操作没被接受，改一下再试，或者刷新页面。");
  });
  it("这件事上有专门的做法时用它", () => {
    expect(errorText("这句话没发出去，已经放回输入框", { status: 500 }, "稍后再发一次")).toBe("这句话没发出去，已经放回输入框：服务暂时出了问题，稍后再发一次。");
  });
});

describe("quotaText：额度用完了", () => {
  const resets_at = new Date(2026, 10, 14).toISOString(); // 本地时间 11 月 14 日 0 点

  it("登录用户：哪个额度、什么时候恢复", () => {
    expect(quotaText({ kind: "day", resets_at, guest: false })).toBe("今天的额度用完了，明天（11月14日）0 点恢复。");
    expect(quotaText({ kind: "week", resets_at, guest: false })).toBe("这周的额度用完了，下周一（11月14日）0 点恢复。");
  });
  it("游客：再说登录后会怎样", () => {
    expect(quotaText({ kind: "day", resets_at, guest: true, login: { day: 10000 } })).toBe("今天的额度用完了，明天（11月14日）0 点恢复。登录后每天 10,000 额度。");
    expect(quotaText({ kind: "day", resets_at, guest: true })).toBe("今天的额度用完了，明天（11月14日）0 点恢复。登录后额度更多。");
  });
  it("所有游客合用的那份：说清不是这个人用多了", () => {
    expect(quotaText({ kind: "guests", resets_at, guest: true, login: { day: 10000 } }))
      .toBe("今天所有游客合用的额度用完了：游客共用一份每日总量，用来控制成本，不是你用多了。明天（11月14日）0 点恢复。登录后用自己的额度，每天 10,000 额度，不受这个影响。");
  });
  it("恢复时间读不出来就不写日期", () => {
    expect(quotaText({ kind: "day", resets_at: "不是时间", guest: false })).toBe("今天的额度用完了，明天 0 点恢复。");
  });
});

describe("failureText：这一轮没跑完", () => {
  it("模型服务、内容审核的说法用后端给的", () => {
    expect(failureText({ error_kind: "model", error: "模型服务暂时不可用" })).toBe("模型服务暂时不可用。已经查到的内容都还在，到时点重试，会从断开的地方接着做。");
    expect(failureText({ error_kind: "blocked", error: "内容审核没通过" })).toBe("内容审核没通过，这一轮停在这里。可以换个说法，开个新对话再问。");
  });
  it("别的类别不用后端的原话", () => {
    expect(failureText({ error_kind: "cut_short", error: "CancelledError()" })).toBe("服务中途重启过，这一轮没有完成。已经查到的内容都还在，点重试会从断开的地方接着做。");
    expect(failureText({ error_kind: "tool", error: "GatewayError(502)" })).not.toContain("GatewayError");
    expect(failureText({ error_kind: "internal", error: "KeyError('x')" })).toBe("处理这一轮时出了意外的错误。已经查到的内容都还在，可以点重试；一直不行，可以开个新对话。");
    expect(failureText(null)).toBe("处理这一轮时出了意外的错误。已经查到的内容都还在，可以点重试；一直不行，可以开个新对话。");
  });
});
