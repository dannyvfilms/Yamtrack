const CACHE_NAME = "yamtrack-v3";
const urlsToCache = [
  "/static/css/main.css",
  "/static/favicon/android-chrome-192x192.png",
  "/static/favicon/android-chrome-512x512.png",
  "/static/fonts/roboto-flex.woff2",
];

// Install event
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(urlsToCache);
    }),
  );
  // Activate this worker immediately; don't wait for old one to finish
  self.skipWaiting();
});

// Fetch event
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Only cache same-origin static assets.
  const isSameOrigin = url.origin === self.location.origin;
  const isHtmxRequest = request.headers.get("HX-Request") === "true";

  // Keep app routes and HTMX requests on network to avoid stale dynamic HTML.
  if (
    request.method !== "GET" ||
    !isSameOrigin ||
    isHtmxRequest ||
    !url.pathname.startsWith("/static/")
  ) {
    event.respondWith(fetch(request));
    return;
  }

  // Cache-first for static assets only.
  event.respondWith(
    caches.match(request).then((response) => {
      if (response) {
        return response;
      }

      return fetch(request).then((networkResponse) => {
        const responseClone = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone));
        return networkResponse;
      });
    }),
  );
});

// Activate event
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((cacheNames) =>
        Promise.all(
          cacheNames.map((cacheName) => {
            if (cacheName !== CACHE_NAME) {
              return caches.delete(cacheName);
            }

            return undefined;
          }),
        ),
      )
      .then(() => self.clients.claim()),
  );
});
