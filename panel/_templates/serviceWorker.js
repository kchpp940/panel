const appName = '{{ name }}'
const appCacheName = '{{ name }}-{{ uuid }}';

const preCacheFiles = [{{ pre_cache }}];

self.addEventListener('install', (e) => {
  console.log('[Service Worker] Install');
  self.skipWaiting();
  e.waitUntil((async () => {
    const cacheNames = await caches.keys();
    for (const cacheName of cacheNames) {
      if (cacheName.startsWith(appName) && cacheName !== appCacheName) {
        console.log(`[Service Worker] Delete old cache ${cacheName}`);
        await caches.delete(cacheName);
      }
    }
    const cache = await caches.open(appCacheName);
    console.log('[Service Worker] Caching ');
    await Promise.all(preCacheFiles.map(async (cacheFile) => {
      const request = new Request(cacheFile);
      try {
        const response = await fetch(request);
        if (response.ok || response.type == 'opaque') {
          await cache.put(request, response);
        }
      } catch (err) {
        console.warn(`[Service Worker] Failed to cache ${cacheFile}:`, err);
      }
    }));
  })());
});

self.addEventListener('activate', (event) => {
  console.log('[Service Worker] Activating');
  return self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') {
    return
  }
  e.respondWith((async () => {
    const cache = await caches.open(appCacheName);
    const cached = await cache.match(e.request);
    const url = new URL(e.request.url);
    console.log(`[Service Worker] Fetching resource: ${e.request.url}`);
    if (cached) {
      return cached;
    }
    try {
      const response = await fetch(e.request);
      if (!response.ok && !(response.type == 'opaque')) {
        console.warn(`[Service Worker] Fetch returned non-ok status: ${response.status} for ${url.pathname}`);
        return response;
      }
      if (response.type === 'basic' || response.type === 'cors' || response.type === 'opaque') {
        console.log(`[Service Worker] Caching new resource: ${e.request.url}`);
        try {
          await cache.put(e.request, response.clone());
        } catch (cacheErr) {
          console.warn(`[Service Worker] Failed to cache ${url.pathname}:`, cacheErr);
        }
      }
      return response;
    } catch (err) {
      console.warn(`[Service Worker] Network fetch failed for ${url.pathname}:`, err);
      if (cached) {
        return cached;
      }
      throw err;
    }
  })());
});
