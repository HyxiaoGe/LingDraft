// 公式用的 KaTeX：页面上有公式才取（见 markdown.jsx 的 useLazy），没有公式的页面不用下载它和它的字体
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

// strict 关掉：公式里夹中文时 KaTeX 会在控制台报警告，照样能显示
export default [rehypeKatex, { strict: "ignore" }];
