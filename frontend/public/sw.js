// Serin service worker — network-first for documents and API, cache-first
// only for immutable hashed assets. Bumping the version evicts old caches on
// the next visit (the byte-change alone triggers the update cycle).
const CACHE_VERSION = 'serin-v4-2026-07-06';
const ASSET_PATTERNS = [/^\/assets\//, /^\/icon-/, /^\/favicon/];

self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

// Network-first: serve fresh, refresh the cache, fall back to cache offline.
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (request.method === 'GET' && response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Documents must never be pinned to a stale shell — a cached index.html
  // references old hashed assets and freezes the whole app on first-visit
  // vintage. Network-first, cache only as the offline fallback.
  if (event.request.mode === 'navigate' || url.pathname === '/') {
    event.respondWith(networkFirst(event.request));
    return;
  }

  // API calls are network-first too — finance data must be fresh.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(event.request));
    return;
  }

  // Immutable hashed assets — cache-first is safe: a new build means new URLs.
  if (event.request.method === 'GET' && ASSET_PATTERNS.some(p => p.test(url.pathname))) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        const response = await fetch(event.request);
        if (response.ok) {
          const clone = response.clone();
          const cache = await caches.open(CACHE_VERSION);
          cache.put(event.request, clone);
        }
        return response;
      })()
    );
  }
});
