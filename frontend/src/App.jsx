// 按地址栏选场景：#poster 是海报（单栏对话），其余是对话页（#r/<线程 id> 打开某个对话）
import React, { useEffect, useState } from "react";
import PosterApp from "./poster.jsx";
import Workspace from "./workspace.jsx";

const viewOf = () => (location.hash === "#poster" ? "poster" : "research");

export default function App() {
  const [view, setView] = useState(viewOf);
  useEffect(() => {
    const onHash = () => setView(viewOf());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  return view === "poster" ? <PosterApp /> : <Workspace />;
}
