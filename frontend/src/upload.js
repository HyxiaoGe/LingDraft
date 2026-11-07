// 对话里带的图：后端收 PNG / JPEG / WebP、不超过 4 MB（image-service 的上限，见 backend/app/tools/gateway.py 的 MAX_UPLOAD_BYTES）。
// 超过 4 MB 的、别的格式浏览器读得出来的（iPhone 的 HEIC 在 Safari 里、GIF 取第一帧等），先在本机缩一缩、转成 WebP（不支持的浏览器转 JPEG）再传；
// 读不出来、压不下去的不传，告诉用户换一张。只传一张，模型看不见图的内容，只能拿它改图、照着画（见后端 prompts 的 tools.generate_image.attached）
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
