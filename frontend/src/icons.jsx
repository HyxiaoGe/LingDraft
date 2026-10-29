// 描边图标：跟随文字颜色（currentColor），尺寸按 size
import React from "react";

const svg = (paths, { size = 16, stroke = 2, className, title } = {}) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke}
    strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
    {title && <title>{title}</title>}
    {paths}
  </svg>
);

export const IconCheck = (p) => svg(<path d="M5 12l5 5L19 7" />, { stroke: 2.4, ...p });
export const IconPlus = (p) => svg(<path d="M12 5v14M5 12h14" />, p);
export const IconSearch = (p) => svg(<><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.5-4.5" /></>, p);
export const IconPage = (p) => svg(<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></>, p);
export const IconBook = (p) => svg(<><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" /><path d="M4 19V5" /></>, p);
export const IconAsk = (p) => svg(<><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" /><path d="M9.5 9.5a2.5 2.5 0 1 1 3.3 2.4c-.5.2-.8.6-.8 1.1v.5M12 16.5v.01" /></>, p);
export const IconLoop = (p) => svg(<><path d="M4 12a8 8 0 0 1 14-5.3L20 9" /><path d="M20 4v5h-5" /><path d="M20 12a8 8 0 0 1-14 5.3L4 15" /><path d="M4 20v-5h5" /></>, p);
export const IconLayers = (p) => svg(<><path d="M12 3l9 5-9 5-9-5z" /><path d="M3 13l9 5 9-5" /></>, p);
export const IconReport = (p) => svg(<><rect x="5" y="3" width="14" height="18" rx="2" /><path d="M9 8h6M9 12h6M9 16h4" /></>, p);
export const IconShield = (p) => svg(<><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" /><path d="M9 12l2 2 4-4" /></>, p);
export const IconWarn = (p) => svg(<><path d="M12 4l9 16H3z" /><path d="M12 10v4M12 17v.01" /></>, p);
export const IconChevron = (p) => svg(<path d="M6 9l6 6 6-6" />, p);
export const IconArrowUp = (p) => svg(<path d="M12 19V5M5 12l7-7 7 7" />, { stroke: 2.4, ...p });
export const IconExternal = (p) => svg(<path d="M7 17L17 7M9 7h8v8" />, p);
export const IconX = (p) => svg(<path d="M6 6l12 12M18 6L6 18" />, p);
export const IconEdit = (p) => svg(<><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M14 6l4 4" /></>, p);
export const IconRetry = (p) => svg(<><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.5" /><path d="M4 4v4.5h4.5" /></>, p);
export const IconSparkle = (p) => svg(<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6" />, p);

// 转圈：正在进行的步骤
export const Spinner = ({ size = 16, className = "" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={"spin " + className} aria-hidden="true">
    <circle cx="12" cy="12" r="8" stroke="currentColor" strokeOpacity="0.2" strokeWidth="2.4" />
    <path d="M12 4a8 8 0 0 1 8 8" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
  </svg>
);
