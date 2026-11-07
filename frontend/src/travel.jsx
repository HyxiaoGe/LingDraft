// 航班卡、火车卡：search_flights、search_trains 查到的（agent-tools 的 flyai，数据是飞猪的，见 backend/app/tools/flyai_mcp.py）。
// 一行一个直达班次：左边出发时刻和站，中间用时、班次号和航司车型，右边到达时刻和站（晚一天以上到的标 +1），最右是参考价、舱位座席和去飞猪看的链接；
// 手机上改成竖着的：出发、用时、到达三行，价格在右边（M8）。四个班次起能排序、按时段筛，火车还能只看高铁动车（评审第 20 条）。
// 预订链接 agent-tools 只放行飞猪的主机（a.feizhu.com、router.feizhu.com，见 services/flyai/src/projector.js 的 BOOKING_HOSTS），按钮写“去飞猪看”
// 行程卡：模型从查到的班次里排的方案（present_itinerary），一个方案一块，按顺序列出每一段，标出哪天、合计参考价。
// 卡片上署飞猪和查的时间，模型的正文里不用再引用；价格是查的时候的参考价
import { useState } from "react";
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

// 排序、筛选：班次少于 4 个时一眼看得完，不出这一排
const TOOLS_MIN = 4;
const SORTS = [
  ["time", "出发早", (a, b) => a.departure.time.localeCompare(b.departure.time)],
  ["price", "价格低", (a, b) => (a.price_yuan ?? Infinity) - (b.price_yuan ?? Infinity)], // 没有价格的排最后
  ["duration", "用时短", (a, b) => a.duration_minutes - b.duration_minutes],
];
// 按出发时刻分：中午以前（含凌晨）、中午到傍晚 6 点、6 点以后
const SLOTS = [["am", "上午", (h) => h < 12], ["pm", "下午", (h) => h >= 12 && h < 18], ["eve", "晚上", (h) => h >= 18]];
const hourOf = (s) => Number(s.departure.time.slice(11, 13));
// 车次编号规则：G 高铁、D 动车、C 城际，都是动车组
const fast = (s) => /^[GDC]/i.test(s.number || "");

function Seg({ label, children }) {
  return <div className="ws-seg" role="group" aria-label={label}><span className="lab" aria-hidden="true">{label}</span>{children}</div>;
}
function Opt({ on, onClick, children }) {
  return <button className={"ws-seg-opt" + (on ? " on" : "")} aria-pressed={on} onClick={onClick}>{children}</button>;
}

export function TravelCard({ card }) {
  const tl = card.data;
  const Icon = ICONS[tl.kind];
  const all = tl.services;
  const [sort, setSort] = useState("time");
  const [slot, setSlot] = useState(null);
  const [onlyFast, setOnlyFast] = useState(false);
  const tools = all.length >= TOOLS_MIN;
  const mixed = tl.kind === "train" && all.some(fast) && !all.every(fast); // 全是（或全不是）动车组时这个开关没用，不出
  const inSlot = (s) => !slot || SLOTS.find(([k]) => k === slot)[2](hourOf(s));
  const fastOk = (s) => !onlyFast || fast(s);
  const shown = tools ? all.filter((s) => inSlot(s) && fastOk(s)).sort(SORTS.find(([k]) => k === sort)[2]) : all;
  const filtered = !!slot || onlyFast;
  const clear = () => { setSlot(null); setOnlyFast(false); };
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
      {tools && (
        <div className="ws-svc-tools">
          <Seg label="排序">{SORTS.map(([k, name]) => <Opt key={k} on={sort === k} onClick={() => setSort(k)}>{name}</Opt>)}</Seg>
          <Seg label="出发">
            {SLOTS.map(([k, name, test]) => (
              <Opt key={k} on={slot === k} onClick={() => setSlot(slot === k ? null : k)}>
                {name}<span className="n">{all.filter((s) => test(hourOf(s)) && fastOk(s)).length}</span>
              </Opt>
            ))}
          </Seg>
          {mixed && <Opt on={onlyFast} onClick={() => setOnlyFast(!onlyFast)}>只看高铁动车<span className="n">{all.filter((s) => fast(s) && inSlot(s)).length}</span></Opt>}
          <span className="ws-svc-count" role="status">{filtered ? `显示 ${shown.length} / ${all.length} 个班次` : ""}</span>
        </div>
      )}
      {shown.length ? (
        <ol className="ws-svcs">{shown.map((s) => <ServiceRow key={s.option_id} s={s} />)}</ol>
      ) : (
        <div className="ws-svc-none">
          <span>查到的 {all.length} 个班次里没有符合的</span>
          <button className="ws-btn ghost sm" onClick={clear}>清除筛选</button>
        </div>
      )}
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
        <span className="cls">{[s.price_yuan != null && "参考价", s.travel_class].filter(Boolean).join(" · ")}</span>
        {s.booking_url && <a href={s.booking_url} target="_blank" rel="noreferrer" aria-label={`去飞猪看 ${s.number}`} title="在飞猪上看这个班次，新窗口打开">去飞猪看<IconExternal size={10} /></a>}
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
        {priced && p.legs.length > 1 && <span className="sum">合计参考价 ¥{yuan(total)}</span>}
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
