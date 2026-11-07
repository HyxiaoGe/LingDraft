// 不小的库用到才去取：公式（KaTeX，math.js）、代码上色（highlight.js，code-colors.js）。取回来之前公式和代码先原样显示，取回来再刷新一次
import { useEffect, useState } from "react";

const LAZY = { math: () => import("./math.js"), code: () => import("./code-colors.js") };
const got = {};
const loading = {};

export function useLazy(name, need) {
  const [, loaded] = useState(0);
  useEffect(() => {
    if (!need || got[name]) return undefined;
    let on = true;
    loading[name] ||= LAZY[name]().then((m) => { got[name] = m.default; }).catch(() => { loading[name] = null; });
    loading[name].then(() => on && loaded((n) => n + 1));
    return () => { on = false; };
  }, [name, need]);
  return need ? got[name] : null;
}
