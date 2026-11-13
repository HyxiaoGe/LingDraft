// research.js 里不碰页面的函数：出字时怎么收拾半截的 Markdown、引用怎么编号、实时片段和存下来的怎么合、时间和额度怎么写。
// 时间都用本地时间造（new Date(年, 月, 日, …)），在哪个时区跑结果都一样。
import { describe, expect, it } from "vitest";
import {
  addLive, answerText, citeNumbers, closesFence, creditsLeft, creditsLine, creditsText, cutPartialCite, domain, elapsed, fenceHtml,
  loginLine, mergeItems, modelName, sayWhen, sourceLine, sourceName, splitTurn, stamp, threadMarkdown, tidyTyping, typingUnit, when,
} from "./research.js";

const DOC = [
  "<!DOCTYPE html>", '<html lang="zh-CN">', "<head>", "  <style>", "    :root { --a: 1; }", "", "    * {", "      box-sizing: border-box;", "    }", "  </style>", "</head>", "<body>", "  <script>",
  "    const k = `${r},${c}`;", '    const s = "</html>"; // 字符串里的', "    const p = a ** b * c;", "  </script>", "</body>", "</html>",
].join("\n");
const HALF = DOC.split("\n").slice(0, 9).join("\n"); // 写到一半：还没到 </html>

describe("fenceHtml：没加围栏的整份 HTML 补上围栏", () => {
  it("整份的前后各补一行", () => {
    expect(fenceHtml(DOC)).toBe("```html\n" + DOC + "\n```");
  });
  it("补过的再来一遍不变", () => {
    expect(fenceHtml(fenceHtml(DOC))).toBe(fenceHtml(DOC));
  });
  it("已经在围栏里的不动", () => {
    const fenced = "好的：\n```html\n" + DOC + "\n```\n以上。";
    expect(fenceHtml(fenced)).toBe(fenced);
    const tilde = "~~~\n" + DOC + "\n~~~";
    expect(fenceHtml(tilde)).toBe(tilde);
  });
  it("前后有话的只包中间那份", () => {
    expect(fenceHtml("代码如下：\n" + DOC + "\n保存成 .html 即可。")).toBe("代码如下：\n```html\n" + DOC + "\n```\n保存成 .html 即可。");
  });
  it("还没写到 </html> 的算到末尾，不收尾", () => {
    expect(fenceHtml(HALF)).toBe("```html\n" + HALF);
  });
  it("里面有单独一行的 ``` 时，围栏比它长", () => {
    const ticks = DOC.replace("    const p = a ** b * c;", "    const md = `\n```\n`;");
    expect(fenceHtml(ticks)).toBe("````html\n" + ticks + "\n````");
  });
  it("</body></html> 写在一行的也认", () => {
    const oneEnd = DOC.replace("</body>\n</html>", "</body></html>");
    expect(fenceHtml(oneEnd + "\n后面的话")).toBe("```html\n" + oneEnd + "\n```\n后面的话");
  });
  it("整份只有一行", () => {
    expect(fenceHtml("<!DOCTYPE html><html><body>hi</body></html>")).toBe("```html\n<!DOCTYPE html><html><body>hi</body></html>\n```");
  });
  it("</html> 后面同一行还跟着字", () => {
    expect(fenceHtml("<!doctype html>\n<p>x</p>\n</html> 以上")).toBe("```html\n<!doctype html>\n<p>x</p>\n</html> 以上\n```");
  });
  it("没有声明、声明不在行首的原样", () => {
    const plain = "普通回答 **加粗** 和 `<div>`\n\n<div>x</div>";
    expect(fenceHtml(plain)).toBe(plain);
    expect(fenceHtml("说到 <!DOCTYPE html> 这个声明")).toBe("说到 <!DOCTYPE html> 这个声明");
    expect(fenceHtml("`<!DOCTYPE html>` 是声明")).toBe("`<!DOCTYPE html>` 是声明");
  });
  it("两份各包各的", () => {
    expect(fenceHtml(DOC + "\n\n第二份：\n" + DOC)).toBe("```html\n" + DOC + "\n```\n\n第二份：\n```html\n" + DOC + "\n```");
  });
});

