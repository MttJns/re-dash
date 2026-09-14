"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// ---------- formatting ----------

const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const safe = (f) => (v) => (v == null || Number.isNaN(v) ? "—" : f(v));
const F = {
  usdCompact: safe((v) => "$" + compact.format(v)),
  usd: safe((v) => "$" + Math.round(v).toLocaleString("en-US")),
  int: safe((v) => Math.round(v).toLocaleString("en-US")),
  share: safe((v) => (v * 100).toFixed(1) + "%"),
  shareTick: safe((v) => Math.round(v * 100) + "%"),
  rate: safe((v) => v.toFixed(2) + "%"),
  ratio: safe((v) => v.toFixed(2)),
  days: safe((v) => Math.round(v) + " days"),
  months: safe((v) => v.toFixed(1) + " mo"),
  ppsf: safe((v) => "$" + Math.round(v)),
};
const monthLabel = (m) =>
  new Date(m + "-15T00:00:00Z").toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
const dayLabel = (d) =>
  new Date(d + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

// ---------- series helpers ----------

function latest(values) {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return i;
  return -1;
}
const last = (values) => values[latest(values)];

// mode: "pct" (percent change), "pts" (fraction -> percentage points), or a unit label for absolute change
function yoyText(values, mode) {
  const i = latest(values);
  if (i < 12 || values[i - 12] == null) return "";
  const now = values[i], then = values[i - 12];
  let d, unit;
  if (mode === "pct") [d, unit] = [(now / then - 1) * 100, "%"];
  else if (mode === "pts") [d, unit] = [(now - then) * 100, " pts"];
  else [d, unit] = [now - then, mode];
  const sign = d > 0 ? "+" : d < 0 ? "−" : "";
  const digits = Number.isInteger(d) ? 0 : 1;
  return `${sign}${Math.abs(d).toFixed(digits)}${unit} vs a year earlier`;
}

function monthlyPI(price, ratePct, a) {
  const principal = price * (1 - a.down_payment);
  const r = ratePct / 100 / 12;
  const n = a.term_years * 12;
  return r === 0 ? principal / n : (principal * r) / (1 - (1 + r) ** -n);
}

// NAR method: qualifying income assumes P&I is 25% of gross income
const affordabilityIndex = (income, pi) => (income / (pi * 12 * 4)) * 100;

// ---------- line chart ----------

const charts = new Map();
const resizer = new ResizeObserver((entries) => entries.forEach((e) => charts.get(e.target)?.()));

function niceTicks(lo, hi, count = 4) {
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let v = Math.floor(lo / step) * step; v <= Math.ceil(hi / step) * step + step / 2; v += step) {
    ticks.push(+v.toFixed(10));
  }
  return ticks;
}

