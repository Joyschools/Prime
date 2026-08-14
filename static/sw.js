const CACHE_NAME = "school-system-v1";
const ASSETS = [
  "/",
  "/login",
  "/dashboard",
  "/profile",
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/manifest.json",
  "/sw.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((key) => key !== CACHE_NAME ? caches.delete(key) : Promise.resolve())))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith((async () => {
    try {
      const networkResponse = await fetch(event.request);
      const cache = await caches.open(CACHE_NAME);
      cache.put(event.request, networkResponse.clone());
      return networkResponse;
    } catch {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      const fallback = await caches.match("/");
      return fallback || Response.error();
    }
  })());
});
