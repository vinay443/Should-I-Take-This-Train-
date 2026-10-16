// Should I take this train? — the page. Talks to /api/*; no framework, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const CROWD = { 1: "Empty", 2: "Seats free", 3: "Standing", 4: "Packed", 5: "Can't board" };
const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

let stations = [];          // [{code, name}]
let lastAnswer = null;

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

const hhmm = (iso) => iso.slice(11, 16);
const dayName = (iso) =>
  new Date(iso).toLocaleDateString("en-IN", { weekday: "short", timeZone: "Asia/Kolkata" });

/** Mumbai wall-clock time and date (now by default), whatever the browser's time zone. */
function mumbaiNow(moment = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(moment).map((p) => [p.type, p.value]));
  return { time: `${parts.hour}:${parts.minute}`, date: `${parts.year}-${parts.month}-${parts.day}` };
}

/** "" for today (Mumbai), " tomorrow", or " on Thu 15 Oct". */
function whichDay(iso) {
  const day = iso.slice(0, 10);
  const today = mumbaiNow().date;
  const tomorrow = new Date(`${today}T12:00:00+05:30`);
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (day === today) return "";
  if (day === tomorrow.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" })) return " tomorrow";
  return ` on ${new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" })}`;
}

function delayText(minutes) {
  if (minutes === 0) return "on time";
  return `${Math.abs(minutes)} min ${minutes > 0 ? "late" : "early"}`;
}

function stationLabel(code) {
  const s = stations.find((x) => x.code === code);
  return s ? s.name : code;
}

/** What someone typed ("Kalyan", "kyn", "Kalyan (KYN)") as text the API understands. */
function stationQuery(text) {
  const t = text.trim();
  const m = t.match(/\(([A-Za-z0-9]+)\)\s*$/);
  return m ? m[1] : t;
}

/** The app on the PC can't be reached: it is stopped, the PC is asleep, or the phone has
 * left the Wi-Fi. Not the same as the app answering with an error. */
class OfflineError extends Error {
  constructor() { super("Can't reach the app on your PC."); this.name = "OfflineError"; }
}

/** {body, offline}: offline is true when the service worker answered from its saved copy. */
async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  } catch {
    throw new OfflineError();
  }
  let body = null;
  try { body = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    throw new Error((body && (body.error || (body.detail && JSON.stringify(body.detail)))) ||
      `The server answered ${response.status}.`);
  }
  return { body, offline: response.headers.get("X-Sitt-Offline") === "1" };
}

async function api(path, options = {}) {
  return (await request(path, options)).body;
}

// ---------- saved answers (for when the PC can't be reached) ----------

const KEEP_ANSWERS = 8;

/** A station as typed, as its code when it is a known name or code. */
function canonicalStation(text) {
  const t = stationQuery(text).toLowerCase();
  const s = stations.find((x) => x.code.toLowerCase() === t || x.name.toLowerCase() === t);
  return s ? s.code : t;
}

function tripKey(from, to, params) {
  return [canonicalStation(from), canonicalStation(to), params.get("at") || "", params.get("date") || ""].join("|");
}

function saveAnswer(params, answer) {
  const all = store.get("answers", {});
  const entry = { saved_at: new Date().toISOString(), answer };
  all[tripKey(params.get("from"), params.get("to"), params)] = entry;
  all[tripKey(answer.origin_code, answer.destination_code, params)] = entry;
  const newest = Object.entries(all)
    .sort((a, b) => b[1].saved_at.localeCompare(a[1].saved_at))
    .slice(0, KEEP_ANSWERS * 2); // each answer is kept under up to two keys
  store.set("answers", Object.fromEntries(newest));
}

function savedAnswer(params) {
  return store.get("answers", {})[tripKey(params.get("from"), params.get("to"), params)] || null;
}

function setOffline(offline) {
  const line = $("data-line");
  line.classList.toggle("offline", offline);
  if (offline) line.textContent = "Offline: can't reach the app on your PC. Showing what was saved on this phone.";
}

function showMessage(text) {
  const m = $("message");
  m.textContent = text || "";
  m.hidden = !text;
}

