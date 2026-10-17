// Delay patterns page. Reads /api/patterns; draws plain SVG, no chart library.
"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
// All 24 hours, in the order a day of trains runs: 04:00 round to 03:00.
const HOURS = [...Array(24).keys()].map((i) => (i + 4) % 24);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

const minutes = (m) => `${Math.round(m)} min`;
const pct = (share) => `${Math.round(share * 100)}%`;
const hourLabel = (h) => `${String(h).padStart(2, "0")}:00`;
const dateText = (iso) =>
  new Date(`${iso}T12:00:00+05:30`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });

// ---------- tooltip ----------

const tip = $("tip");
function showTip(target, lines) {
  tip.replaceChildren(...lines.map((line, i) => el(i === 0 ? "strong" : "span", { text: line })));
  tip.hidden = false;
  const box = target.getBoundingClientRect();
  const width = tip.offsetWidth;
  const left = Math.min(Math.max(8, box.left + box.width / 2 - width / 2), document.documentElement.clientWidth - width - 8);
  tip.style.left = `${left + window.scrollX}px`;
  tip.style.top = `${box.top + window.scrollY - tip.offsetHeight - 8}px`;
}
function hideTip() { tip.hidden = true; }

function cellLines(cell, title) {
  if (!cell) return [title, "No trains seen"];
  return [
    title,
    `Median ${minutes(cell.median)} late, 90th percentile ${minutes(cell.p90)}`,
    `${pct(cell.share_late)} were 5+ min late`,
    `${cell.runs} train${cell.runs === 1 ? "" : "s"}${cell.few ? " (too few to go by)" : ""}`,
  ];
}

// ---------- pieces ----------

function bandTable(data) {
  const cell = (dir, band) => data.by_band.find((c) => c.direction === dir && c.key === band);
  const head = el("thead", {}, el("tr", {},
    el("th", { scope: "col", text: "Scheduled" }),
    ...data.directions.map((d) => el("th", { scope: "col", text: d.label }))));
  const body = el("tbody", {}, ...data.bands.map((band) => el("tr", {},
    el("th", { scope: "row" }, band.label, el("small", { text: `${hourLabel(band.from)}–${hourLabel(band.to)}` })),
    ...data.directions.map((d) => {
      const c = cell(d.key, band.key);
      if (!c) return el("td", { class: "none", text: "no trains seen" });
      return el("td", { class: c.few ? "few" : null },
        el("span", { class: "big", text: minutes(c.median) }),
        el("span", { class: "sub", text: `${pct(c.share_late)} late 5+ · ${c.runs} train${c.runs === 1 ? "" : "s"}` }),
        c.few ? el("span", { class: "sub few-note", text: "too few to go by" }) : null);
    }))));
  $("bands").replaceChildren(
    el("caption", { class: "sr-only", text: "Median delay and share of late trains by part of the day and direction" }),
    head, body);
}

function hourChart(data, direction, width) {
  const cells = new Map(data.by_hour.filter((c) => c.direction === direction.key).map((c) => [c.key, c]));
  // Drawn at the size it is shown, so the 12px labels stay 12px on a phone.
  const W = Math.max(280, Math.round(width)), H = W < 480 ? 170 : 200, left = 34, right = 6, top = 10, bottom = 24;
  const plotW = W - left - right, plotH = H - top - bottom;
  const top_ = Math.max(10, ...[...cells.values()].map((c) => c.median));
  const step = top_ > 30 ? 10 : 5;
  const max = Math.ceil(top_ / step) * step;
  const slot = plotW / HOURS.length;
  const y = (v) => top + plotH - (Math.max(0, v) / max) * plotH;

  // A group, not an image: an image's children are presentational, so screen readers that
  // follow the spec would drop the bars' own labels.
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "hour-chart", role: "group",
    "aria-label": `Median delay by hour, ${direction.label.toLowerCase()}` });
  for (let v = 0; v <= max; v += step) {
    chart.append(svg("line", { x1: left, x2: W - right, y1: y(v), y2: y(v), class: v === 0 ? "base" : "grid" }));
    const label = svg("text", { x: left - 6, y: y(v) + 4, class: "tick", "text-anchor": "end" });
    label.textContent = v;
    chart.append(label);
  }
  HOURS.forEach((hour, i) => {
    const x = left + i * slot;
    if (hour % 3 === 1) {
      const label = svg("text", { x: x + slot / 2, y: H - 6, class: "tick", "text-anchor": "middle" });
      label.textContent = String(hour).padStart(2, "0");
      chart.append(label);
    }
    const cell = cells.get(hour);
    const group = cell
      ? svg("g", { class: "slot", tabindex: "0" })
      : svg("g", { class: "slot", "aria-hidden": "true" }); // an hour with no trains
    // A hit area wider and taller than the bar.
    group.append(svg("rect", { x, y: top, width: slot, height: plotH, class: "hit" }));
    if (cell) {
      const height = Math.max(2, y(0) - y(cell.median));
      const barW = Math.max(4, slot - 4);
      group.append(svg("path", {
        d: roundedTop(x + 2, y(0) - height, barW, height, Math.min(4, barW / 2)),
        class: cell.few ? "bar few" : "bar",
      }));
      group.setAttribute("role", "img");
      group.setAttribute("aria-label", cellLines(cell, `${hourLabel(hour)}`).join(". "));
      const lines = cellLines(cell, `${hourLabel(hour)}–${hourLabel((hour + 1) % 24)}, ${direction.label}`);
      group.addEventListener("pointerenter", () => showTip(group, lines));
      group.addEventListener("focus", () => showTip(group, lines));
      group.addEventListener("pointerleave", hideTip);
      group.addEventListener("blur", hideTip);
    }
    chart.append(group);
  });
  if ([...cells.values()].every((c) => Math.round(c.median) < 1)) {
    // Nothing to draw: say it instead of showing a flat line.
    return el("figure", { class: "chart" },
      el("figcaption", {}, el("strong", { text: direction.label })),
      el("p", { class: "chart-note", text: cells.size ? onTimeNote(cells, data) : "No trains seen in this direction yet." }));
  }
  return el("figure", { class: "chart" },
    el("figcaption", {}, el("strong", { text: direction.label }), el("span", { text: " · minutes late, by scheduled hour" })),
    chart);
}

