const CACHE_NAME = "fixtrack-shell-v6";
const APP_SHELL = [
  "/",
  "/index.html",
  "/app.js",
  "/notifications.js",
  "/style.css",
  "/mobile-fixes.css",
  "/auth.css",
  "/manifest.webmanifest",
  "/icons/fixtrack.svg",
  "/icons/fixtrack-192.png",
  "/icons/fixtrack-512.png"
];
const APP_ASSETS = new Set(APP_SHELL);

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key.startsWith("fixtrack-shell-") && key !== CACHE_NAME)
        .map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  const isAppShell = url.pathname === "/" || APP_ASSETS.has(url.pathname);
  if (!isAppShell) return;

  event.respondWith(
    fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.put(request, copy)));
      }
      return response;
    }).catch(async () => {
      const cached = await caches.match(request, { ignoreSearch: true });
      if (cached) return cached;
      if (request.mode === "navigate") return caches.match("/");
      return new Response("FixTrack is offline. Reconnect to reach the workshop server.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    })
  );
});