describe("tidyTyping：正在写的最后一行", () => {
  it("没配对的 ** 和 ` 先补上", () => {
    expect(tidyTyping("这是 **加粗")).toBe("这是 **加粗**");
    expect(tidyTyping("这是 **加粗*")).toBe("这是 **加粗**"); // 收尾的 ** 只来了一半
    expect(tidyTyping("用 `code")).toBe("用 `code`");
  });
  it("刚出来的半个符号先不显示", () => {
    expect(tidyTyping("这是 **")).toBe("这是 ");
    expect(tidyTyping("用 `")).toBe("用 ");
  });
  it("只有 #、列表符号、一两个 ` 的行先不显示", () => {
    expect(tidyTyping("前文\n##")).toBe("前文\n");
    expect(tidyTyping("前文\n-")).toBe("前文\n");
    expect(tidyTyping("前文\n1.")).toBe("前文\n");
    expect(tidyTyping("代码：\n``")).toBe("代码：\n");
  });
  it("写到一半的链接先只显示文字", () => {
    expect(tidyTyping("见 [文档")).toBe("见 文档");
    expect(tidyTyping("见 [文档]")).toBe("见 文档");
    expect(tidyTyping("见 [文档](https://exa")).toBe("见 文档");
  });
  it("写完的引用不动，半截的引用去掉", () => {
    expect(tidyTyping("结论[s3fa9c1]")).toBe("结论[s3fa9c1]");
    expect(tidyTyping("结论[s3f")).toBe("结论");
  });
  it("表头一出来就补一行分隔行", () => {
    expect(tidyTyping("| 列 A | 列 B |")).toBe("| 列 A | 列 B |\n| --- |");
    expect(tidyTyping("| A | B |\n| --")).toBe("| A | B |\n| --- |"); // 分隔行还在写
  });
  it("分隔行写完了的表格不动", () => {
    const t = "| A | B |\n| --- | --- |\n";
    expect(tidyTyping(t)).toBe(t);
    expect(tidyTyping(t + "| 1 | 2 |")).toBe(t + "| 1 | 2 |");
  });
  it("代码块里的、围栏那一行原样", () => {
    expect(tidyTyping("```js\nconst a = `x")).toBe("```js\nconst a = `x");
    expect(tidyTyping("代码：\n```")).toBe("代码：\n```");
    expect(tidyTyping("```\ncode\n```\n然后 **粗")).toBe("```\ncode\n```\n然后 **粗**"); // 代码块收了以后照常
  });
  it("没加围栏的整份 HTML 也算代码：最后一行半截的 ` ** [ 不收拾", () => {
    for (const last of ["    const k = `${r}", "    const p = a **", "    const arr = [1, 2", "    x = a *"]) {
      const t = HALF + "\n" + last;
      expect(tidyTyping(t)).toBe("```html\n" + t);
    }
  });
});

describe("typingUnit：逐字出现时整块跳过的", () => {
  it("引用进了一个字就整个跳过，还没到的不跳", () => {
    const t = "结论[s3fa9c1]。";
    expect(typingUnit(t, 3)).toBe(11);
    expect(typingUnit(t, 7)).toBe(11); // 停在引用中间的也一样
    expect(typingUnit(t, 2)).toBe(2);
  });
  it("链接的网址整个跳过", () => {
    const t = "见[文档](https://example.com/a)后";
    expect(typingUnit(t, 5)).toBe(t.indexOf("后"));
  });
  it("表格的分隔行连换行一起跳过", () => {
    const t = "| A | B |\n| --- | --- |\n| 1 | 2 |";
    expect(typingUnit(t, 11)).toBe(t.indexOf("| 1"));
    expect(typingUnit(t, t.indexOf("| 1") + 1)).toBe(t.indexOf("| 1") + 1); // 数据行照常一个字一个字放
  });
  it("换行、行首的列表符号和 #、** 跟着跳过", () => {
    expect(typingUnit("前文\n- 项目", 2)).toBe(5);
    expect(typingUnit("## 标题", 0)).toBe(3);
    expect(typingUnit("这是**粗**", 2)).toBe(4);
  });
});

