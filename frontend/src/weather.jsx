// 天气卡片：get_weather 查到的预报（和风天气，agent-tools 的 weather-service，见 backend/app/tools/weather_mcp.py）。
// 上面是逐日几列（问了某一天、在预报范围内时只有那一天，排成一行；不在范围内没有卡片），下面是逐小时：没问哪天是接下来 24 小时，问了 7 天内的某天是那天的
// 气温折线和降水概率柱子分上下两条，共用时间轴，不叠在一张图上（单位不同，不用两套纵轴）
import { useLayoutEffect, useRef, useState } from "react";
import { IconCloud, IconCloudSun, IconFog, IconRain, IconSnow, IconSun, IconThunder } from "./icons.jsx";
import shell from "./card.module.css";
import { stamp } from "./research.js";
import styles from "./weather.module.css";

// 这一轮查到的天气，一个地方的一天一张（没指定哪天的算一张）：同样的查了几次，位置按第一次、内容用最后一次
export function forecastsOf(items) {
  const by = new Map();
  for (const i of items) {
    if (i.type === "step" && i.kind === "weather" && i.weather) by.set(`${i.weather.location}|${i.weather.requested_date || ""}`, i.weather);
  }
  return [...by.values()];
}

// 和风的天气现象是一套固定的说法（晴、多云、晴间多云、阴、小雨、雷阵雨、雨夹雪、雾、霾、扬沙……），按字挑图标
function skyIcon(text = "") {
  if (text.includes("雷")) return IconThunder;
  if (text.includes("雪") || text.includes("冰雹")) return IconSnow;
  if (text.includes("雨")) return IconRain;
  if (/[雾霾沙尘]/.test(text)) return IconFog;
  if (text.includes("晴")) return text.includes("云") ? IconCloudSun : IconSun;
  if (text.includes("少云")) return IconCloudSun;
  if (text.includes("云") || text.includes("阴")) return IconCloud;
  return IconCloudSun;
}

const WEEK = "日一二三四五六";
const monthDay = (date) => `${Number(date.slice(5, 7))}月${Number(date.slice(8, 10))}日`;
const nextDay = (date) => new Date(Date.parse(`${date}T00:00:00Z`) + 86400000).toISOString().slice(0, 10);
const weekday = (date) => `周${WEEK[new Date(`${date}T00:00:00`).getDay()]}`;
const dayName = (date, today) => (date === today ? "今天" : date === nextDay(today) ? "明天" : weekday(date));
// 逐小时里某个小时是哪天：今天、明天写字，再往后写日期
const dayWord = (date, today) => (date === today ? "今天" : date === nextDay(today) ? "明天" : monthDay(date));
const deg = (t) => `${Math.round(t)}°`;

export function WeatherCard({ fc }) {
  const parts = fc.location.split("·");
  const at = stamp(fc.fetched_at);
  // 问了某一天、预报里有：只有这一天，排成一行，是哪天写在标题上
  const one = fc.days.length === 1 && fc.days[0].date === fc.requested_date;
  return (
    <section className={shell.card + " " + shell.weather} aria-label={`${fc.location}的天气预报`}>
      <div className={styles.head}>
        <IconCloudSun size={15} />
        <span className={styles.place}>{parts.at(-1)}</span>
        {parts.length > 1 && <span className={styles.adm}>{parts.slice(0, -1).join("·")}</span>}
        {one && <span className={styles.date}>{dayName(fc.requested_date, fc.today)} {monthDay(fc.requested_date)}</span>}
        <span className={styles.by} title={at?.full}>和风天气{at && ` · ${at.text} 更新`}</span>
      </div>
      <div className={styles.days + (one ? " " + styles.one : "")} style={{ "--n": fc.days.length }}>
        {fc.days.map((d) => <Day key={d.date} d={d} today={fc.today} />)}
      </div>
      {fc.hourly.length > 1 && <Hourly hours={fc.hourly} today={fc.today} />}
    </section>
  );
}