function pathFor(values, x, y) {
  let d = "", pen = false;
  values.forEach((v, i) => {
    if (v == null) return void (pen = false);
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

function drawLine(host, o) {
  const W = host.clientWidth;
  if (!W) return;
  const H = o.height;
  const n = o.x.length;
  const pad = o.compact ? { t: 6, r: 6, b: 6, l: 2 } : { t: 8, r: o.endLabels ? 40 : 10, b: 20, l: 40 };
  const all = o.series.flatMap((s) => s.values).filter((v) => v != null);
  if (o.ref != null) all.push(o.ref);
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) [lo, hi] = [lo - 1, hi + 1];
  let ticks = [];
  if (o.compact) {
    const p = (hi - lo) * 0.1;
    [lo, hi] = [lo - p, hi + p];
  } else {
    ticks = niceTicks(lo, hi);
    [lo, hi] = [ticks[0], ticks[ticks.length - 1]];
  }
  const x = (i) => pad.l + (n === 1 ? 0 : (i * (W - pad.l - pad.r)) / (n - 1));
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const root = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.title });
  for (const t of ticks) {
    root.append(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), class: "gl" }));
    const label = svgEl("text", { x: pad.l - 6, y: y(t) + 3.5, "text-anchor": "end", class: "tick" });
    label.textContent = o.tickFmt(t);
    root.append(label);
  }
  if (!o.compact) {
    for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
      const anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
      const label = svgEl("text", { x: x(i), y: H - 5, "text-anchor": anchor, class: "tick" });
      label.textContent = o.xFmt(o.x[i]);
      root.append(label);
    }
  }
  if (o.ref != null) root.append(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y(o.ref), y2: y(o.ref), class: "ref" }));

  for (const s of o.series) {
    if (o.area) {
      const i0 = s.values.findIndex((v) => v != null), i1 = latest(s.values);
      const d = pathFor(s.values, x, y) + `L${x(i1)},${y(lo)}L${x(i0)},${y(lo)}Z`;
      root.append(svgEl("path", { d, style: `fill:${s.color};fill-opacity:0.1;stroke:none` }));
    }
    root.append(svgEl("path", {
      d: pathFor(s.values, x, y),
      style: `fill:none;stroke:${s.color};stroke-width:2;stroke-linejoin:round;stroke-linecap:round`,
    }));
  }

  const ends = o.series.map((s) => ({ s, i: latest(s.values) })).filter((e) => e.i >= 0);
  const endY = ends.map((e) => y(e.s.values[e.i]));
  const labelsFit = ends.length < 2 || Math.abs(endY[0] - endY[1]) >= 12;
  ends.forEach((e, k) => {
    root.append(svgEl("circle", { cx: x(e.i), cy: endY[k], r: 4, class: "ring", style: `fill:${o.dot ?? e.s.color}` }));
    if (o.endLabels && labelsFit) {
      const label = svgEl("text", { x: x(e.i) + 8, y: endY[k] + 4, class: "endlabel" });
      label.textContent = o.fmt(e.s.values[e.i]);
      root.append(label);
    }
  });

  const hair = svgEl("line", { y1: pad.t, y2: H - pad.b, class: "hair", visibility: "hidden" });
  const hit = svgEl("rect", {
    x: pad.l, y: 0, width: Math.max(0, W - pad.l - pad.r), height: H, fill: "transparent", tabindex: 0,
    "aria-label": `${o.title}. Use arrow keys to read values.`,
  });
  root.append(hair, hit);
  const tip = el("div", { class: "tip", hidden: "" });
  host.replaceChildren(root, tip);

  let cur = n - 1;
  const show = (i) => {
    cur = Math.max(0, Math.min(n - 1, i));
    hair.setAttribute("x1", x(cur));
    hair.setAttribute("x2", x(cur));
    hair.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "tip-x" }, o.xFmt(o.x[cur])));
    for (const s of o.series) {
      const row = el("div", { class: "tip-row" });
      row.append(el("span", { class: "key", style: `background:${s.color}` }), el("strong", {}, o.fmt(s.values[cur])), el("span", { class: "tip-name" }, s.name));
      tip.append(row);
    }
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${x(cur) + 10 + tw > W ? x(cur) - tw - 10 : x(cur) + 10}px`;
    tip.style.top = `${pad.t}px`;
  };
  const hide = () => {
    tip.hidden = true;
    hair.setAttribute("visibility", "hidden");
  };
  hit.addEventListener("pointermove", (ev) => {
    const box = root.getBoundingClientRect();
    show(Math.round(((ev.clientX - box.left - pad.l) / (W - pad.l - pad.r)) * (n - 1)));
  });
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("focus", () => show(cur));
  hit.addEventListener("blur", hide);
  hit.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    show(cur + (ev.key === "ArrowLeft" ? -1 : 1));
  });
}

function tableView(o) {
  const details = el("details", { class: "tbl" });
  const table = el("table");
  const head = el("tr");
  head.append(el("th", {}, "Period"), ...o.series.map((s) => el("th", {}, s.name)));
  table.append(head);
  for (let i = o.x.length - 1; i >= 0; i--) {
    const row = el("tr");
    row.append(el("td", {}, o.xFmt(o.x[i])), ...o.series.map((s) => el("td", {}, o.fmt(s.values[i]))));
    table.append(row);
  }
  const wrap = el("div", { class: "tbl-wrap" });
  wrap.append(table);
  details.append(el("summary", {}, "Table"), wrap);
  return details;
}

// Appends legend (2+ series), chart and table view to an attached parent, then draws.
function mountChart(parent, o) {
  if (o.series.length > 1) {
    const legend = el("div", { class: "legend" });
    for (const s of o.series) {
      const item = el("span");
      item.append(el("span", { class: "key", style: `background:${s.color}` }), document.createTextNode(s.name));
      legend.append(item);
    }
    parent.append(legend);
  }
  const host = el("div", { class: "chart" });
  parent.append(host, tableView(o));
  charts.set(host, () => drawLine(host, o));
  resizer.observe(host);
  drawLine(host, o);
}

function clearCard(card) {
  card.querySelectorAll(".chart").forEach((host) => {
    resizer.unobserve(host);
    charts.delete(host);
  });
  card.replaceChildren();
}

function fillCard(id, { label, value, notes = [], extra = [], chart }) {
  const card = $(id);
  clearCard(card);
  card.append(el("div", { class: "label" }, label));
  if (value != null) card.append(el("div", { class: "value" }, value));
  for (const note of notes) if (note) card.append(el("div", { class: "note" }, note));
  card.append(...extra);
  if (chart) mountChart(card, chart);
}

// ---------- cards ----------

function sparkline(title, x, values, fmt) {
  return { title, x, xFmt: monthLabel, fmt, tickFmt: fmt, height: 36, compact: true, dot: "var(--s1)", series: [{ name: title, values, color: "var(--muted)" }] };
}

function trend(title, x, series, fmt, extra = {}) {
  return { title, x, xFmt: monthLabel, fmt, tickFmt: fmt, height: 88, series, ...extra };
}

function mosScale(v) {
  const bar = el("div", { class: "scale", "aria-hidden": "true" });
  bar.append(el("span", { style: "flex:4" }), el("span", { style: "flex:2" }), el("span", { style: "flex:4" }));
  if (v != null) bar.append(el("i", { style: `left:${Math.min(v, 10) * 10}%` }));
  return [bar];
}

function renderTier1(d) {
  const r = d.redfin, z = d.zillow, m = r.months;

  const mos = last(r.months_of_supply);
  const zone = mos == null ? "" : mos < 4 ? "Seller's market (under 4)" : mos <= 6 ? "Balanced market (4–6)" : "Buyer's market (over 6)";
  fillCard("c-mos", {
    label: "Months of supply", value: F.months(mos), notes: [zone, yoyText(r.months_of_supply, " mo")],
    extra: mosScale(mos), chart: sparkline("Months of supply", m, r.months_of_supply, F.months),
  });

  fillCard("c-inventory", {
    label: "Active inventory", value: F.int(last(r.inventory)),
    notes: [yoyText(r.inventory, "pct"), `${F.int(last(r.new_listings))} new · ${F.int(last(r.pending_sales))} pending`],
    chart: sparkline("Active inventory", m, r.inventory, F.int),
  });

  const zi = latest(z.zhvi_mid);
  const tiers = el("div", { class: "tiers" });
  for (const [name, key] of [["Zillow entry", "zhvi_entry"], ["Zillow mid", "zhvi_mid"], ["Zillow luxury", "zhvi_luxury"]]) {
    const cell = el("span");
    cell.append(el("b", {}, F.usdCompact(z[key][zi])), document.createTextNode(name));
    tiers.append(cell);
  }
  fillCard("c-price", {
    label: "Median sale price", value: F.usd(last(r.median_sale_price)), notes: [yoyText(r.median_sale_price, "pct")],
    extra: [tiers],
    chart: sparkline("Median sale price", m, r.median_sale_price, F.usd),
  });

  fillCard("c-dom", {
    label: "Median days on market", value: F.days(last(r.median_dom)), notes: [yoyText(r.median_dom, " days")],
    chart: sparkline("Median days on market", m, r.median_dom, F.days),
  });

  fillCard("c-stl", {
    label: "Sale-to-list ratio", value: F.share(last(r.sale_to_list)),
    notes: [yoyText(r.sale_to_list, "pts"), `${F.share(last(r.sold_above_list))} sold above list`],
    chart: sparkline("Sale-to-list ratio", m, r.sale_to_list, F.share),
  });
}

function renderMechanics(d) {
  const r = d.redfin, m = r.months;

  fillCard("c-cuts", {
    label: "Listings with a price drop", value: F.share(last(r.price_drops)), notes: [yoyText(r.price_drops, "pts")],
    chart: trend("Share of listings with a price drop", m, [{ name: "Price drops", values: r.price_drops, color: "var(--s1)" }], F.share, { tickFmt: F.shareTick, area: true }),
  });

  fillCard("c-absorb", {
    label: "Pending sales per new listing", value: F.ratio(last(r.pending_to_new)),
    notes: ["Above 1.0, buyers absorb listings faster than they arrive"],
    chart: trend("Pending sales per new listing", m, [{ name: "Pending / new", values: r.pending_to_new, color: "var(--s1)" }], F.ratio, { ref: 1 }),
  });

  const sold = last(r.median_ppsf), list = last(r.median_list_ppsf);
  fillCard("c-ppsf", {
    label: "Price per sq ft", value: `${F.ppsf(sold)} sold`,
    notes: [`${F.ppsf(list)} list · sold ${sold && list ? (sold < list ? "−" : "+") + Math.abs((sold / list - 1) * 100).toFixed(1) + "%" : "—"} vs list`],
    chart: trend("Price per square foot", m, [
      { name: "Sold", values: r.median_ppsf, color: "var(--s1)" },
      { name: "List", values: r.median_list_ppsf, color: "var(--s2)" },
    ], F.ppsf, { endLabels: true }),
  });
}

function renderAffordability(d) {
  const a = d.assumptions, rates = d.rates.mortgage30;
  const current = last(rates);
  const price = last(d.redfin.median_sale_price);
  const card = $("c-afford");
  clearCard(card);

  const slider = el("input", { type: "range", min: "2", max: "10", step: "0.01", value: String(current), "aria-label": "30-year fixed mortgage rate" });
  const rateOut = el("div", { class: "value" });
  const reset = el("button", { type: "button", class: "link" }, `Reset to current ${F.rate(current)}`);
  const piOut = el("div", { class: "value sm" });
  const haiOut = el("div", { class: "value sm" });
  const haiNote = el("div", { class: "note" });

  const rateRow = el("div", { class: "rate-row" });
  rateRow.append(rateOut, reset);
  const pair = el("div", { class: "pair" });
  const piBox = el("div"), haiBox = el("div");
  piBox.append(el("div", { class: "label" }, "Monthly P&I, median sale price"), piOut);
  haiBox.append(el("div", { class: "label" }, "Affordability index"), haiOut);
  pair.append(piBox, haiBox);

  const left = el("div");
  left.append(
    el("div", { class: "label" }, "30-yr fixed rate (drag to test)"), rateRow, slider, pair, haiNote,
  );
  const right = el("div");
  right.append(el("div", { class: "label" }, "30-yr fixed rate, weekly (Freddie Mac)"));
  const split = el("div", { class: "split" });
  split.append(left, right);
  card.append(split);
  mountChart(right, {
    title: "30-year fixed mortgage rate", x: d.rates.weeks, xFmt: dayLabel, fmt: F.rate, tickFmt: F.rate, height: 104,
    series: [{ name: "30-yr fixed", values: rates, color: "var(--s1)" }], area: true,
  });
  right.append(el("div", { class: "note muted" }, `Assumes ${a.down_payment * 100}% down, ${a.term_years}-yr term. Property tax ${(a.property_tax_rate * 100).toFixed(1)}% and insurance ${F.usd(a.insurance_annual)}/yr are placeholder estimates, not sourced data.`));

  const update = () => {
    const rate = Number(slider.value);
    const pi = monthlyPI(price, rate, a);
    rateOut.textContent = F.rate(rate);
    piOut.textContent = F.usd(pi);
    if (d.income) {
      haiOut.textContent = String(Math.round(affordabilityIndex(d.income.median_family_income, pi)));
      haiNote.textContent = `100 = median family income (${F.usd(d.income.median_family_income)}, ACS ${d.income.year}) exactly qualifies`;
    } else {
      haiOut.textContent = "—";
      haiNote.textContent = "Needs Census income (CENSUS_API_KEY)";
    }
    renderRentVsOwn(d, rate);
  };
  slider.addEventListener("input", update);
  reset.addEventListener("click", () => {
    slider.value = String(current);
    update();
  });
  update();
}

function renderRentVsOwn(d, rate) {
  const a = d.assumptions, r = d.redfin, z = d.zillow;
  const pi = latest(r.median_sale_price), ri = latest(z.zori);
  const price = r.median_sale_price[pi], rent = z.zori[ri];
  const own = monthlyPI(price, rate, a) + (price * a.property_tax_rate + a.insurance_annual) / 12;
  const gap = own - rent;

  const bars = el("div", { class: "bars" });
  for (const [name, v] of [["Own (PITI)", own], ["Rent", rent]]) {
    const track = el("div", { class: "bar-track" });
    track.append(el("div", { class: "bar", style: `width:${(v / Math.max(own, rent)) * 75}%` }), el("span", { class: "bar-val" }, F.usd(v)));
    const row = el("div", { class: "bar-row" });
    row.append(el("span", {}, name), track);
    bars.append(row);
  }
  fillCard("c-rent", {
    label: "Rent vs own, monthly cost",
    value: `${gap >= 0 ? "+" : "−"}${F.usd(Math.abs(gap))}`,
    notes: [`${gap >= 0 ? "more" : "less"} per month to own than rent (${((own / rent - 1) * 100).toFixed(0)}%)`],
    extra: [bars, el("div", { class: "note muted" }, `Own: median sale price (${monthLabel(r.months[pi])}) at ${F.rate(rate)}. Rent: Zillow observed rent index (${monthLabel(z.months[ri])}).`)],
  });
}

function renderYield(d) {
  const z = d.zillow;
  const values = z.zori.map((rent, i) => (rent != null && z.zhvi_mid[i] ? (rent * 12) / z.zhvi_mid[i] : null));
  fillCard("c-yield", {
    label: "Gross rental yield", value: F.share(last(values)),
    notes: ["Annual rent ÷ mid-tier home value"],
    chart: sparkline("Gross rental yield", z.months, values, F.share),
  });
}

function renderPermits(d) {
  const p = d.permits;
  const total = p.single_family.map((s, i) => (s == null ? null : s + (p.multifamily[i] ?? 0)));
  const i = latest(total);
  const notes = [i >= 0 ? `${monthLabel(p.months[i])}: ${F.int(p.single_family[i])} single-family · ${F.int(p.multifamily[i])} multifamily` : ""];
  // Single months are noisy, so compare the latest 12 months with the 12 before.
  if (i >= 23) {
    const sum = (from, to) => total.slice(from, to).reduce((acc, v) => acc + (v ?? 0), 0);
    const recent = sum(i - 11, i + 1), prior = sum(i - 23, i - 11);
    const change = (recent / prior - 1) * 100;
    notes.push(`${F.int(recent)} in the last 12 months, ${change > 0 ? "+" : change < 0 ? "−" : ""}${Math.abs(change).toFixed(1)}% vs prior 12`);
  }
  fillCard("c-permits", {
    label: "Housing units permitted", value: F.int(total[i]), notes,
    chart: sparkline("Housing units permitted", p.months, total, F.int),
  });
}

function renderMigration(d) {
  const g = d.migration;
  const i = latest(g.net);
  const signed = safe((v) => `${v < 0 ? "−" : "+"}${F.int(Math.abs(v))}`);
  const prior = i > 0 && g.net[i - 1] != null ? ` · ${signed(g.net[i - 1])} in ${g.years[i - 1]}` : "";
  const notes = [
    `${g.years[i]} · ${((g.net[i] / g.population[i]) * 100).toFixed(1)}% of population${prior}`,
    `${signed(g.domestic[i])} domestic · ${signed(g.international[i])} international`,
  ];
  fillCard("c-migration", {
    label: "Net in-migration", value: signed(g.net[i]), notes,
    chart: { ...sparkline("Net in-migration", g.years, g.net, signed), xFmt: String },
  });
}

function renderSources(d) {
  const r = d.redfin, z = d.zillow, weeks = d.rates.weeks;
  const items = [
    ["Redfin Data Center", "https://www.redfin.com/news/data-center/", `through ${monthLabel(r.months[r.months.length - 1])}, published ${r.updated}`],
    ["Zillow Research", "https://www.zillow.com/research/data/", `through ${monthLabel(z.months[z.months.length - 1])}`],
    ["Freddie Mac PMMS", "https://www.freddiemac.com/pmms", `week of ${dayLabel(weeks[weeks.length - 1])}`],
    ["Census building permits", "https://www.census.gov/construction/bps/", `through ${monthLabel(d.permits.months[d.permits.months.length - 1])}`],
    ["Census population estimates", "https://www.census.gov/programs-surveys/popest.html", `vintage ${d.migration.vintage}`],
    ["Census ACS 1-year", "https://www.census.gov/programs-surveys/acs", d.income ? `${d.income.year} median family income` : "not loaded"],
  ];
  $("sources").replaceChildren(...items.map(([name, href, detail]) => {
    const item = el("span");
    item.append(el("a", { href, rel: "noopener" }, name), document.createTextNode(` · ${detail}`));
    return item;
  }));
}

function render(d) {
  $("metro-name").textContent = `${d.metro.name} housing market`;
  const b = d.built_at;
  $("built").textContent = `Built ${b.slice(0, 4)}-${b.slice(4, 6)}-${b.slice(6, 8)}`;
  renderTier1(d);
  renderMechanics(d);
  renderAffordability(d);
  renderYield(d);
  renderPermits(d);
  renderMigration(d);
  renderSources(d);
}

// ---------- boot ----------

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

async function main() {
  const manifest = await getJSON("data/manifest.json");
  const select = $("metro");
  for (const m of manifest.metros) select.append(el("option", { value: m.id }, m.name));
  const pick = () => manifest.metros.find((m) => m.id === location.hash.slice(1)) ?? manifest.metros[0];
  const load = async () => {
    const entry = pick();
    select.value = entry.id;
    document.body.classList.add("loading");
    render(await getJSON(entry.path));
    document.body.classList.remove("loading");
  };
  select.addEventListener("change", () => { location.hash = select.value; });
  window.addEventListener("hashchange", () => load().catch(showError));
  await load();
}

function showError(err) {
  const status = $("status");
  status.textContent = `Could not load data: ${err.message}`;
  status.hidden = false;
  document.body.classList.remove("loading");
}

main().catch(showError);
