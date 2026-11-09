// 对话里带的图：后端收 PNG / JPEG / WebP、不超过 4 MB（image-service 的上限，见 backend/app/tools/gateway.py 的 MAX_UPLOAD_BYTES）。
// 超过 4 MB 的、别的格式浏览器读得出来的（iPhone 的 HEIC 在 Safari 里、GIF 取第一帧等），先在本机缩一缩、转成 WebP（不支持的浏览器转 JPEG）再传；
// 读不出来、压不下去的不传，告诉用户换一张。一句话最多 MAX_IMAGES 张（后端 history.MAX_ATTACHED）：模型能看图里的内容，也能拿它改图、照着画
// （看不见图的模型调看图工具，见后端 app/tools/vision.py）
export const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const TYPES = ["image/png", "image/jpeg", "image/webp"];
// 选图框只列这几种：iPhone 上选 HEIC 的照片时，系统会先转成 JPEG 再给页面
export const IMAGE_ACCEPT = TYPES.join(",");
const MAX_SIDE = 4096;
const MAX_PIXELS = 16_000_000;

export class ImageError extends Error {}

async function decode(file) {
  try {
    return await createImageBitmap(file);
  } catch { /* 有的浏览器 createImageBitmap 不认的格式 <img> 认（Safari 的 HEIC） */ }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } catch {
    return null;
  } finally {
    URL.revokeObjectURL(url);
  }
}

const toBlob = (canvas, type, quality) => new Promise((r) => canvas.toBlob(r, type, quality));

let webp = null;
function canWebp() {
  if (webp === null) {
    const c = document.createElement("canvas");
    c.width = c.height = 1;
    webp = c.toDataURL("image/webp").startsWith("data:image/webp");
  }
  return webp;
}

// 一边缩一边试，直到不超过 4 MB；WebP 留着透明，JPEG 没有透明，垫白底
async function shrink(src) {
  const w = src.naturalWidth || src.width;
  const h = src.naturalHeight || src.height;
  if (!w || !h) return null;
  const type = canWebp() ? "image/webp" : "image/jpeg";
  let scale = Math.min(1, MAX_SIDE / Math.max(w, h), Math.sqrt(MAX_PIXELS / (w * h)));
  for (let i = 0; i < 6; i++, scale *= 0.75) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(w * scale));
    canvas.height = Math.max(1, Math.round(h * scale));
    const ctx = canvas.getContext("2d");
    if (type === "image/jpeg") {
      ctx.fillStyle = "white";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
    const blob = await toBlob(canvas, type, 0.9);
    if (blob && blob.size <= MAX_IMAGE_BYTES) return blob;
  }
  return null;
}

// 选的、粘贴的、拖进来的文件 → 能直接传的图（File）；不行抛 ImageError，message 是给用户看的
export async function prepareImage(file) {
  if (TYPES.includes(file.type) && file.size <= MAX_IMAGE_BYTES) return file;
  if (file.type && !file.type.startsWith("image/")) throw new ImageError("只能传图片（PNG、JPEG、WebP）");
  const src = await decode(file);
  if (!src) throw new ImageError("这张图读不出来，换成 PNG、JPEG 或 WebP 再试");
  const blob = await shrink(src);
  src.close?.();
  if (!blob) throw new ImageError("这张图太大，压到 4 MB 以内没成功，换一张小一点的再试");
  const base = (file.name || "image").replace(/\.[^.]+$/, "");
  return new File([blob], `${base}.${blob.type === "image/webp" ? "webp" : "jpg"}`, { type: blob.type });
}

// 传图被后端拒（发消息时一起传的）：按状态码说人话，不直接显示后端的原话
export function uploadErrorText(status) {
  if (status === 413) return "图片超过 4 MB，没发出去";
  if (status === 415) return "这张图的格式不支持（只支持 PNG、JPEG、WebP），没发出去";
  return "图片暂时传不上去，稍后再发一次";
}