function Day({ d, today }) {
  const Icon = skyIcon(d.day);
  const sky = !d.night || d.night === d.day ? d.day : `${d.day}转${d.night}`;
  return (
    <div className={styles.day + (d.date === today ? " " + styles.today : "")}>
      <div className={styles.when}><b>{dayName(d.date, today)}</b><span>{monthDay(d.date)}</span></div>
      <Icon size={22} stroke={1.6} />
      <div className={styles.sky} title={`白天${d.day}，夜间${d.night}`}>{sky}</div>
      <div className={styles.temp}><b>{deg(d.high)}</b><span>{deg(d.low)}</span></div>
      {d.wind && <div className={styles.wind}>{d.wind}</div>}
    </div>
  );
}

// 图的竖向布局（像素）：上面一条画气温，下面一条画降水概率，最下面是钟点
const PAD = 16;
const TEMP_TOP = 20;
const TEMP_BOTTOM = 62;
const BAR_TOP = 80;
const BAR_BOTTOM = 112;
const HEIGHT = 132;
const MIN_SPAN = 6; // 一天里气温差得少时，折线不要被拉成大起大落
const POP_LABEL = 30; // 降水概率到这么多才在柱子上写数

// 当地钟点：直接从时间里取，不经浏览器的时区换算
const hourOf = (t) => Number(t.slice(11, 13));
const hourName = (h, today) => `${dayWord(h.time.slice(0, 10), today)}${hourOf(h.time)}时`;
// 图上画的是哪段时间：同一天写一次是哪天，跨天两头都写
const spanName = (hours, today) => {
  const [a, b] = [hours[0].time, hours.at(-1).time];
  return a.slice(0, 10) === b.slice(0, 10)
    ? `${dayWord(a.slice(0, 10), today)} ${hourOf(a)}时–${hourOf(b)}时`
    : `${hourName(hours[0], today)}–${hourName(hours.at(-1), today)}`;
};

