// 航班卡、火车卡：search_flights、search_trains 查到的（agent-tools 的 flyai，数据是飞猪的，见 backend/app/tools/flyai_mcp.py）。
// 一行一个直达班次：左边出发时刻和站，中间用时、班次号和航司车型，右边到达时刻和站（晚一天以上到的标 +1），最右是参考价、舱位座席和预订链接。
// 行程卡：模型从查到的班次里排的方案（present_itinerary），一个方案一块，按顺序列出每一段，标出哪天、合计参考价。
// 卡片上署飞猪和查的时间，模型的正文里不用再引用；价格是查的时候的参考价
import { IconExternal, IconPlane, IconTicket, IconTrain } from "./icons.jsx";
import { stamp } from "./research.js";

const KIND_NAMES = { flight: "航班", train: "火车" };
const ICONS = { flight: IconPlane, train: IconTrain };
// 服务标的限制（见 agent-tools 的 services/flyai/README.md）
const NOTES = {
  filtered_partial_list: "飞猪一次给 10 个班次，这里是其中符合条件的，可能还有别的",
  other_class_returned: "有的班次没有要的舱位座席，标的是别的舱位座席的价格",
  high_speed_not_covered: "飞猪给的这一页里没有高铁动车，不代表没有，要看高铁可以单独查",
};
const WEEKDAYS = "日一二三四五六";

// 这一轮的航班卡、火车卡，按出现的先后；同一个查询查了几次，位置按第一次、内容用最后一次。没查到班次的不画（过程里写了）
export function travelOf(items) {
  const by = new Map();
  for (const i of items) {
    if (i.type === "step" && (i.kind === "flight" || i.kind === "train") && i.travel?.services.length) {
      by.set(`${i.kind}|${i.travel.origin}|${i.travel.destination}|${i.travel.date}|${(i.limits || []).join(",")}`, i.travel);
    }
  }
  return [...by.entries()].map(([key, data]) => ({ key, data }));
}

// 这一轮排好的行程（一个回答最多一张）
export function itinerariesOf(items) {
  return items.filter((i) => i.type === "step" && i.kind === "itinerary" && i.itinerary).map((i) => ({ key: i.id, data: i.itinerary }));
}

// "2026-11-20T08:05" → "08:05"；时刻是当地时间，不换时区
const hm = (t) => t.slice(11, 16);

export function dayText(date) {
  const [y, m, d] = date.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return date;
  return `${m}月${d}日 周${WEEKDAYS[new Date(y, m - 1, d).getDay()]}`;
}

export function mins(m) {
  if (m < 60) return `${m} 分钟`;
  return `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分` : ""}`;
}

const yuan = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
const stationOf = (stop) => [stop.station, stop.terminal].filter(Boolean).join(" ");

function Credit({ at }) {
  const t = stamp(at);
  return <span className="by" title={t?.full}>飞猪旅行{t && ` · ${t.text} 查询`}</span>;
}

export function TravelCard({ card }) {
  const tl = card.data;
  const Icon = ICONS[tl.kind];
  return (
    <section className="ws-card ws-map ws-travel" aria-label={`${tl.origin}到${tl.destination}的${KIND_NAMES[tl.kind]}`}>
      <div className="ws-map-head">
        <Icon size={15} />
        <span className="what">{tl.origin}</span>
        <span className="to">→</span>
        <span className="what">{tl.destination}</span>
        <span className="where">{dayText(tl.date)} · 直达{KIND_NAMES[tl.kind]}</span>
        <Credit at={tl.observed_at} />
      </div>
      <ol className="ws-svcs">{tl.services.map((s) => <ServiceRow key={s.option_id} s={s} />)}</ol>
      {tl.limitations.filter((l) => NOTES[l]).map((l) => <p key={l} className="ws-map-note">{NOTES[l]}</p>)}
      <p className="ws-map-note">价格是查询时的参考价，余票、退改以飞猪为准</p>
    </section>
  );
}

function ServiceRow({ s }) {
  const late = s.day_offset > 0 ? `+${s.day_offset}` : "";
  return (
    <li className="ws-svc">
      <div className="end">
        <b className="t">{hm(s.departure.time)}</b>
        <span className="st" title={stationOf(s.departure)}>{stationOf(s.departure)}</span>
      </div>
      <div className="mid">
        <span className="dur">{mins(s.duration_minutes)}</span>
        <span className="line" aria-hidden />
        <span className="no">{s.number}{s.carrier && ` · ${s.carrier}`}</span>
      </div>
      <div className="end arr">
        <b className="t">{hm(s.arrival.time)}{late && <sup title={s.day_offset === 1 ? "次日到达" : `${s.day_offset} 天后到达`}>{late}</sup>}</b>
        <span className="st" title={stationOf(s.arrival)}>{stationOf(s.arrival)}</span>
      </div>
      <div className="fare">
        {s.price_yuan != null ? <b>¥{yuan(s.price_yuan)}</b> : <span className="none">没有价格</span>}
        {s.travel_class && <span className="cls">{s.travel_class}</span>}
        {s.booking_url && <a href={s.booking_url} target="_blank" rel="noreferrer" title="在飞猪上看这个班次">预订<IconExternal size={10} /></a>}
      </div>
    </li>
  );
}

// ---- 行程卡 ----

export function ItineraryCard({ card }) {
  const it = card.data;
  return (
    <section className="ws-card ws-map ws-travel" aria-label={it.title}>
      <div className="ws-map-head">
        <IconTicket size={15} />
        <span className="what">{it.title}</span>
        <span className="by">飞猪旅行</span>
      </div>
      {it.plans.map((p, k) => <Plan key={k} p={p} />)}
      <p className="ws-map-note">价格是查询时的参考价，余票、退改以飞猪为准</p>
    </section>
  );
}

// 几个方案时模型把推荐的排在第一个（见 prompts.toml 的 tools.present_itinerary），标题里常带着“推荐”，卡片上不再加标签
function Plan({ p }) {
  const priced = p.legs.every((l) => l.price_yuan != null);
  const total = p.legs.reduce((sum, l) => sum + (l.price_yuan || 0), 0);
  return (
    <div className="ws-plan">
      <div className="ws-plan-head">
        <b>{p.title}</b>
        {priced && p.legs.length > 1 && <span className="sum">合计 ¥{yuan(total)}</span>}
      </div>
      <p className="ws-plan-why">{p.reason}</p>
      {p.legs.map((l, k) => (
        <div key={l.option_id} className="ws-leg">
          <span className="ws-leg-day">第 {k + 1} 段 · {dayText(l.departure.time)} · {KIND_NAMES[l.kind]}</span>
          <ol className="ws-svcs"><ServiceRow s={l} /></ol>
        </div>
      ))}
    </div>
  );
}
