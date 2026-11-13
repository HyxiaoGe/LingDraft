// preview.js：右栏预览网页时放进页面的那段脚本（内存里的 localStorage / sessionStorage），和它放在页面的哪个位置。
import vm from "node:vm";
import { describe, expect, it } from "vitest";
import { STORAGE_SHIM, previewDoc } from "./preview.js";

const TAG = `<script>${STORAGE_SHIM}</script>`;

describe("previewDoc：脚本放在页面自己的脚本之前", () => {
  it("有 <head> 的放在它里面最前面", () => {
    const html = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n<script>localStorage.getItem("a")</script>\n</head>\n<body></body>\n</html>';
    const lead = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>';
    expect(previewDoc(html)).toBe(lead + TAG + html.slice(lead.length));
  });
  it("声明前面有空白、注释的照样认，不放到声明前面", () => {
    const html = "﻿ \n<!-- 说明 -->\n<!doctype html>\n<html><body>hi</body></html>";
    const out = previewDoc(html);
    expect(out.indexOf("<script>")).toBe(html.indexOf("<html>") + "<html>".length);
    expect(out.replace(TAG, "")).toBe(html);
  });
  it("没有 <head> 的放在 <html> 后面，连 <html> 都没有的放在声明后面", () => {
    expect(previewDoc("<!doctype html><html><body>hi</body></html>")).toBe("<!doctype html><html>" + TAG + "<body>hi</body></html>");
    expect(previewDoc("<!DOCTYPE html>\n<title>t</title><p>hi</p>")).toBe("<!DOCTYPE html>\n" + TAG + "<title>t</title><p>hi</p>");
    expect(previewDoc("<!doctype html><html><header>x</header></html>")).toBe("<!doctype html><html>" + TAG + "<header>x</header></html>"); // <header> 不是 <head>
  });
  it("没有声明的片段放最前面", () => {
    expect(previewDoc("<div>hi</div>")).toBe(TAG + "<div>hi</div>");
    expect(previewDoc("")).toBe(TAG);
  });
  it("属性值里的 > 不当成标签收尾", () => {
    const html = '<!DOCTYPE html><html data-x="a>b" lang=\'zh\'><head><title>t</title></head></html>';
    const lead = '<!DOCTYPE html><html data-x="a>b" lang=\'zh\'><head>';
    expect(previewDoc(html)).toBe(lead + TAG + html.slice(lead.length));
  });
  it("正文里提到的 <head>、<!DOCTYPE> 不算", () => {
    const html = "<p>页面以 &lt;!DOCTYPE html&gt; 开头</p><pre><!DOCTYPE html><html><head></pre>";
    expect(previewDoc(html)).toBe(TAG + html);
  });
  it("不是文字的原样交回", () => {
    expect(previewDoc(null)).toBe(null);
    expect(previewDoc(undefined)).toBe(undefined);
  });
  it("脚本里没有会提前收掉 <script> 的字", () => {
    expect(STORAGE_SHIM).not.toMatch(/<\/script/i);
  });
});

// 假的 window：locked 的像 sandbox 里那样一读就抛错
function fakeWindow({ locked = true, real = null } = {}) {
  const window = {};
  for (const name of ["localStorage", "sessionStorage"]) {
    Object.defineProperty(window, name, {
      configurable: true,
      get() {
        if (locked) throw new Error("SecurityError");
        return real;
      },
    });
  }
  vm.runInNewContext(STORAGE_SHIM, { window });
  return window;
}

describe("放进页面的脚本：读不到的存储换成内存里的", () => {
  it("getItem / setItem / removeItem / clear / key / length 和真的一样", () => {
    const s = fakeWindow().localStorage;
    expect(s.getItem("best")).toBe(null);
    expect(s.length).toBe(0);
    s.setItem("best", 42);
    s.setItem("name", "灵稿");
    expect(s.getItem("best")).toBe("42"); // 存进去的都成了字符串
    expect(s.length).toBe(2);
    expect(s.key(0)).toBe("best");
    expect(s.key(2)).toBe(null);
    s.removeItem("best");
    expect(s.getItem("best")).toBe(null);
    s.clear();
    expect(s.length).toBe(0);
  });
  it("当对象的属性读写、列出键也行", () => {
    const s = fakeWindow().localStorage;
    s.theme = "dark";
    s["save:1"] = JSON.stringify({ level: 3 });
    expect(s.getItem("theme")).toBe("dark");
    expect(s.theme).toBe("dark");
    expect("theme" in s).toBe(true);
    expect("nope" in s).toBe(false);
    expect(s.nope).toBe(undefined);
    expect(Object.keys(s)).toEqual(["theme", "save:1"]);
    expect(JSON.parse(JSON.stringify(s))).toEqual({ theme: "dark", "save:1": '{"level":3}' });
    expect(Object.prototype.hasOwnProperty.call(s, "theme")).toBe(true);
    delete s.theme;
    expect(s.getItem("theme")).toBe(null);
  });
  it("存的键和方法同名时，方法还在", () => {
    const s = fakeWindow().localStorage;
    s.setItem("getItem", "x");
    s.length = 5;
    expect(typeof s.getItem).toBe("function");
    expect(s.getItem("getItem")).toBe("x");
    expect(s.length).toBe(1);
  });
  it("localStorage 和 sessionStorage 各存各的", () => {
    const w = fakeWindow();
    w.localStorage.setItem("a", "1");
    expect(w.sessionStorage.getItem("a")).toBe(null);
  });
  it("本来读得到的不换", () => {
    const real = { getItem: () => "真的" };
    const w = fakeWindow({ locked: false, real });
    expect(w.localStorage).toBe(real);
  });
});
