// Service worker: makes the site installable and keeps recently read pages available offline.
// Newsroom (/admin) and members' private pages are never cached.
const CACHE = "newsroom-v2";
const PRIVATE = ["/admin", "/me", "/submit", "/login", "/join", "/reset", "/confirm", "/forgot"];
const SHELL = ["/", "/offline", "/static/public/site.css", "/static/public/site.js", "/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || PRIVATE.some((p) => url.pathname.startsWith(p)) || url.pathname.endsWith("/manage")) return;
  if (e.request.mode === "navigate") {
    // pages: network first, fall back to the cached copy, then the offline page
    e.respondWith(fetch(e.request).then((res) => {
      const copy = res.clone();
      if (res.ok) caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match("/offline"))));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/media/")) {
    // files: cache first
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request).then((res) => {
      const copy = res.clone();
      if (res.ok) caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    })));
  }
});
