// 回答里的表格（2026-11-07 Sean 定，照搬 Fusion 的 MarkdownTable）：外面一圈圆角框，格子只有横线，表头竖着滑时停在顶上；
// 放不下就在框里滑，页面不跟着滑；左右还有没露出来的列，那一边淡出；能滑的时候框下面一行说怎么滑，框能用 Tab 停上去、方向键滑
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ArrowLeftRight, ArrowUpDown } from "lucide-react";
import styles from "./md-table.module.css";

const HINT = { both: "可上下、左右滑动查看表格，表头保持可见", horizontal: "可左右滑动查看完整表格", vertical: "可上下滑动，表头保持可见" };

export function MdTable({ children }) {
  const ref = useRef(null);
  const id = useId();
  const [over, setOver] = useState({ horizontal: false, vertical: false, left: false, right: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const maxLeft = el.scrollWidth - el.clientWidth;
    const next = {
      horizontal: maxLeft > 1,
      vertical: el.scrollHeight - el.clientHeight > 1,
      left: el.scrollLeft > 1,
      right: maxLeft - el.scrollLeft > 1,
    };
    setOver((cur) => (Object.keys(next).every((k) => cur[k] === next[k]) ? cur : next));
  }, []);

  useEffect(() => {
    const el = ref.current;
    const table = el?.querySelector("table");
    if (!el || !table) return undefined;
    measure();
    // 盯着表格本身：逐字出新行、字体变了、左栏开合都会重新算
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    ro.observe(table);
    return () => ro.disconnect();
  }, [measure]);

  const scrolls = over.horizontal || over.vertical;
  const kind = over.horizontal && over.vertical ? "both" : over.horizontal ? "horizontal" : "vertical";
  return (
    <div className={"md-wide " + styles.box}>
      <div className={styles.frame}>
        <div className={styles.viewport} data-left={over.left} data-right={over.right}>
          <div ref={ref} className={styles.scroll} role="region" aria-label="表格" aria-describedby={scrolls ? id : undefined}
            tabIndex={scrolls ? 0 : undefined} onScroll={measure}>
            <table>{children}</table>
          </div>
        </div>
        {scrolls && (
          <div id={id} className={styles.hint}>
            {over.horizontal ? <ArrowLeftRight aria-hidden="true" /> : <ArrowUpDown aria-hidden="true" />}
            <span>{HINT[kind]}</span>
          </div>
        )}
      </div>
    </div>
  );
}
