// 取用户现在的位置（浏览器的 navigator.geolocation）。模型选了“当前位置”时后端停下来要（见后端 app.tools.location），页面取了交回去。
// 位置只交给后端用这一轮，页面不存。浏览器只在 https 和 localhost 上给位置：别的地址当取不到。
//
// 两段取法照 Fusion（frontend/src/hooks/useLocationContextHandshake.ts）：先要粗的（快，一分钟内取过的直接用），
// 超时或取不到再要准的（手机上是 GPS，慢一些）。用户或浏览器不让的不再试第二段。
const STAGES = [
  { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
  { enableHighAccuracy: true, timeout: 25000, maximumAge: 0 },
];
const PERMISSION_DENIED = 1; // GeolocationPositionError.code

const usable = () => typeof navigator !== "undefined" && !!navigator.geolocation && window.isSecureContext !== false;

// 浏览器里这个网站的定位权限："granted" 已经允许过（直接取，不用再问）、"denied" 禁了、"prompt" 还没定（取的时候浏览器会弹它自己的授权）、
// "unsupported" 取不了（浏览器不支持，或者不是 https）。查不了权限的浏览器当还没定
export async function locatePermission() {
  if (!usable()) return "unsupported";
  try {
    const status = await navigator.permissions.query({ name: "geolocation" });
    return status.state === "granted" || status.state === "denied" ? status.state : "prompt";
  } catch {
    return "prompt";
  }
}

const once = (options) => new Promise((resolve, reject) => { navigator.geolocation.getCurrentPosition(resolve, reject, options); });

// 取一次位置。返回 {location: {longitude, latitude, accuracy_m}}（WGS84，后端原样交给地图、天气服务），
// 或 {denied: true}（用户或浏览器不让），或 {unavailable: true}（取不到、超时、取不了）
export async function locate() {
  if (!usable()) return { unavailable: true };
  for (const options of STAGES) {
    try {
      const { coords } = await once(options);
      const accuracy = Number.isFinite(coords.accuracy) && coords.accuracy >= 0 ? { accuracy_m: coords.accuracy } : {};
      return { location: { longitude: coords.longitude, latitude: coords.latitude, ...accuracy } };
    } catch (e) {
      if (e?.code === PERMISSION_DENIED) return { denied: true };
    }
  }
  return { unavailable: true };
}
