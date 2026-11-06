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
// 查文档（Context7）：一本打开的书
export const IconBook = (p) => svg(<><path d="M12 7v14" /><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z" /></>, p);
export const IconLoop = (p) => svg(<><path d="M4 12a8 8 0 0 1 14-5.3L20 9" /><path d="M20 4v5h-5" /><path d="M20 12a8 8 0 0 1-14 5.3L4 15" /><path d="M4 20v-5h5" /></>, p);
export const IconWarn = (p) => svg(<><path d="M12 4l9 16H3z" /><path d="M12 10v4M12 17v.01" /></>, p);
// 天气：晴、多云（晴间多云）、阴、雨、雪、雷、雾霾沙尘
const CLOUD_TOP = "M4 14.9A7 7 0 1 1 15.7 8h1.8a4.5 4.5 0 0 1 2.5 8.24";
export const IconSun = (p) => svg(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>, p);
export const IconCloud = (p) => svg(<path d="M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 1 1 0 9z" />, p);
export const IconCloudSun = (p) => svg(<><path d="M12 2v2M4.9 4.9l1.4 1.4M20 12h2M19.1 4.9l-1.4 1.4M15.9 12.7A4 4 0 0 0 10 8.5" /><path d="M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6z" /></>, p);
export const IconRain = (p) => svg(<><path d={CLOUD_TOP} /><path d="M16 14v6M8 14v6M12 16v6" /></>, p);
export const IconSnow = (p) => svg(<><path d={CLOUD_TOP} /><path d="M8 15h.01M8 19h.01M12 17h.01M12 21h.01M16 15h.01M16 19h.01" /></>, p);
export const IconThunder = (p) => svg(<><path d="M6 16.3A7 7 0 1 1 15.7 8h1.8a4.5 4.5 0 0 1 .5 8.97" /><path d="M13 12l-3 5h4l-3 5" /></>, p);
export const IconFog = (p) => svg(<><path d={CLOUD_TOP} /><path d="M16 17H7M17 21H9" /></>, p);
// 地点、路线
export const IconPin = (p) => svg(<><path d="M12 21s-6.5-5.7-6.5-11a6.5 6.5 0 0 1 13 0c0 5.3-6.5 11-6.5 11z" /><circle cx="12" cy="10" r="2.3" /></>, p);
export const IconRoute = (p) => svg(<><circle cx="6" cy="19" r="2" /><circle cx="18" cy="5" r="2" /><path d="M8 19h8.5a3.5 3.5 0 0 0 0-7h-9a3.5 3.5 0 0 1 0-7H16" /></>, p);
// 航班、火车、行程
export const IconPlane = (p) => svg(<path d="M10.5 21l1.5-6.5L5 14l-1.5 2H2l1-4-1-4h1.5L5 10l7-.5L10.5 3H13l4.5 6.5H21a1.5 1.5 0 0 1 0 3h-3.5L13 21z" />, p);
export const IconTrain = (p) => svg(<><rect x="5" y="3" width="14" height="14" rx="3" /><path d="M5 11h14M9 21l1.5-4M15 21l-1.5-4M9 14h.01M15 14h.01" /></>, p);
export const IconTicket = (p) => svg(<><path d="M4 6h16v4a2 2 0 0 0 0 4v4H4v-4a2 2 0 0 0 0-4z" /><path d="M14 6v2M14 11v2M14 16v2" /></>, p);
export const IconChevron = (p) => svg(<path d="M6 9l6 6 6-6" />, p);
export const IconArrowUp = (p) => svg(<path d="M12 19V5M5 12l7-7 7 7" />, { stroke: 2.4, ...p });
export const IconArrowDown = (p) => svg(<path d="M12 5v14M5 12l7 7 7-7" />, p);
export const IconSparkle = (p) => svg(<><path d="M11 3.5l1.9 5.6 5.6 1.9-5.6 1.9L11 18.5l-1.9-5.6-5.6-1.9 5.6-1.9z" /><path d="M19 15.5v4M17 17.5h4" /></>, p);
export const IconExternal = (p) => svg(<path d="M7 17L17 7M9 7h8v8" />, p);
export const IconRetry = (p) => svg(<><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.5" /><path d="M4 4v4.5h4.5" /></>, p);
export const IconStop = (p) => svg(<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />, p);
export const IconCopy = (p) => svg(<><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h8" /></>, p);
export const IconBulb = (p) => svg(<><path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" /><path d="M9 18h6M10 22h4" /></>, p);
export const IconCheck = (p) => svg(<path d="M5 12.5l4.5 4.5L19 7.5" />, p);
export const IconLock = (p) => svg(<><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></>, p);
export const IconEdit = (p) => svg(<><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" /></>, p);
export const IconTrash = (p) => svg(<><path d="M4 7h16M10 11v6M14 11v6" /><path d="M6 7l1 13h10l1-13M9 7V4h6v3" /></>, p);
export const IconDownload = (p) => svg(<><path d="M12 4v11M7 10l5 5 5-5" /><path d="M5 20h14" /></>, p);
export const IconFork = (p) => svg(<><circle cx="6" cy="5" r="2" /><circle cx="18" cy="5" r="2" /><circle cx="12" cy="19" r="2" /><path d="M6 7v1a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V7M12 11v6" /></>, p);
export const IconImage = (p) => svg(<><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="M21 16l-5-5-9 9" /></>, p);
export const IconInsert = (p) => svg(<><path d="M5 4v7a4 4 0 0 0 4 4h11" /><path d="M15 10l5 5-5 5" /></>, p);
export const IconUndo = (p) => svg(<><path d="M9 14L4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></>, p);
export const IconClose = (p) => svg(<path d="M6 6l12 12M18 6L6 18" />, p);
export const IconMore = (p) => svg(<><circle cx="5.5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="18.5" cy="12" r="1" fill="currentColor" /></>, p);

// 转圈：正在进行的步骤
export const Spinner = ({ size = 16, className = "" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={"spin " + className} aria-hidden="true">
    <circle cx="12" cy="12" r="8" stroke="currentColor" strokeOpacity="0.2" strokeWidth="2.4" />
    <path d="M12 4a8 8 0 0 1 8 8" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
  </svg>
);