// ---- 文件（见后端 app/files）：PDF、Word、Excel、PPT、CSV、文本、Markdown、代码，一句话最多 MAX_FILES 个，每个不超过 FILE_MAX_MB。
// 认类型、解析都在后端；页面只把图片和别的文件分开（图片走上面那条路，模型直接看），再挡一下明显太大、空的 ----
export const MAX_FILES = 5;
export const FILE_MAX_MB = 20;
const FILE_MAX_BYTES = FILE_MAX_MB * 1024 * 1024;
const IMAGE_EXT = /\.(png|jpe?g|webp|gif|heic|heif|avif|bmp)$/i;
export const isImage = (f) => (f.type ? f.type.startsWith("image/") && f.type !== "image/svg+xml" : IMAGE_EXT.test(f.name || ""));

// 传之前就知道传不了的（不用等后端说）：原因同后端的 reason
export const tooBig = (f) => (f.size > FILE_MAX_BYTES ? "too_large" : f.size === 0 ? "empty_file" : null);

const SUPPORTED = "可以传 PDF、Word（docx）、Excel（xlsx）、PPT（pptx）、CSV、文本、Markdown 或代码文件";
// 文件传不了、读不了、发不出去：后端给原因（reason，见 app/files/service.py 和 parse.py 的 Unreadable），这里说人话。
// d 是 429 今天传满了时的 detail（limit、guest、login_limit）
export function fileErrorText(reason, d = {}) {
  switch (reason) {
    case "too_large": return `文件超过 ${FILE_MAX_MB} MB，传不了`;
    case "empty_file": return "文件是空的";
    case "daily_limit": return `今天传的文件到上限了（${d.limit} 个），${d.guest && d.login_limit ? `登录后每天能传 ${d.login_limit} 个` : "明天再传"}`;
    case "image": return "图片请直接作为图片发送";
    case "kb_image": return "知识库只放文档，图片放不进来";
    case "legacy_office": return "老格式的 doc、xls、ppt 和加了密码的 Office 文件读不了，另存成 docx、xlsx、pptx 再传";
    case "encrypted": return "这个 PDF 有密码，读不了";
    case "broken": return "文件打不开，可能已经损坏";
    case "scanned": return "这个 PDF 是扫描的图片，读不出文字";
    case "empty": return "文件里没有能读出来的文字";
    case "unzipped_too_large": return "文件解压后太大，读不了";
    case "unsupported": return `不支持这种文件，${SUPPORTED}`;
    case "timeout": case "killed": return "文件太复杂，读不了";
    case "expired": return "文件已经过期了";
    case "offline": return "没传上去，网络好像断了";
    case "busy": return "现在传不上去，稍后再传";
    // 知识库（见后端 app/knowledge/service.py）
    case "login_required": return "登录后才能用知识库";
    case "kb_full": return `知识库最多放 ${d.limit} 个文件，删掉一些再传`;
    case "kb_chars": return "知识库的文字到上限了，删掉一些再传";
    case "index_failed": return "没处理好，删掉重新上传试试";
    case "no_text": return "文件里没有能读出来的文字";
    default: return "文件没读成";
  }
}

// 卡片上那行短的：还没传上去的（传之前就挡下、后端不收、网络断了）和传上去了读不了的
const NOT_UPLOADED = new Set(["too_large", "empty_file", "daily_limit", "offline", "busy", "login_required", "kb_full"]);
export const fileShortError = (reason) => (NOT_UPLOADED.has(reason) ? "没传上去" : reason === "expired" ? "已经过期" : "读不了");

// 发消息时带的文件不能发（400，detail.code 是 files_not_sendable）
export function notSendableText(reason) {
  if (reason === "parsing") return "有文件还在解析";
  if (reason === "unreadable") return "有文件读不了";
  if (reason === "too_many") return `一句话最多带 ${MAX_FILES} 个文件`;
  return "有文件已经过期了";
}
