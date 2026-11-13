// 给记着渲染结果的组件（memo：一轮、左栏、输入框）用的两样。
// 出字时对话页每秒要重新渲染十来次，那几个组件拿到的东西没变就不跟着渲染；回调每次渲染都是新的函数，直接传过去等于每次都变了。
import { useRef, useState } from "react";

// 一组回调（键是固定的）换成一份不变的：调的时候用的是最近一次渲染给的那个，所以里面读到的状态总是新的
export function useLatest(fns) {
  const ref = useRef(fns);
  ref.current = fns;
  const [stable] = useState(() => Object.fromEntries(Object.keys(fns).map((k) => [k, (...a) => ref.current[k](...a)])));
  return stable;
}

// 单个回调，同上
export function useEvent(fn) {
  const ref = useRef(fn);
  ref.current = fn;
  const [stable] = useState(() => (...a) => ref.current(...a));
  return stable;
}

// 每次渲染都重新算出来的值（一个新对象），内容没变就还用上一次那个：sig 是它的内容写成的字符串
export function useKept(value, sig) {
  const ref = useRef({ value, sig });
  if (ref.current.sig !== sig) ref.current = { value, sig };
  return ref.current.value;
}
