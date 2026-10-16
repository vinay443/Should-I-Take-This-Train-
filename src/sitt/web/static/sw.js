// Service worker: lets the page open when the app on the PC can't be reached.
// Served at /sw.js by app.py, which fills in VERSION (a hash of the static files) and SHELL.
// It only caches the page itself and the station list; answers are kept by the page
// (app.js), so the "not live" fallback also works where service workers can't run.
"use strict";

const VERSION = "__VERSION__";
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

async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(fallbackUrl || request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(fallbackUrl || request, { ignoreSearch: true });
    if (!cached) throw error;
    // Tell the page this copy is not fresh.
    const headers = new Headers(cached.headers);
    headers.set("X-Sitt-Offline", "1");
    return new Response(cached.body, { status: cached.status, headers });
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  return cached || fetch(request);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // collecting and logging always go to the app
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    // Each page keeps its own copy (with any ?from=…&to= dropped); unknown pages fall back to "/".
    event.respondWith(networkFirst(request, SHELL.includes(url.pathname) ? url.pathname : "/"));
  } else if (LOOKUPS.includes(url.pathname)) {
    event.respondWith(networkFirst(request));
  } else if (SHELL.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
  // Everything else (recommendations, reports, the API docs) goes straight to the network.
});