let toastTimer;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

// ---------- status and stations ----------

async function loadStatus() {
  try {
    const { body: s, offline } = await request("/api/status");
    if (offline) { setOffline(true); return; }
    const line = $("data-line");
    line.classList.remove("offline");
    line.replaceChildren();
    if (!s.database || !s.trains) {
      line.textContent = "No timetable loaded yet. Run uv run sitt-init-db and load one (see the README).";
      return;
    }
    line.append(
      s.sample_timetable ? "Sample timetable (invented trains). " : `Central line timetable, ${s.trains} trains. `,
    );
    if (s.last_collected) {
      line.append("Live data last fetched ", el("strong", { text: friendlyAge(s.last_collected) }), ".");
    } else {
      line.append("No live data fetched yet.");
    }
  } catch (error) {
    if (error instanceof OfflineError) setOffline(true);
    else $("data-line").textContent = error.message;
  }
}

function friendlyAge(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 90) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} hours ago`;
  return `${Math.round(minutes / 1440)} days ago (${hhmm(iso)} on ${new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" })})`;
}

async function loadStations() {
  try {
    stations = await api("/api/stations");
    store.set("stations", stations);
  } catch (error) {
    stations = store.get("stations", []);
    if (!(error instanceof OfflineError) || !stations.length) throw error;
  }
  $("station-list").replaceChildren(
    ...stations.map((s) => el("option", { value: s.name === s.code ? s.code : `${s.name} (${s.code})` })),
  );
}

// ---------- the trip ----------

function readTrip() {
  const params = new URLSearchParams({
    from: stationQuery($("from").value),
    to: stationQuery($("to").value),
  });
  if (document.querySelector('input[name="when"]:checked').value === "later") {
    if ($("at").value) params.set("at", $("at").value);
    if ($("date").value) params.set("date", $("date").value);
  }
  return params;
}

async function showTrains({ live = false } = {}) {
  const params = readTrip();
  if (!params.get("from") || !params.get("to")) {
    showMessage("Pick where you're starting and where you're going.");
    return;
  }
  const go = $("go"), liveButton = $("live");
  go.disabled = liveButton.disabled = true;
  $("answer").classList.add("loading");
  showMessage("");
  try {
    if (live) {
      liveButton.textContent = "Checking live…";
      const { message } = await api("/api/collect", { method: "POST" });
      toast(message);
      loadStatus();
    }
    const answer = await api(`/api/recommend?${params}`);
    lastAnswer = answer;
    render(answer);
    saveAnswer(params, answer);
    if ($("data-line").classList.contains("offline")) loadStatus(); // the PC is back
    store.set("last-route", { from: $("from").value, to: $("to").value });
    history.replaceState(null, "", `?from=${encodeURIComponent(answer.origin_code)}&to=${encodeURIComponent(answer.destination_code)}`);
  } catch (error) {
    if (!(error instanceof OfflineError)) {
      showMessage(error.message);
      return;
    }
    setOffline(true);
    const saved = savedAnswer(params);
    if (saved) {
      render(saved.answer, saved.saved_at);
      if (live) toast("Checking live needs the app on your PC.");
    } else {
      $("answer").hidden = true;
      showMessage("Can't reach the app on your PC, and there is no saved answer for this trip on this phone yet. Check that the PC is on and sitt-web is running.");
    }
  } finally {
    go.disabled = liveButton.disabled = false;
    liveButton.textContent = "Check live first";
    $("answer").classList.remove("loading");
  }
}

// ---------- rendering ----------

function boardRow(option, kind) {
  const late = option.departure_delay > 0;
  const state = option.cancelled ? "CANCELLED" : late ? `${option.departure_delay} LATE` : "ON TIME";
  return el("div", { class: `board-row ${kind}` },
    el("span", { text: hhmm(option.departs) }),
    el("span", { class: "dest", text: (option.other_day ? `${dayName(option.departs)} ` : "") + option.destination.toUpperCase() }),
    el("span", { text: option.type === "fast" ? "F" : "S" }),
    el("span", { class: "cars", text: String(option.cars) }),
    el("span", { class: `state ${late || option.cancelled ? "late" : ""}` },
      kind === "pick" ? el("span", { class: "marker", text: "▶ " }) : null, state),
  );
}

/** Show an answer. `savedAt` (ISO) marks one kept from earlier, shown because the PC
 * can't be reached: it is labelled as not live. */
function render(answer, savedAt = null) {
  $("answer").hidden = false;
  $("answer").classList.toggle("stale", Boolean(savedAt));
  const stale = $("stale");
  stale.hidden = !savedAt;
  if (savedAt) {
    const at = mumbaiNow(new Date(savedAt));
    stale.replaceChildren(
      el("strong", { text: "Not live. " }),
      `Can't reach the app on your PC, so this is the answer saved at ${at.time}${whichDay(`${at.date}T${at.time}`)}. `,
      "Delays and crowding may have changed since: check the station board.",
    );
  }
  const saved = savedAt ? stillToCome(answer) : null;
  if (saved) answer = saved.answer;
  const options = answer.options;
  const pick = answer.choice == null ? null : options[answer.choice];

  const board = $("board");
  board.replaceChildren();
  if (pick) {
    board.append(boardRow(pick, "pick"));
    options
      .filter((o) => o !== pick && !o.cancelled && o.departs > pick.departs)
      .slice(0, 2)
      .forEach((o) => board.append(boardRow(o, "then")));
  } else if (saved && options.length) {
    // The picked train has left: list what is still to come, with no pick.
    options.filter((o) => !o.cancelled).slice(0, 3).forEach((o) => board.append(boardRow(o, "then")));
  } else {
    board.append(el("div", { class: "board-row pick" }, el("span", { class: "dest", text: "NO TRAIN" })));
  }

  $("reason").textContent = !saved || !saved.pickGone
    ? answer.reason
    : options.length
      ? `The train this answer picked (the ${saved.pickGone}) has already left. These saved trains haven't left yet:`
      : "Every train in this saved answer has already left. Reconnect to the PC for a new answer, or check the station board.";
  const basis = $("basis");
  basis.className = answer.synthetic ? "basis warn" : "basis";
  basis.textContent = answer.synthetic
    ? "These delays come from invented (synthetic) data, not real trains."
    : answer.timetable_only
      ? "Timetable times: there is no delay data for these trains yet."
      : answer.level === "timetable"
        ? "Timetable times, with live readings where marked."
        : `Times use the ${answer.level_text}${options.some((o) => o.live) ? ", with live readings where marked" : ""}.`;
  const notes = [...answer.notes];
  if (saved && saved.gone && options.length) {
    notes.unshift(`${saved.gone} train${saved.gone === 1 ? "" : "s"} in the saved answer ${saved.gone === 1 ? "has" : "have"} left since and ${saved.gone === 1 ? "is" : "are"} not shown.`);
  }
  $("notes").replaceChildren(...notes.map((n) => el("li", { text: n })));

  $("trains-heading").textContent = `${answer.origin} to ${answer.destination}, from ${hhmm(answer.asked_at)}${whichDay(answer.asked_at)}`;
  $("trains").replaceChildren(...options.map((o, i) => trainRow(o, i, answer)));
  $("explanation").textContent = answer.explanation;
  // The explanation is about the picked train; once it has left it no longer applies.
  document.querySelector(".why").hidden = Boolean(saved && saved.pickGone);
}

/** A saved answer without the trains that have left (predicted departure more than a
 * minute ago). `gone` counts them; `pickGone` names the picked train if it has left. */
function stillToCome(answer, now = Date.now()) {
  const left = (o) => new Date(o.predicted_departure).getTime() < now - 60_000;
  const options = answer.options.filter((o) => !left(o));
  const pick = answer.choice == null ? null : answer.options[answer.choice];
  const pickGone = pick && left(pick) ? `${hhmm(pick.departs)} ${pick.type}` : null;
  return {
    answer: { ...answer, options, choice: pick && !pickGone ? options.indexOf(pick) : null },
    gone: answer.options.length - options.length,
    pickGone,
  };
}

function trainRow(o, index, answer) {
  const isPick = index === answer.choice;
  const li = el("li", { class: `train${isPick ? " pick" : ""}${o.cancelled ? " cancelled" : ""}` });
  const more = el("div", { class: "t-more", hidden: true });
  const delayed = o.arrival_delay !== 0;
  const leavesLate = o.departure_delay !== o.arrival_delay && o.departure_delay > 0;
  const arrival = o.cancelled
    ? el("span", { class: "t-arr" }, el("span", { class: "delay late", text: "Cancelled" }))
    : el("span", { class: "t-arr" },
        "Arrives ",
        delayed ? el("s", { text: hhmm(o.arrives) }) : null, delayed ? " " : null,
        el("strong", { text: hhmm(o.predicted_arrival) }),
        o.margin ? ` ±${o.margin}` : null,
        ", ",
        el("span", { class: `delay ${o.arrival_delay > 0 ? "late" : "ontime"}`, text: delayText(o.arrival_delay) }),
        leavesLate ? ` (leaves ${o.departure_delay} min late)` : null,
        o.live ? el("span", { class: "badge-live", title: `NTES reading at ${hhmm(o.live.at)}`, text: "live" }) : null,
      );
  const tags = o.tags.length ? el("span", { class: "tags", text: `  ${o.tags.join(", ")}` }) : null;
  const button = el("button", {
    type: "button",
    "aria-expanded": "false",
    "aria-label": `${hhmm(o.departs)} ${o.type} to ${o.destination}${isPick ? ", recommended" : ""}`,
    onclick: () => {
      const open = more.hidden;
      more.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
    },
  },
    el("span", { class: "t-time" }, hhmm(o.departs), o.other_day ? el("small", { text: dayName(o.departs) }) : null),
    el("span", { class: `t-kind ${o.type}`, title: o.type === "fast" ? "Fast" : "Slow", text: o.type === "fast" ? "F" : "S" }),
    el("span", { class: "t-dest" }, isPick ? "Take this: " : null, o.destination, tags),
    arrival,
    o.crowding
      ? el("span", { class: "t-crowd" },
          el("span", { class: "meter", "data-score": String(o.crowding.score), "aria-hidden": "true" },
            el("i"), el("i"), el("i"), el("i"), el("i")),
          el("span", { class: "crowd-label", text: o.crowding.label }))
      : null,
  );

  more.append(...[
    el("p", { text: `${o.minutes} min journey, train ${o.number || "?"}${o.starts_here ? ", starts here" : ""}.` }),
    o.crowding ? el("p", { text: `Crowding ${o.crowding.score}/5: ${o.crowding.reason}.` }) : null,
    o.live ? el("p", { text: `NTES said ${delayText(o.live.delay)} at ${o.live.station}, ${hhmm(o.live.at)}.` }) : null,
    o.ladies_special ? el("p", { text: "Ladies' special: women only." }) : null,
    el("button", {
      type: "button", class: "link",
      onclick: () => prefillLog(o, answer),
      text: "I'm on this train: log how crowded it is",
    }),
  ].filter(Boolean));
  li.append(button, more);
  return li;
}

/** "Kalyan (KYN)" for a code, as the station boxes show it. */
function stationText(code) {
  const s = stations.find((x) => x.code.toLowerCase() === String(code).toLowerCase());
  return s ? (s.name === s.code ? s.code : `${s.name} (${s.code})`) : String(code);
}

// ---------- saved routes ----------

function renderSaved() {
  const routes = store.get("routes", []);
  $("saved").replaceChildren(...routes.map((r, i) =>
    el("span", { class: "route-chip" },
      el("button", {
        type: "button",
        onclick: () => { $("from").value = r.from; $("to").value = r.to; showTrains(); },
        text: `${shortName(r.from)} to ${shortName(r.to)}`,
      }),
      el("button", {
        type: "button", class: "forget", "aria-label": `Forget ${r.from} to ${r.to}`,
        onclick: () => { routes.splice(i, 1); store.set("routes", routes); renderSaved(); },
        text: "×",
      }),
    )));
}
const shortName = (text) => text.replace(/\s*\([^)]*\)\s*$/, "");