describe("引用和围栏", () => {
  it("cutPartialCite 只去掉末尾半截的引用", () => {
    expect(cutPartialCite("结论[s3fa")).toBe("结论");
    expect(cutPartialCite("结论[")).toBe("结论");
    expect(cutPartialCite("结论[s3fa9c1]")).toBe("结论[s3fa9c1]");
    expect(cutPartialCite("数组 a[i")).toBe("数组 a[i");
  });
  it("citeNumbers 按第一次出现的先后编号，不认识的 id 不编", () => {
    const nums = citeNumbers(["甲[saaaaaa]乙[sbbbbbb]", "丙[saaaaaa][scccccc]"], { saaaaaa: {}, sbbbbbb: {} });
    expect(nums).toEqual({ saaaaaa: "1", sbbbbbb: "2" });
  });
  it("closesFence：同一种符号、不比开头短、后面没有别的字", () => {
    expect(closesFence("```", "```")).toBe(true);
    expect(closesFence("````", "```")).toBe(true);
    expect(closesFence("  ```  ", "```")).toBe(true);
    expect(closesFence("```", "````")).toBe(false);
    expect(closesFence("~~~", "```")).toBe(false);
    expect(closesFence("```js", "```")).toBe(false);
  });
});

const text = (id, t) => ({ type: "text", id, text: t });
const step = (id, more = {}) => ({ type: "step", id, ...more });

describe("一轮的条目", () => {
  it("splitTurn：最后一步之前的都是过程，之后的文字是正文", () => {
    const items = [{ type: "thinking", id: "m1" }, text("m1", "我先查一下"), step("c1"), { type: "thinking", id: "m2" }, text("m2", "答案")];
    const { process, answer } = splitTurn(items);
    expect(process).toEqual(items.slice(0, 4));
    expect(answer).toEqual([items[4]]);
  });
  it("splitTurn：没有步骤时思考是过程、文字是正文；问用户和一步一样算", () => {
    expect(splitTurn([{ type: "thinking", id: "m1" }, text("m1", "答")]).answer).toEqual([text("m1", "答")]);
    const asked = [text("m1", "先问一下"), { type: "ask", id: "a1" }, text("m2", "好")];
    expect(splitTurn(asked).process).toEqual(asked.slice(0, 2));
  });
  it("splitTurn：被拦下的提示总在正文里，用户插的话总在过程里", () => {
    const items = [text("m1", "中途"), step("c1"), { type: "blocked", id: "b1" }, { type: "guide", id: "g1" }];
    const { process, answer } = splitTurn(items);
    expect(answer).toEqual([items[2]]);
    expect(process).toEqual([items[0], items[1], items[3]]);
  });

  const SOURCES = { saaaaaa: { title: "甲", url: "https://a.com/1" }, sbbbbbb: { title: "", url: "https://www.b.com/2" } };
  const CITED = [text("m1", "先查"), step("c1"), text("m2", "结论一[saaaaaa]，结论二[sbbbbbb][saaaaaa]。未知[sdddddd]")];

  it("answerText：只要正文，引用换成编号，后面列出来源", () => {
    expect(answerText(CITED, SOURCES)).toBe("结论一[1]，结论二[2][1]。未知\n\n来源：\n[1] 甲 https://a.com/1\n[2] b.com https://www.b.com/2");
  });
  it("answerText：导出时来源写成 Markdown 链接", () => {
    expect(answerText(CITED, SOURCES, { links: true })).toBe("结论一[1]，结论二[2][1]。未知\n\n来源：\n\n1. [甲](https://a.com/1)\n2. [b.com](https://www.b.com/2)");
  });
  it("answerText：没有引用的只有正文", () => {
    expect(answerText([text("m1", "  你好  ")], {})).toBe("你好");
  });

  it("threadMarkdown：每轮是用户的话加回答", () => {
    const st = {
      title: "标题", sources: {},
      turns: [
        { user: "你好\n第二行", items: [text("m1", "回答")] },
        {
          user: "看图", images: [1, 2], files: [{ name: "a.pdf" }], record: { outcome: "stopped" },
          items: [{ type: "guide", id: "g1", text: "快点" }, { type: "ask", id: "a1", question: "哪个城市？", answer: "上海" }, text("m2", "好")],
        },
        { user: "任务里那份很长的交代", scheduled: { title: "早报" }, items: [] },
      ],
    };
    expect(threadMarkdown(st)).toBe([
      "# 标题", "",
      "> 你好", "> 第二行", "", "回答", "", "---", "",
      "> 看图", "> （带了2 张图，文件 a.pdf）", ">", "> （回答过程中插话）快点", ">", "> （它问：哪个城市？）上海", "", "好", "", "（已停止）", "", "---", "",
      "> （由定时任务「早报」触发）", "", "（这一轮没有回答）", "",
    ].join("\n"));
  });
});

