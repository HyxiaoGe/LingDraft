// 前端的静态检查（npm run lint，CI 里跟构建一起跑）。vite build 不查未定义的名字：
// 2026-11-09 composer.jsx 少引入 focusInput，构建、CI 都过了，上线后桌面选模型整个不生效（#112），所以加上
import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default [
  { ignores: ["dist/"] },
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    plugins: { react, "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: "18.3" } },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "react/jsx-no-undef": "error",
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",
      ...reactHooks.configs.recommended.rules,
      // 用不上的参数照常留着（组件的 props、回调的参数）；{ src, ...d } 这种是有意去掉一项
      "no-unused-vars": ["error", { args: "none", ignoreRestSiblings: true }],
      // 中文里的全角空格是有意的（正文、注释）
      "no-irregular-whitespace": ["error", { skipComments: true, skipJSXText: true, skipTemplates: true }],
      // 文件名、代码里去掉控制字符的正则就是要写 \u0000-\u001f
      "no-control-regex": "off",
    },
  },
  {
    files: ["vite.config.js", "eslint.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
];
