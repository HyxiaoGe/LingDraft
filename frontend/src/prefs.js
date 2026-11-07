// 设置窗口里存在这台设备上的两项（2026-11-07 Sean 定）：主题、新对话默认开不开深度思考。存在 localStorage，换设备、换浏览器不跟着走；
// 存不了（无痕、禁用存储）就按默认，只管这一次打开的页面
import { useEffect, useState } from "react";

const THEME_KEY = "lingdraft.theme"; // "light" | "dark"；跟随系统不存
const THINK_KEY = "lingdraft.think"; // 默认关时存 "off"；默认开不存

const read = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const write = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* 存不下就只管这一次 */ } };

export const THEMES = [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]];
const savedTheme = () => { const v = read(THEME_KEY); return v === "light" || v === "dark" ? v : "system"; };
const saveTheme = (v) => write(THEME_KEY, v === "system" ? null : v);
export const thinkDefault = () => read(THINK_KEY) !== "off";
export const saveThinkDefault = (on) => write(THINK_KEY, on ? null : "off");

// 主题：返回 [选的那档, 改的函数, 现在是不是深色]。跟随系统时系统一变跟着变；别的标签页里改了，这里也跟着变
export function useTheme() {
  const [theme, setTheme] = useState(savedTheme);
  const [sysDark, setSysDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches);
  useEffect(() => {
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const sys = () => setSysDark(mq.matches);
    const other = (e) => { if (e.key === THEME_KEY || e.key === null) setTheme(savedTheme()); };
    mq.addEventListener("change", sys);
    addEventListener("storage", other);
    return () => {
      mq.removeEventListener("change", sys);
      removeEventListener("storage", other);
    };
  }, []);
  const choose = (v) => {
    saveTheme(v);
    setTheme(v);
  };
  return [theme, choose, theme === "dark" || (theme === "system" && sysDark)];
}
