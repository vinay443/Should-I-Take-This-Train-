// Service worker: lets the page open when the app on the PC can't be reached.
// Served at /sw.js by app.py, which fills in VERSION (a hash of the static files), PAGES
// (the app's own pages) and SHELL (every file to keep; styles and scripts carry ?v=VERSION,
// exactly as the pages link them, so a new page never runs with an old cached script).
// It keeps only the pages, their files, the station list and the status line. Answers are
// kept by the page (app.js), so the "not live" fallback also works where workers can't run.
"use strict";

const VERSION = "__VERSION__";
const PAGES = __PAGES__;
const SHELL = __SHELL__;
const CACHE = `sitt-shell-${VERSION}`;
// Small, slowly changing lookups the page needs to start: network first, cached copy if not.
const LOOKUPS = ["/api/stations", "/api/status"];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // "reload" skips the browser's HTTP cache, so a new version never stores old files.
    await cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" })));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("sitt-shell-") && name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

/** From the app if it answers; otherwise the copy kept under `key`, marked as offline.
 * Only responses for `key` itself are stored, so no other page can replace it. */
async function networkFirst(request, key, store) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (store && response.ok) await cache.put(key, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(key);
    if (!cached) throw error;
    const headers = new Headers(cached.headers);
    headers.set("X-Sitt-Offline", "1"); // tells the page this copy is not fresh
    return new Response(cached.body, { status: cached.status, headers });
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request); // full URL, version included
  return cached || fetch(request);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // collecting and logging always go to the app
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    // The app's own pages are kept (whatever ?from=…&to= they were opened with). Any other
    // page (the API docs, raw JSON) is never stored; offline it falls back to the main page.
    const page = PAGES.includes(url.pathname);
    event.respondWith(networkFirst(request, page ? url.pathname : "/", page));
  } else if (LOOKUPS.includes(url.pathname)) {
    event.respondWith(networkFirst(request, url.pathname, true));
  } else if (SHELL.includes(url.pathname + url.search)) {
    event.respondWith(cacheFirst(request));
  }
  // Everything else (recommendations, reports, patterns, the API docs) goes straight to
  // the network.
});
