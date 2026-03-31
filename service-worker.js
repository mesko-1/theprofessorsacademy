const CACHE_NAME = "tpa-shell-v5";
const STATIC_ASSETS = [
  "/",
  "/index.html",
  "/favicon.svg",
  "/site.webmanifest",
  "/static/icons/icon-32.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

const NETWORK_ONLY_PREFIXES = [
  "/adminpanel1010tpa",
  "/adminpanel0109tpa2026",
  "/api/",
  "/uploads/",
  "/health",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)).catch(() => null),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (NETWORK_ONLY_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(async () => (await caches.match("/")) || caches.match("/index.html")),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          if (!response || response.status !== 200) return response;
          if (url.pathname.startsWith("/static/icons/") || url.pathname === "/favicon.svg") {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone)).catch(() => null);
          }
          return response;
        })
        .catch(() => caches.match("/favicon.svg"));
    }),
  );
});
