// 右栏把网页当页面看时（artifact.jsx 里模型写的，workspace-files.jsx 里发的、命令做出来的），页面关在 sandbox 的 iframe 里、没有
// allow-same-origin：碰不到灵稿这边的登录信息，但页面自己一读 localStorage / sessionStorage 浏览器就抛 SecurityError。存进度的小游戏、
// 记状态的小工具在预览里要么提示“存储不可用”，要么脚本直接断在那一行。所以预览时在页面最前面放一小段脚本：读不到的这两个换成只在内存里的，
// 用法和真的一样（getItem / setItem / removeItem / clear / key / length，也能当对象的属性读写、列出键）。
// 只在这次预览里有：关掉右栏、切到源码再切回来、换一版，存的就没了。隔离不放开；下载、复制、看源码拿到的还是原文，不带这一段。

// 写成字符串：要原样放进页面里跑，不能经过打包（preview.test.js 里拿这个字符串在假的 window 上跑）。里面不能出现 </script
export const STORAGE_SHIM = `(function () {
  function memory() {
    var data = new Map();
    var api = {
      getItem: function (k) { k = String(k); return data.has(k) ? data.get(k) : null; },
      setItem: function (k, v) { data.set(String(k), String(v)); },
      removeItem: function (k) { data.delete(String(k)); },
      clear: function () { data.clear(); },
      key: function (i) { var ks = Array.from(data.keys()); return i >= 0 && i < ks.length ? ks[i] : null; }
    };
    return new Proxy(api, {
      get: function (t, p) { return p === "length" ? data.size : p in t ? t[p] : typeof p === "string" && data.has(p) ? data.get(p) : undefined; },
      set: function (t, p, v) { if (typeof p === "string" && p !== "length" && !(p in t)) data.set(p, String(v)); return true; },
      has: function (t, p) { return p === "length" || p in t || (typeof p === "string" && data.has(p)); },
      deleteProperty: function (t, p) { if (typeof p === "string") data.delete(p); return true; },
      ownKeys: function () { return Array.from(data.keys()); },
      getOwnPropertyDescriptor: function (t, p) {
        return typeof p === "string" && data.has(p) ? { value: data.get(p), writable: true, enumerable: true, configurable: true } : undefined;
      }
    });
  }
  ["localStorage", "sessionStorage"].forEach(function (name) {
    try { if (window[name]) return; } catch (e) { /* 读不到：下面换成内存里的 */ }
    try { Object.defineProperty(window, name, { configurable: true, value: memory() }); } catch (e) { /* 换不了的浏览器照旧 */ }
  });
})();`;

// 放在哪：页面自己的脚本之前，又不动页面的结构。文档开头的声明、<html>、<head> 这几样（中间可以有空白和注释）后面——有 <head> 就成了
// 它里面的第一个脚本，和作者自己写在那里一样；不放到 <!DOCTYPE> 前面（那样浏览器得先替页面补出 <html>、<head>，作者写的那两个标签成了多余的）。
// 这几样都没有的片段直接放最前面。
const GAP = String.raw`(?:\s|<!--[\s\S]*?-->)*`;
const tag = (name) => String.raw`(?:<${name}\b(?:[^>"']|"[^"]*"|'[^']*')*>)?`; // 属性值里的 > 不算收尾
const LEAD = new RegExp(String.raw`^(?:\s|<!--[\s\S]*?-->|<\?[^>]*>)*(?:<!doctype[^>]*>)?` + GAP + tag("html") + GAP + tag("head"), "i");

export function previewDoc(html) {
  if (typeof html !== "string") return html;
  const at = html.match(LEAD)[0].length;
  return `${html.slice(0, at)}<script>${STORAGE_SHIM}</script>${html.slice(at)}`;
}
