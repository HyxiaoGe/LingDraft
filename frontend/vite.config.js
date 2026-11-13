import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 解析、渲染 Markdown 的那一串包（react-markdown、remark、micromark、mdast、unified 和它们用的小件）
const MARKDOWN = /^(react-markdown|remark-.+|micromark.*|mdast-.+|unified|vfile.*|unist-.+|hast-util-to-jsx-runtime|hast-util-whitespace|property-information|style-to-(object|js)|inline-style-parser|html-url-attributes|decode-named-character-reference|character-entities.*|(space|comma)-separated-tokens|estree-util-is-identifier-name|markdown-table|trim-lines|zwitch|longest-streak|ccount|escape-string-regexp|devlop|bail|extend|is-plain-obj|trough|@ungap\/structured-clone)$/;
const MATH = /^(remark-math|micromark-extension-math|mdast-util-math)$/;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: {
    rollupOptions: {
      output: {
        // 不常变的库各自一个文件：文件名带内容哈希、浏览器一直缓存着（见后端 api/main.py 的 Pages），发新版只有我们自己的代码那个文件要重新下载。
        // 认公式的那三个包（MATH）不能放进去：其中一处引了 KaTeX（260 kB，那处代码我们没用到）。Rollup 会把一个文件里的包依赖的包也放进同一个文件，
        // 放进去 KaTeX 就跟着成了一打开就要下载的，公式就不是用到才取的了（见 src/lazy.js）；单独给 KaTeX 一个文件也不行，页面照样一打开就取它。
        // 没列到的包照 Rollup 自己的分法
        manualChunks(id) {
          const pkg = id.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/)?.[1];
          if (!pkg || MATH.test(pkg)) return undefined;
          if (/^(react|react-dom|scheduler)$/.test(pkg)) return "react";
          return MARKDOWN.test(pkg) ? "markdown" : undefined;
        },
      },
    },
  },
});