function saveRoute() {
  const from = $("from").value.trim(), to = $("to").value.trim();
  if (!from || !to) { showMessage("Pick both stations before saving the route."); return; }
  const routes = store.get("routes", []).filter((r) => !(r.from === from && r.to === to));
  routes.unshift({ from, to });
  store.set("routes", routes.slice(0, 6));
  renderSaved();
  toast(`Saved ${shortName(from)} to ${shortName(to)}.`);
}

// ---------- crowd log ----------

function buildCrowdOptions() {
  $("crowd-options").replaceChildren(...Object.entries(CROWD).map(([level, label]) =>
    el("label", {},
      el("input", { type: "radio", name: "crowd", value: level, required: true }),
      el("span", { class: "meter", "data-score": level, "aria-hidden": "true" }, el("i"), el("i"), el("i"), el("i"), el("i")),
      label,
    )));
}

function prefillLog(option, answer) {
  $("log-station").value = stationText(answer.origin_code);
  $("log-time").value = hhmm(option.departs);
  document.querySelector(`input[name="service"][value="${option.type}"]`).checked = true;
  $("log-heading").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  $("crowd-options").querySelector("input").focus();
}

async function logCrowding(event) {
  event.preventDefault();
  const crowd = document.querySelector('input[name="crowd"]:checked');
  if (!crowd) { toast("Pick how crowded it was."); return; }
  try {
    const result = await api("/api/reports", {
      method: "POST",
      body: JSON.stringify({
        station: stationQuery($("log-station").value),
        departure: $("log-time").value,
        service: document.querySelector('input[name="service"]:checked').value,
        crowd: Number(crowd.value),
      }),
    });
    toast(result.matched
      ? `Logged: ${result.crowd_label} on the ${result.matched}.`
      : `Logged: ${result.train}, ${result.crowd_label}.`);
    crowd.checked = false;
    loadReports();
  } catch (error) {
    toast(error instanceof OfflineError
      ? "Can't reach the app on your PC, so this wasn't saved. Log it again when the PC is back."
      : error.message);
  }
}