describe("mergeItems：存下来的叠上实时的", () => {
  it("没有实时的就是存下来的", () => {
    const saved = [text("m1", "存的")];
    expect(mergeItems(saved, null)).toBe(saved);
  });
  it("文字以存下来的为准，还在等结果的步骤用实时收到的，没存的接在后面", () => {
    const saved = [text("m1", "存的"), step("c1", { pending: true })];
    const live = { items: [text("m1", "实时的"), step("c1", { pending: false, result: "ok" }), text("m2", "新的")] };
    expect(mergeItems(saved, live)).toEqual([text("m1", "存的"), step("c1", { pending: false, result: "ok" }), text("m2", "新的")]);
  });
  it("存下来已经有结果的步骤不换", () => {
    const saved = [step("c1", { result: "存的" })];
    expect(mergeItems(saved, { items: [step("c1", { result: "实时的" })] })).toEqual(saved);
  });
  it("重新回答时旧回答的条目先不显示", () => {
    const saved = [text("m1", "旧的"), text("m2", "留着")];
    expect(mergeItems(saved, { items: [], hidden: new Set(["text:m1"]) })).toEqual([text("m2", "留着")]);
  });
});

describe("addLive：实时事件变成条目", () => {
  const start = { items: [] };
  it("没有这次执行的不管", () => {
    expect(addLive(null, "delta", { kind: "text", id: "m1", text: "x" })).toBe(null);
  });
  it("同一条消息的片段接在一起，原来的不改", () => {
    const a = addLive(start, "delta", { kind: "thinking", id: "m1", text: "想" });
    const b = addLive(a, "delta", { kind: "thinking", id: "m1", text: "一下" });
    expect(start.items).toEqual([]);
    expect(a.items[0]).toMatchObject({ type: "thinking", id: "m1", text: "想", live: true });
    expect(b.items).toHaveLength(1);
    expect(b.items[0].text).toBe("想一下");
    expect(b.items[0].ms).toBeUndefined();
  });
  it("正文一开始，前面的思考就算想完了", () => {
    const a = addLive(start, "delta", { kind: "thinking", id: "m1", text: "想" });
    const b = addLive(a, "delta", { kind: "text", id: "m1", text: "答" });
    expect(b.items.map((i) => i.type)).toEqual(["thinking", "text"]);
    expect(b.items[0].ms).toBeGreaterThanOrEqual(0);
  });
  it("思考的用时以后端给的为准", () => {
    const a = addLive(start, "delta", { kind: "thinking", id: "m1", text: "想", ms: 1234 });
    const b = addLive(a, "delta", { kind: "text", id: "m1", text: "答" });
    expect(b.items[0].ms).toBe(1234);
  });
  it("同一步后到的换掉先到的，带来的来源记下", () => {
    const a = addLive({ items: [], sources: { saaaaaa: 1 } }, "step", { step: { id: "c1", kind: "search", pending: true } });
    const b = addLive(a, "step", { step: { id: "c1", kind: "search", result: "ok" }, sources: { sbbbbbb: 2 } });
    expect(b.items).toEqual([{ type: "step", id: "c1", kind: "search", result: "ok" }]);
    expect(b.sources).toEqual({ saaaaaa: 1, sbbbbbb: 2 });
  });
  it("正在写的命令先占一步，写完了换成那一步", () => {
    const a = addLive(start, "writing", { id: "c1", mid: "m1", text: "ls" });
    const b = addLive(a, "writing", { id: "c1", text: " -la" });
    expect(b.items).toEqual([{ type: "step", kind: "command", id: "c1", mid: "m1", text: "ls -la", pending: true, writing: true }]);
    const c = addLive(b, "step", { step: { id: "c1", kind: "command", text: "ls -la" } });
    expect(c.items).toEqual([{ type: "step", id: "c1", kind: "command", text: "ls -la" }]);
  });
  it("写完的那一步编号对不上时，按先后换掉正在写的那条", () => {
    const a = addLive(start, "writing", { id: "call_0", mid: "m1", text: "ls" });
    const b = addLive(a, "step", { step: { id: "c9", kind: "command", text: "ls" } });
    expect(b.items).toEqual([{ type: "step", id: "c9", kind: "command", text: "ls" }]);
  });
  it("模型重新生成：刚才那条消息流出来的半截不要了", () => {
    const l = {
      items: [text("m0", "上一条"), { type: "thinking", id: "m1", text: "半截" }, text("m1", "半截"), step("c1", { writing: true, mid: "m1" })],
      drafts: { d1: { mid: "m1" }, d0: { mid: "m0" } },
    };
    const r = addLive(l, "restart", { id: "m1" });
    expect(r.items).toEqual([text("m0", "上一条")]);
    expect(r.drafts).toEqual({ d0: { mid: "m0" } });
    expect(r.phase).toBe("restart");
    expect(addLive(l, "restart", { id: null }).items).toEqual(l.items); // 没流出东西
  });
  it("正在写的文件按调用 id 攒着", () => {
    const a = addLive(start, "draft", { id: "d1", name: "create_file", mid: "m1", text: "<!DOC" });
    const b = addLive(a, "draft", { id: "d1", text: "TYPE" });
    const c = addLive(b, "draft", { id: "d1", title: "计数器", type: "html" });
    expect(c.drafts.d1).toMatchObject({ id: "d1", name: "create_file", mid: "m1", text: "<!DOCTYPE", title: "计数器", type: "html" });
  });
  it("子 agent 的放进派它的那个子任务里", () => {
    const a = addLive(start, "delta", { task: "t1", kind: "text", id: "s1", text: "子" });
    expect(a.items).toHaveLength(1);
    expect(a.items[0]).toMatchObject({ type: "step", kind: "task", id: "t1", pending: true });
    expect(a.items[0].items[0]).toMatchObject({ type: "text", id: "s1", text: "子" });
    const b = addLive(a, "phase", { task: "t1", phase: "waiting" });
    expect(b.items[0].phase).toBe("waiting");
  });
  it("阶段、模型记在这次执行上；不认识的事件不管", () => {
    expect(addLive(start, "phase", { phase: "waiting" }).phase).toBe("waiting");
    expect(addLive(start, "model", { ref: "deepseek:v4-pro" }).model).toBe("deepseek:v4-pro");
    expect(addLive(start, "别的", {})).toBe(start);
  });
});