/** Every hour's median is on time: say so, and keep the hours where some trains were late. */
function onTimeNote(cells, data) {
  const ordered = [...cells.values()].sort((a, b) => HOURS.indexOf(a.key) - HOURS.indexOf(b.key));
  const hours = ordered.map((c) => String(c.key).padStart(2, "0")).join(", ");
  const late = ordered.filter((c) => c.share_late > 0)
    .map((c) => `${hourLabel(c.key)} ${pct(c.share_late)} of ${c.runs}`);
  return `Typically on time (median 0 min) in every hour with trains: ${hours}.`
    + (late.length ? ` Some were ${data.late_minutes}+ min late: ${late.join(", ")}.` : "");
}

/** A bar with rounded top corners, square at the baseline. */
function roundedTop(x, y, w, h, r) {
  r = Math.min(r, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function drawCharts(data) {
  hideTip();
  const charts = $("charts");
  charts.replaceChildren(...data.directions.map((d) => hourChart(data, d, 600)));
  // The figure's inner width, now that it is laid out (padding 0.75rem each side).
  const inner = charts.firstElementChild.clientWidth - 24;
  charts.replaceChildren(...data.directions.map((d) => hourChart(data, d, inner)));
}

/** The headline: each direction as a line on a platform board, then the same in words. */
function verdict(data) {
  const label = (key) => data.directions.find((d) => d.key === key).label;
  const at = data.stations.length === 1 ? ` at ${data.stations[0]}` : "";
  const rows = data.by_direction.map((d) => {
    const late = Math.round(d.median) >= 1;
    return el("div", { class: "board-row" },
      el("span", { class: "dest", text: label(d.direction).toUpperCase() }),
      el("span", { class: `state${Math.round(d.median) >= data.late_minutes ? " late" : ""}`, text: late ? `${Math.round(d.median)} LATE` : "ON TIME" }));
  });
  $("verdict-board").replaceChildren(...rows);
  $("verdict").textContent = data.by_direction.map((d) => {
    const typical = Math.round(d.median) >= 1
      ? `half the trains were at least ${minutes(d.median)} late${at}`
      : `trains were typically on time${at}`;
    return `${label(d.direction)}, ${typical}, and ${pct(d.share_late)} were ${data.late_minutes}+ min late (${d.runs} train${d.runs === 1 ? "" : "s"}${d.few ? ", too few to go by" : ""}).`;
  }).join(" ");
}

// ---------- page ----------

const KIND_PARAM = "days";
let current = null; // the numbers on show: every day, or one kind of day

/** "3 weekdays and 1 weekend day or holiday" (days with trains seen), like sitt-patterns. */
function kindsText(data) {
  const days = Object.fromEntries(data.day_kinds.map((k) => [k.key, k.days]));
  const s = (n) => (n === 1 ? "" : "s");
  return `${days.weekday} weekday${s(days.weekday)} and ${days.weekend} weekend day${s(days.weekend)} or holiday${s(days.weekend)}`;
}

/** The whole data with one kind of day's numbers in place of the all-days ones. */
function viewOf(data, kind) {
  if (kind === "all" || !data.split_ready || !data.by_kind[kind]) return { ...data, kind: "all" };
  return { ...data, ...data.by_kind[kind], kind };
}

function summaryText(view) {
  const kind = view.kind === "all" ? "" : `${view.day_kinds.find((k) => k.key === view.kind).label}: `;
  if (!view.runs) return `${kind}no trains seen.`;
  return `${kind}${view.runs} trains seen at ${view.stations.join(", ")}, ${dateText(view.first_day)}${view.last_day !== view.first_day ? ` to ${dateText(view.last_day)}` : ""} (${view.days} day${view.days === 1 ? "" : "s"}).`;
}

function daysPicker(data, chosen) {
  if (!data.split_ready) return;
  const options = [{ key: "all", label: "All days" }, ...data.day_kinds];
  $("days-toggle").replaceChildren(...options.map((o) => el("label", {},
    el("input", { type: "radio", name: "days", value: o.key, checked: o.key === chosen }),
    ` ${o.label}`)));
  $("days-toggle").addEventListener("change", (event) => {
    const kind = event.target.value;
    const url = new URL(location.href);
    if (kind === "all") url.searchParams.delete(KIND_PARAM); else url.searchParams.set(KIND_PARAM, kind);
    history.replaceState(null, "", url);
    showView(viewOf(data, kind));
  });
  $("days-pick").hidden = false;
}

/** How much the numbers on show rest on: for the view chosen, not every day. */
function caveat(view) {
  const thin = view.days < 7 || view.stations.length < 2;
  $("caveat").replaceChildren(
    el("strong", { text: thin ? "Early days. " : "Read with care. " }),
    `${view.runs} trains over ${view.days} day${view.days === 1 ? "" : "s"} at ${view.stations.length} station${view.stations.length === 1 ? "" : "s"}. `,
    thin ? "One bad morning can move these numbers a lot. " : "",
    view.split_ready
      ? "Monsoon days aren't told apart yet. "
      : `Weekdays and weekends are shown apart once trains have been seen on at least ${view.split_min_days} days of each; so far ${kindsText(view)}. `,
    "Delays are what NTES last said before the train's time, not measured departures.",
  );
}

function showView(view) {
  current = view;
  $("summary").textContent = summaryText(view);
  caveat(view);
  verdict(view);
  bandTable(view);
  drawCharts(view);
  $("types").replaceChildren(...view.by_type.map((t) =>
    el("li", {}, el("strong", { text: t.type === "fast" ? "Fast trains" : "Slow trains" }),
      `: median ${minutes(t.median)} late, ${pct(t.share_late)} were ${view.late_minutes}+ min late (${t.runs} trains).`)));
}

/** How far the readings are from what sitt-retrain needs. Every day of readings, not the
 * Days switch's view: the model trains on all of them. */
function modelProgress(model) {
  if (!model) return; // synthetic data, or no readings
  const number = (n) => n.toLocaleString("en-IN");
  if (model.in_use) {
    $("model-note").textContent = `A delay model trained on these readings (${model.in_use}) is in use; it is retrained with uv run sitt-retrain.`;
  } else if (model.ready) {
    $("model-note").textContent = "There are enough real readings to train a delay model: run uv run sitt-retrain on the PC. Until one passes its checks, predictions use each train's typical past delay (or the timetable), plus live readings.";
  } else {
    $("model-note").textContent = "Until there are enough real readings to train a delay model, predictions use each train's typical past delay (or the timetable), plus live readings. sitt-retrain checks these numbers; each Check live first adds to them.";
  }
  if (!model.in_use) {
    $("model-gates").replaceChildren(...model.gates.map((g) =>
      el("li", {}, el("strong", { text: g.name[0].toUpperCase() + g.name.slice(1) }),
        `: ${number(g.have)} of ${number(g.need)}${g.met ? " (enough)" : ""}`)));
  }
  $("model").hidden = false;
}

function render(data) {
  if (!data.runs) {
    $("summary").textContent = "No real readings yet.";
    $("message").textContent = "There are no real readings to show yet. Press Check live first on the main page (or run uv run sitt-collect) a few times, at different times of day, and come back.";
    $("message").hidden = false;
    return;
  }
  $("late-def").textContent = `${data.late_minutes}+ minutes`;
  $("min-runs").textContent = String(data.min_runs);
  $("close").textContent = String(data.close_minutes);
  const asked = new URLSearchParams(location.search).get(KIND_PARAM);
  const view = viewOf(data, asked || "all");
  $("content").hidden = false; // before drawing: the charts are sized to the page
  daysPicker(data, view.kind);
  showView(view);
  modelProgress(data.model);
  let lastWidth = $("charts").clientWidth;
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if ($("charts").clientWidth !== lastWidth) { lastWidth = $("charts").clientWidth; drawCharts(current); }
    }, 150);
  });
}

async function start() {
  try {
    const response = await fetch("/api/patterns");
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error((body && body.error) || `The server answered ${response.status}.`);
    render(body);
  } catch (error) {
    $("summary").textContent = "";
    $("message").textContent = error instanceof TypeError
      ? "Can't reach the app on your PC. Patterns need it running (uv run sitt-web)."
      : error.message;
    $("message").hidden = false;
  }
}

start();