// 柱子：顶上两个角圆，底边贴着基线
const barPath = (x, y, w, h) => {
  const r = Math.min(3, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
};

function Hourly({ hours, today }) {
  const box = useRef(null);
  const [w, setW] = useState(0);
  const [at, setAt] = useState(null); // 正在看的那个小时：鼠标悬停、手指按着拖、键盘左右键选的
  useLayoutEffect(() => {
    const el = box.current;
    const fit = () => setW(Math.floor(el.clientWidth));
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const temps = hours.map((h) => h.temp);
  let lo = Math.min(...temps), hi = Math.max(...temps);
  if (hi - lo < MIN_SPAN) [lo, hi] = [(lo + hi - MIN_SPAN) / 2, (lo + hi + MIN_SPAN) / 2];
  const hasPop = hours.some((h) => h.pop != null);
  const maxPop = Math.max(0, ...hours.map((h) => h.pop ?? 0));
  const step = (w - PAD * 2) / (hours.length - 1);
  const x = (i) => PAD + i * step;
  const y = (t) => TEMP_BOTTOM - ((t - lo) / (hi - lo)) * (TEMP_BOTTOM - TEMP_TOP);
  const bw = Math.min(step * 0.6, 14);
  // 钟点和气温每 3 小时写一个，第一个小时总写；离第一个太近的不写，免得挤在一起
  const ticks = hours.map((h, i) => i === 0 || (hourOf(h.time) % 3 === 0 && i >= 2));
  const span = spanName(hours, today);
  const summary = `${span}气温 ${deg(Math.min(...temps))} 到 ${deg(Math.max(...temps))}` + (hasPop ? `，降水概率最高 ${maxPop}%` : "");
  const last = hours.length - 1;
  const pick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setAt(Math.max(0, Math.min(last, Math.round((e.clientX - r.left - PAD) / step))));
  };
  // 手指（M8）：按下就读那个小时，左右拖着读；上下划是滚页面（touch-action: pan-y），浏览器会取消这次按下，读数留着。
  // 抬起手指读数也留着，好看清；鼠标移出图才清掉
  const pointer = {
    onPointerDown: (e) => {
      if (e.pointerType === "mouse") return;
      e.currentTarget.setPointerCapture?.(e.pointerId);
      pick(e);
    },
    onPointerMove: (e) => (e.pointerType === "mouse" || e.buttons) && pick(e),
    onPointerLeave: (e) => e.pointerType === "mouse" && setAt(null),
  };
  // 键盘：Tab 停到图上从第一个小时开始，左右键一小时一小时看，Home、End 到头尾，离开图清掉
  const keys = (e) => {
    const to = { ArrowLeft: (at ?? 0) - 1, ArrowRight: (at ?? -1) + 1, Home: 0, End: last }[e.key];
    if (to == null) return;
    e.preventDefault();
    setAt(Math.max(0, Math.min(last, to)));
  };
  const h = at == null ? null : hours[at];
  const reading = h && `${hourName(h, today)} ${h.text}，${deg(h.temp)}` + (h.pop != null ? `，降水概率 ${h.pop}%` : "") + (h.precip > 0 ? `，${h.precip} 毫米` : "");

  return (
    <div className={styles.hourly}>
      <div className={styles.hourlyHead}>
        <span className={styles.title}>{span}</span>
        {h ? (
          <span className={styles.read}>
            <b>{hourName(h, today)}</b> {h.text} · {deg(h.temp)}
            {h.pop != null && ` · 降水概率 ${h.pop}%`}{h.precip > 0 && ` · ${h.precip} 毫米`}
          </span>
        ) : (
          <span className={styles.key}><i />气温{hasPop && <><i className={styles.rain} />降水概率</>}</span>
        )}
      </div>
      <div ref={box}>
        {w > 0 && (
          <svg width={w} height={HEIGHT} tabIndex={0} role="slider" aria-label={`${summary}。左右方向键逐小时看`} aria-valuemin={0} aria-valuemax={last} aria-valuenow={at ?? 0}
            aria-valuetext={reading || summary} {...pointer} onKeyDown={keys} onFocus={(e) => e.target.matches(":focus-visible") && at == null && setAt(0)} onBlur={() => setAt(null)}>
            {h && <line className={styles.cross} x1={x(at)} x2={x(at)} y1={TEMP_TOP - 8} y2={BAR_BOTTOM} />}
            {hasPop && (
              <>
                {hours.map((p, i) => {
                  const bh = ((p.pop ?? 0) / 100) * (BAR_BOTTOM - BAR_TOP);
                  return bh > 0 && <path key={p.time} className={styles.bar + (i === at ? " " + styles.on : "")} d={barPath(x(i) - bw / 2, BAR_BOTTOM - bh, bw, bh)} />;
                })}
                {hours.map((p, i) => p.pop >= POP_LABEL && p.pop !== hours[i - 1]?.pop && (
                  <text key={p.time} x={x(i)} y={BAR_BOTTOM - (p.pop / 100) * (BAR_BOTTOM - BAR_TOP) - 4}>{p.pop}%</text>
                ))}
                {maxPop === 0 && <text className={styles.none} x={w / 2} y={(BAR_TOP + BAR_BOTTOM) / 2 + 8}>降水概率都是 0</text>}
                <line className={styles.base} x1={PAD - bw / 2} x2={w - PAD + bw / 2} y1={BAR_BOTTOM + 0.5} y2={BAR_BOTTOM + 0.5} />
              </>
            )}
            <path className={styles.tl} d={hours.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.temp).toFixed(1)}`).join("")} />
            {hours.map((p, i) => ticks[i] && (
              <g key={p.time}>
                <circle className={styles.dot} cx={x(i)} cy={y(p.temp)} r={3} />
                <text className={styles.t} x={x(i)} y={y(p.temp) - 8}>{deg(p.temp)}</text>
                <text x={x(i)} y={HEIGHT - 4}>{i > 0 && hourOf(p.time) === 0 ? `${dayWord(p.time.slice(0, 10), today)}0时` : `${hourOf(p.time)}时`}</text>
              </g>
            ))}
            {h && <circle className={styles.dot} cx={x(at)} cy={y(h.temp)} r={4.5} />}
          </svg>
        )}
      </div>
    </div>
  );
}