describe("时间", () => {
  const at = (...a) => new Date(...a).toISOString();
  const NOW = new Date(2026, 10, 13, 15, 0, 0).getTime(); // 2026-11-13 15:00

  it("stamp：今天只写钟点，昨天、明天带上，今年别的天写月日，往年不写钟点", () => {
    expect(stamp(at(2026, 10, 13, 14, 32, 5), NOW)).toEqual({ day: "", hm: "14:32", text: "14:32", full: "2026年11月13日 14:32:05" });
    expect(stamp(at(2026, 10, 12, 20, 53), NOW).text).toBe("昨天 20:53");
    expect(stamp(at(2026, 10, 14, 3, 0), NOW).text).toBe("明天 03:00");
    expect(stamp(at(2026, 10, 5, 17, 1), NOW).text).toBe("11月5日 17:01");
    expect(stamp(at(2025, 10, 5, 14, 32), NOW).text).toBe("2025年11月5日");
    expect(stamp(null, NOW)).toBe(null);
  });
  it("stamp：跨年的昨天还是写昨天", () => {
    expect(stamp(at(2026, 11, 31, 23, 0), new Date(2027, 0, 1, 10, 0).getTime()).text).toBe("昨天 23:00");
  });
  it("when：今天、昨天两组里只写钟点，别的只写哪天", () => {
    expect(when(at(2026, 10, 13, 14, 32), NOW, "today")).toBe("14:32");
    expect(when(at(2026, 10, 12, 20, 53), NOW, "yesterday")).toBe("20:53");
    expect(when(at(2026, 10, 5, 17, 1), NOW, "earlier")).toBe("11月5日");
    expect(when(at(2026, 10, 13, 14, 32), NOW)).toBe("今天");
    expect(when(null, NOW)).toBe("");
  });
  it("sayWhen：今天的也说出“今天”", () => {
    expect(sayWhen(stamp(at(2026, 10, 13, 3, 0), NOW))).toBe("今天 03:00");
    expect(sayWhen(stamp(at(2026, 10, 12, 3, 0), NOW))).toBe("昨天 03:00");
  });
  it("elapsed：不到一分钟写秒，等用户回答的那段不算", () => {
    const since = "2026-11-13T00:00:00Z";
    const t0 = Date.parse(since);
    expect(elapsed(null, t0)).toBe("");
    expect(elapsed(since, t0 + 5000)).toBe("5 秒");
    expect(elapsed(since, t0 + 125000)).toBe("2 分 05 秒");
    expect(elapsed(since, t0 + 125000, 65000)).toBe("1 分 00 秒");
    expect(elapsed(since, t0 - 3000)).toBe("0 秒");
  });
});

