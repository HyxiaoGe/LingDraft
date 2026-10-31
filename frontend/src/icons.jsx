// 描边图标：跟随文字颜色（currentColor），尺寸按 size
import React from "react";

const svg = (paths, { size = 16, stroke = 2, className, title } = {}) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke}
    strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
    {title && <title>{title}</title>}
    {paths}
  </svg>
);

export const IconPlus = (p) => svg(<path d="M12 5v14M5 12h14" />, p);
export const IconSearch = (p) => svg(<><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.5-4.5" /></>, p);
export const IconPage = (p) => svg(<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></>, p);
export const IconLoop = (p) => svg(<><path d="M4 12a8 8 0 0 1 14-5.3L20 9" /><path d="M20 4v5h-5" /><path d="M20 12a8 8 0 0 1-14 5.3L4 15" /><path d="M4 20v-5h5" /></>, p);
export const IconWarn = (p) => svg(<><path d="M12 4l9 16H3z" /><path d="M12 10v4M12 17v.01" /></>, p);
export const IconChevron = (p) => svg(<path d="M6 9l6 6 6-6" />, p);
export const IconArrowUp = (p) => svg(<path d="M12 19V5M5 12l7-7 7 7" />, { stroke: 2.4, ...p });
export const IconArrowDown = (p) => svg(<path d="M12 5v14M5 12l7 7 7-7" />, p);
export const IconSparkle = (p) => svg(<><path d="M11 3.5l1.9 5.6 5.6 1.9-5.6 1.9L11 18.5l-1.9-5.6-5.6-1.9 5.6-1.9z" /><path d="M19 15.5v4M17 17.5h4" /></>, p);
export const IconExternal = (p) => svg(<path d="M7 17L17 7M9 7h8v8" />, p);
export const IconRetry = (p) => svg(<><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.5" /><path d="M4 4v4.5h4.5" /></>, p);
export const IconStop = (p) => svg(<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />, p);
export const IconCopy = (p) => svg(<><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h8" /></>, p);
export const IconCheck = (p) => svg(<path d="M5 12.5l4.5 4.5L19 7.5" />, p);
export const IconEdit = (p) => svg(<><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" /></>, p);
export const IconTrash = (p) => svg(<><path d="M4 7h16M10 11v6M14 11v6" /><path d="M6 7l1 13h10l1-13M9 7V4h6v3" /></>, p);
export const IconDownload = (p) => svg(<><path d="M12 4v11M7 10l5 5 5-5" /><path d="M5 20h14" /></>, p);
export const IconMore = (p) => svg(<><circle cx="5.5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="18.5" cy="12" r="1" fill="currentColor" /></>, p);

// 转圈：正在进行的步骤
export const Spinner = ({ size = 16, className = "" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={"spin " + className} aria-hidden="true">
    <circle cx="12" cy="12" r="8" stroke="currentColor" strokeOpacity="0.2" strokeWidth="2.4" />
    <path d="M12 4a8 8 0 0 1 8 8" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
  </svg>
);
