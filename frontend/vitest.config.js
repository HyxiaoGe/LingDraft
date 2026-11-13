// 前端的单元测试（npm test，CI 里跟检查、构建一起跑）：只测不碰页面的函数（src 下的 *.test.js），在 Node 里跑，不起浏览器。
// 单独一份配置，不读 vite.config.js：测试用不着 React、Tailwind 的插件
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["src/**/*.test.js"] },
});
