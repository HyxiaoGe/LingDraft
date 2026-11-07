// 地点卡、路线卡：place_search、route_compare 查到的（agent-tools 的 maps-service，先高德后腾讯，见 backend/app/tools/maps_mcp.py）。
// 地点卡一行一个地方：有图放图，名字能点开高德地图（腾讯的没有链接），下面是评分、人均、距离、地址、营业时间。
// 路线卡一行一种出行方式：用时最显眼，后面是距离、费用；公交列出每一段坐什么、从哪站到哪站，另外几种走法收着。
// 卡片上署地图服务商和查的时间，模型的正文里不用再引用
import { useState } from "react";
import { IconChevron, IconExternal, IconPin, IconRoute } from "./icons.jsx";
import { stamp } from "./research.js";
import shell from "./card.module.css";
import styles from "./maps.module.css";

const PROVIDERS = { amap: "高德地图", tmap: "腾讯地图" };
// transit 是公共交通：地铁、公交都算，不只是公交车
export const MODES = { driving: "驾车", transit: "公共交通", walking: "步行", bicycling: "骑行" };
const LEG_KINDS = { walking: "步行", subway: "地铁", bus: "公交", other: "乘车" };

// 这一轮的地点卡、路线卡，按出现的先后；同样的查了几次，位置按第一次、内容用最后一次。查到 0 个地方的不画卡片（过程里写了没找到）
export function mapsOf(items) {
  const by = new Map();
  for (const i of items) {
    if (i.type !== "step") continue;
    if (i.kind === "place" && i.places?.places.length) {
      by.set(`place|${i.places.query}|${i.places.near}|${i.places.city}`, { kind: "place", data: i.places });
    } else if (i.kind === "route" && i.routes?.routes.length) {
      const modes = [...i.routes.routes.map((r) => r.mode), ...i.routes.unavailable_modes].sort().join(",");
      by.set(`route|${i.routes.origin}|${i.routes.destination}|${modes}`, { kind: "route", data: i.routes });
    }
  }
  return [...by.entries()].map(([key, card]) => ({ key, ...card }));
}

export function MapCard({ card }) {
  return card.kind === "place" ? <PlaceCard pl={card.data} /> : <RouteCard rp={card.data} />;
}

// 署名：用到了哪几家
const credit = (providers) => [...new Set(providers.filter(Boolean))].map((p) => PROVIDERS[p] || p).join("、") || "地图";

function dist(m) {
  if (m < 1000) return `${m} 米`;
  return `${(m / 1000).toFixed(m < 10000 ? 1 : 0).replace(/\.0$/, "")} 公里`;
}

function dur(s) {
  const min = Math.max(1, Math.round(s / 60));
  if (min < 60) return `${min} 分钟`;
  return `${Math.floor(min / 60)} 小时${min % 60 ? ` ${min % 60} 分钟` : ""}`;
}

const yuan = (v) => `${Number.isInteger(v) ? v : v.toFixed(1)} 元`;

function Head({ icon: Icon, children, providers, at }) {
  const t = stamp(at);
  return (
    <div className={styles.head}>
      <Icon size={15} />
      {children}
      <span className={styles.by} title={t?.full}>{credit(providers)}{t && ` · ${t.text} 更新`}</span>
    </div>
  );
}

// ---- 地点卡 ----

function PlaceCard({ pl }) {
  const where = pl.near ? `${pl.near}附近` : pl.city;
  const what = pl.query.split("|").map((w) => w.trim()).filter(Boolean).join("、");
  return (
    <section className={shell.card + " " + shell.map} aria-label={`${where || ""}的${what}`}>
      <Head icon={IconPin} providers={pl.places.map((p) => p.provider)} at={pl.fetched_at}>
        <span className={styles.what}>{what}</span>
        {where && <span className={styles.where}>{where}</span>}
      </Head>
      <ol className={styles.places}>{pl.places.map((p, k) => <PlaceRow key={`${k}:${p.name}`} p={p} />)}</ol>
      {pl.limitations.includes("first_term_only") && <p className={styles.note}>腾讯地图只按“{what.split("、")[0]}”搜了，别的词没搜</p>}
    </section>
  );
}

function PlaceRow({ p }) {
  const [photo, setPhoto] = useState(Boolean(p.photo_url));
  const kind = p.category.split(";").filter(Boolean).at(-1);
  const facts = [
    p.rating != null && <span key="r" className={styles.rate}>★ {p.rating}</span>,
    p.cost != null && <span key="c">人均 {yuan(p.cost)}</span>,
    p.distance_m != null && <span key="d">{dist(p.distance_m)}</span>,
    kind && <span key="k">{kind}</span>,
  ].filter(Boolean);
  const addr = [p.district, p.address].filter(Boolean).join(" ");
  return (
    <li className={styles.place}>
      {photo && <img src={p.photo_url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setPhoto(false)} />}
      <div className={styles.placeBody}>
        <div className={styles.name}>
          {p.map_url ? <a href={p.map_url} target="_blank" rel="noreferrer" title="在高德地图上看">{p.name}<IconExternal size={11} /></a> : p.name}
        </div>
        {facts.length > 0 && <div className={styles.facts}>{facts}</div>}
        {addr && <div className={styles.addr} title={addr}>{addr}</div>}
        {p.open_hours && <div className={styles.addr} title={p.open_hours}>营业 {p.open_hours}</div>}
      </div>
    </li>
  );
}