describe("额度和模型的写法", () => {
  it("creditsText：取整、千位加逗号，不到 1 单独说", () => {
    expect(creditsText(null)).toBe(null);
    expect(creditsText(0.4)).toBe("不到 1 额度");
    expect(creditsText(1)).toBe("1 额度");
    expect(creditsText(3249.4)).toBe("3,249 额度");
  });
  it("creditsLeft：只给最紧的那个窗口", () => {
    const day = { kind: "day", limit: 10000, used: 2700 };
    const week = { kind: "week", limit: 50000, used: 45000 };
    const guests = { kind: "guests", limit: 100, used: 100 };
    expect(creditsLeft({ windows: [day, guests] })).toMatchObject({ kind: "day", remaining: 7300, exhausted: false, warn: false, span: "今天" });
    expect(creditsLeft({ windows: [day, week] })).toMatchObject({ kind: "week", remaining: 5000, warn: true, span: "这周", resetText: "下周一 0 点恢复" });
    expect(creditsLeft({ windows: [day], warn_at: 0.2 }).warn).toBe(true);
  });
  it("creditsLeft：没取到、不限额的是 null", () => {
    expect(creditsLeft(null)).toBe(null);
    expect(creditsLeft({ windows: [{ kind: "day", limit: null, used: 5 }] })).toBe(null);
  });
  it("creditsLine：剩多少、什么时候恢复", () => {
    expect(creditsLine(creditsLeft({ windows: [{ kind: "day", limit: 10000, used: 2700 }] }))).toBe("今天还剩 7,300 额度 · 明天 0 点恢复");
    expect(creditsLine(creditsLeft({ windows: [{ kind: "day", limit: 10000, used: 10400 }] }))).toBe("今天的额度用完了 · 明天 0 点恢复");
    expect(creditsLine(null)).toBe(null);
  });
  it("loginLine：游客登录后有多少", () => {
    expect(loginLine({ login: { day: 10000 } })).toBe("登录后每天 10,000 额度");
    expect(loginLine({ login: { day: 10000 } }, false)).toBe("后每天 10,000 额度");
    expect(loginLine({})).toBe(null);
  });
  it("modelName：跳过第一个（自动），不再提供的不露代号", () => {
    const models = [{ name: "自动", model: "deepseek:v4-pro" }, { name: "DeepSeek V4 Pro", model: "deepseek:v4-pro" }];
    expect(modelName(models, "deepseek:v4-pro")).toBe("DeepSeek V4 Pro");
    expect(modelName(models, "old:model")).toBe("已不再提供的模型");
    expect(modelName([], "deepseek:v4-pro")).toBe(null);
  });
});

describe("来源的写法", () => {
  it("domain 去掉 www，不是网址的原样", () => {
    expect(domain("https://www.example.com/a?b=1")).toBe("example.com");
    expect(domain("不是网址")).toBe("不是网址");
  });
  it("sourceName 没标题的用域名", () => {
    expect(sourceName({ title: "", url: "https://www.a.com/x" })).toBe("a.com");
  });
  it("sourceLine：网页带网址，资料库的写明是资料库", () => {
    const s = { title: "A [1]", url: "https://a.com" };
    expect(sourceLine(s)).toBe("A [1] https://a.com");
    expect(sourceLine(s, true)).toBe("[A 1](https://a.com)");
    expect(sourceLine({ excerpt_kind: "knowledge", title: "手册.pdf", url: "kb://f1/3" }, true)).toBe("手册.pdf（资料库）");
  });
});