async function loadReports() {
  try {
    const reports = await api("/api/reports?limit=5");
    $("reports").replaceChildren(...reports.map((r) =>
      el("li", {},
        el("span", { text: `${r.train || stationLabel(r.station)}, ${r.crowd_label}` }),
        el("span", { text: new Date(r.reported_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) }),
      )));
  } catch { /* the list is a nicety */ }
}

// ---------- start ----------

function wire() {
  $("trip").addEventListener("submit", (e) => { e.preventDefault(); showTrains(); });
  $("live").addEventListener("click", () => showTrains({ live: true }));
  $("save").addEventListener("click", saveRoute);
  $("swap").addEventListener("click", () => {
    const from = $("from").value;
    $("from").value = $("to").value;
    $("to").value = from;
  });
  document.querySelectorAll('input[name="when"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      const later = radio.value === "later" && radio.checked;
      if (radio.checked) $("later").hidden = !later;
      if (later && !$("at").value) $("at").value = mumbaiNow().time;
    }));
  $("log-form").addEventListener("submit", logCrowding);
  $("log-time").value = mumbaiNow().time;
}

async function start() {
  // Keeps the page on the phone for when the PC can't be reached. Browsers only allow this
  // on https:// or localhost; elsewhere the page still falls back to saved answers while open.
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* the page works without it */ });
  }
  window.addEventListener("online", loadStatus);
  wire();
  buildCrowdOptions();
  renderSaved();
  loadStatus();
  loadReports();
  try {
    await loadStations();
  } catch (error) {
    showMessage(error.message);
    return;
  }
  const url = new URLSearchParams(location.search);
  const last = store.get("last-route", null);
  const label = stationText;
  const from = url.get("from") ? label(url.get("from")) : last?.from || label("KYN");
  const to = url.get("to") ? label(url.get("to")) : last?.to || label("CSMT");
  $("from").value = from;
  $("to").value = to;
  $("log-station").value = from;
  if (stations.length) showTrains();
}

start();
