// Esc 一键一义（批次 G 第 34 条）：开着的一层层东西（菜单、底部弹层、弹窗、来源卡、看大图、手机抽屉、说明浮层）
// 按打开的先后叠起来，Esc 只收最上面那一层；一层都没开时，Esc 才归输入框（光标在输入框里或没在哪儿时停止回答，见 timeline.jsx 的 Composer）。
// 每层自己管关上以后焦点去哪（回到打开它的按钮），这里只管谁先关。
// 光标在自己处理 Esc 的输入框里（改名、改最后一句、改记忆，标 data-esc）时，Esc 归那个框：先取消编辑，再按一下才收外面那层。
import { useEffect, useRef } from "react";

const stack = [];

// 比各层、输入框自己的监听都早：模块一加载就挂在 window 的捕获阶段上
addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.isComposing || !stack.length) return;
  if (e.target?.closest?.("[data-esc]")) return;
  e.preventDefault();
  e.stopImmediatePropagation(); // 后面的（别的层、停止回答）都不再收到
  stack.at(-1).current(); // 这一层这会儿不让关（登录弹窗正在换登录）就什么也不做
}, true);

// on 为真时这一层算开着；close 是按 Esc 时调的
export function useLayer(on, close) {
  const ref = useRef(close);
  ref.current = close;
  useEffect(() => {
    if (!on) return undefined;
    stack.push(ref);
    return () => {
      const i = stack.lastIndexOf(ref);
      if (i >= 0) stack.splice(i, 1);
    };
  }, [on]);
}

// 打开一层时记下“原来的焦点”，关上以后回去。Safari 点按钮不给按钮焦点（焦点还在页面上），这时用刚按下的那个按钮（待确认第 24 条）
let pressed = null;
let pressedAt = 0;
addEventListener("pointerdown", (e) => {
  pressed = e.target?.closest?.("button, a[href], [tabindex]") || null;
  pressedAt = Date.now();
}, true);
export function opener() {
  const a = document.activeElement;
  if (a && a !== document.body) return a;
  return pressed?.isConnected && Date.now() - pressedAt < 2000 ? pressed : null; // 很早以前按的不算：这层是用快捷键开的
}

// 大窗口（设置）里 Tab 每一下都自己挪，只在窗口里转（和登录窗口一样：Safari 默认 Tab 跳过按钮）。box 是窗口那个元素
const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
export function trapTab(e, box) {
  if (e.key !== "Tab" || !box) return;
  const els = [...box.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
  if (!els.length) return;
  e.preventDefault();
  const i = els.indexOf(document.activeElement);
  const n = els.length;
  els[i < 0 ? (e.shiftKey ? n - 1 : 0) : (i + (e.shiftKey ? n - 1 : 1)) % n].focus();
}
