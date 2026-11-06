// 按地址栏选场景：#poster 是海报（单栏对话），/auth/callback 是登录回跳页（见 login.jsx），其余是对话页（/chat/new 新对话，/chat/<线程 id> 打开某个对话）
import React, { useEffect, useState } from "react";
import { CALLBACK_PATH } from "./auth.js";
import { AuthCallbackPage } from "./login.jsx";
import PosterApp from "./poster.jsx";
import Workspace from "./workspace.jsx";

const viewOf = () => (location.pathname === CALLBACK_PATH ? "callback" : location.hash === "#poster" ? "poster" : "research");

export default function App() {
  const [view, setView] = useState(viewOf);
  useEffect(() => {
    const onHash = () => setView(viewOf());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  if (view === "callback") return <AuthCallbackPage />;
  return view === "poster" ? <PosterApp /> : <Workspace />;
}
