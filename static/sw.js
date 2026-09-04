// GCSSHG service worker.
// Deliberately minimal: this is a financial app, so pages showing balances,
// loans, and statements must NEVER be served from a stale cache. Only truly
// static assets (logo, icons) are cached. Everything else always goes to
// the network - if the network fails, the app simply shows the browser's
// normal offline error rather than risking outdated numbers.

const CACHE_NAME = 'gcsshg-static-v1';
const STATIC_ASSETS = [
  '/static/logo.svg',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Only cache-first for static assets under /static/icons/ or /static/logo.svg
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }

  // Everything else (pages, forms, data) - always go to the network.
  // No caching of financial pages, ever.
});
