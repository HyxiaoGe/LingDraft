// 代码上色（用到才取，见 lazy.js）：照 Fusion，highlight.js 自带的常用语言（36 种）再加 Dockerfile、Nginx、PowerShell。
// 认得的语言按语言上色，不认得的让它自己猜；返回的是转义过的 HTML（highlight.js 自己转义，模型写的标签不会被当成标签）
import hljs from "highlight.js/lib/common";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import nginx from "highlight.js/lib/languages/nginx";
import powershell from "highlight.js/lib/languages/powershell";

hljs.registerLanguage("dockerfile", dockerfile);
hljs.registerLanguage("nginx", nginx);
hljs.registerLanguage("powershell", powershell);

const ALIAS = { vue: "xml", svelte: "xml", docker: "dockerfile" };

export default function highlight(value, lang) {
  const name = ALIAS[lang] || lang;
  try {
    return hljs.getLanguage(name) ? hljs.highlight(value, { language: name, ignoreIllegals: true }).value : hljs.highlightAuto(value).value;
  } catch {
    return null; // 上色出错就原样显示
  }
}