// ---- 路线卡 ----

function RouteCard({ rp }) {
  const fastest = Math.min(...rp.routes.map((r) => r.duration_s ?? Infinity));
  const missing = rp.unavailable_modes.map((m) => MODES[m] || m);
  return (
    <section className={shell.card + " " + shell.map} aria-label={`${rp.origin}到${rp.destination}的路线`}>
      <Head icon={IconRoute} providers={rp.routes.map((r) => r.provider)} at={rp.fetched_at}>
        <span className={styles.what}>{rp.origin}</span>
        <span className={styles.to}>→</span>
        <span className={styles.what}>{rp.destination}</span>
      </Head>
      <div className={styles.routes}>
        {rp.routes.map((r) => <RouteRow key={r.mode} r={r} fastest={rp.routes.length > 1 && r.duration_s === fastest} />)}
      </div>
      {missing.length > 0 && <p className={styles.note}>{missing.join("、")}没算出来</p>}
      {rp.departure_time && <p className={styles.note}>路线是按查的时候算的，没按“{rp.departure_time}”出发算</p>}
    </section>
  );
}

function costs(r) {
  return [
    r.distance_m != null && dist(r.distance_m),
    r.toll_yuan > 0 && `过路费 ${yuan(r.toll_yuan)}`,
    r.fare_yuan != null && `票价 ${yuan(r.fare_yuan)}`,
    r.transfers != null && (r.transfers ? `换乘 ${r.transfers} 次` : "不用换乘"),
    r.walking_distance_m > 0 && `步行 ${dist(r.walking_distance_m)}`,
  ].filter(Boolean);
}

function RouteRow({ r, fastest }) {
  const [more, setMore] = useState(false);
  const facts = costs(r);
  return (
    <div className={styles.route}>
      <div className={styles.routeMain}>
        <span className={styles.mode}>{MODES[r.mode] || r.mode}</span>
        <b className={styles.dur}>{r.duration_s != null ? dur(r.duration_s) : "—"}</b>
        {fastest && <span className={styles.tag}>用时最短</span>}
        {facts.length > 0 && <span className={styles.facts}>{facts.join(" · ")}</span>}
        {r.summary && <span className={styles.facts} title="地图规划路线用的策略">按“{r.summary}”规划</span>}
      </div>
      {r.legs.length > 0 && <Legs legs={r.legs} />}
      {r.alternatives.length > 0 && (
        <>
          <button className={styles.more} aria-expanded={more} onClick={() => setMore(!more)}>
            另外 {r.alternatives.length} 种走法<IconChevron size={12} className={"chev" + (more ? " up" : "")} />
          </button>
          {more && r.alternatives.map((a, k) => (
            <div key={k} className={styles.alt}>
              <div className={styles.routeMain}>
                <b className={styles.dur}>{a.duration_s != null ? dur(a.duration_s) : "—"}</b>
                <span className={styles.facts}>{costs(a).join(" · ")}</span>
              </div>
              {a.legs.length > 0 && <Legs legs={a.legs} />}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// 途经站数（via_stop_count）是中间经过几站、不算上下车那两站，卡片上照平常的说法写坐几站（加 1）。
// 线路名后面括号里是起终点站（“地铁10号线(虹桥火车站--基隆路)”），卡片上只写线路，悬停看全名
const lineName = (name) => name.replace(/\s*[(（][^()（）]*[)）]\s*$/, "") || name;

function Legs({ legs }) {
  return (
    <ol className={styles.legs}>
      {legs.map((g, k) => (
        <li key={k} className={g.kind === "walking" ? styles.walk : undefined}>
          {g.kind === "walking" ? (
            <span className={styles.what}>步行{g.distance_m != null && ` ${dist(g.distance_m)}`}{g.duration_s != null && `，约 ${dur(g.duration_s)}`}</span>
          ) : (
            <>
              <b className={styles.line} title={g.line_name}>{g.line_name ? lineName(g.line_name) : LEG_KINDS[g.kind] || "乘车"}</b>
              <span className={styles.what}>
                {g.departure_stop}{g.entrance && `（${g.entrance}进站）`}
                {g.arrival_stop && <> → {g.arrival_stop}{g.exit && `（${g.exit}出站）`}</>}
                {g.via_stop_count != null && ` · 坐 ${g.via_stop_count + 1} 站`}
                {g.duration_s != null && ` · 约 ${dur(g.duration_s)}`}
              </span>
            </>
          )}
        </li>
      ))}
    </ol>
  );
}
