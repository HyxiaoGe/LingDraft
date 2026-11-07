// 代码上色：lowlight（也就是 highlight.js），只登记常用的二十几种语言，别名（js、py、sh、html、yml……）各语言自己带着。
// 回答里有标了语言的代码块才取（见 markdown.jsx 的 useLazy）；认识的语言才上色，不认识、没写语言的原样显示。
// 不用 rehype-highlight：它会把 lowlight 的三十多种“常用语言”都打进包里
import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import scss from "highlight.js/lib/languages/scss";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const LANGUAGES = { bash, c, cpp, csharp, css, diff, dockerfile, go, java, javascript, json, kotlin, markdown, php, python, ruby, rust, scss, shell, sql, swift, typescript, xml, yaml };
const lowlight = createLowlight(LANGUAGES);
const text = (node) => (node.type === "text" ? node.value : (node.children || []).map(text).join(""));

export default function rehypeCode() {
  const walk = (node) => {
    for (const ch of node.children || []) {
      if (ch.tagName !== "pre") { walk(ch); continue; }
      const code = ch.children?.find((x) => x.tagName === "code");
      const cls = (code?.properties?.className || []).map(String);
      const lang = (cls.find((x) => x.startsWith("language-")) || "").slice(9).toLowerCase();
      if (!code || !lang || !lowlight.registered(lang)) continue;
      try {
        code.children = lowlight.highlight(lang, text(code)).children;
        code.properties.className = [...cls, "hljs"];
      } catch { /* 上色出错就原样显示 */ }
    }
  };
  return walk;
}
