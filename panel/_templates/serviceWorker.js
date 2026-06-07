const appName = '{{ name }}'
const appCacheName = '{{ name }}-{{ uuid }}';

const preCacheFiles = [{{ pre_cache }}];
const runtimeNetworkUrls = [{{ runtime_network }}];

function isRuntimeNetworkRequest(url) {
  const fullUrl = url.toString();
  for (const prefix of runtimeNetworkUrls) {
    if (fullUrl.startsWith(prefix)) {
      return true;
    }
  }
  return false;
}

function buildOfflineResponse(requestUrl) {
  const body = JSON.stringify({
    error: 'OfflineError',
    message: 'Network request failed: the application is currently offline.',
    detail: 'This is a runtime business API URL that requires a live network connection. '
          + 'Static app assets (JS, CSS, wheels) continue to work offline via the service worker cache.',
    url: requestUrl.toString(),
    offlinePolicy: {
      static: 'cache-only',
      runtimeStrategy: 'network-first',
    }
  });
  return new Response(body, {
    status: 503,
    statusText: 'Service Unavailable (Offline)',
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'X-Offline-Error': 'true',
    }
  });
}

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
    console.log('[Service Worker] Pre-caching ' + preCacheFiles.length + ' static asset(s)');
    await Promise.all(preCacheFiles.map(async (cacheFile) => {
      const request = new Request(cacheFile);
      try {
        const response = await fetch(request);
        if (response.ok || response.type == 'opaque') {
          await cache.put(request, response);
        } else {
          console.warn(`[Service Worker] Non-ok status ${response.status} for static asset ${cacheFile}`);
        }
      } catch (err) {
        console.warn(`[Service Worker] Failed to cache static asset ${cacheFile}:`, err);
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
    return;
  }

  const url = new URL(e.request.url);

  // ---------------------------------------------------------------
  // Strategy 1: RUNTIME BUSINESS NETWORK REQUESTS  →  network-first
  // If the request matches a detected business API URL, always try
  // the network first. If the network fails (offline), return a
  // structured 503 JSON error so the app can display an explicit
  // "you are offline" message instead of silently failing.
  // These requests are NOT cached — they are strictly online.
  // ---------------------------------------------------------------
  if (isRuntimeNetworkRequest(e.request.url)) {
    e.respondWith((async () => {
      console.log(`[Service Worker] Runtime network request (network-first): ${e.request.url}`);
      try {
        const response = await fetch(e.request);
        return response;
      } catch (err) {
        console.warn(`[Service Worker] Runtime request FAILED (offline): ${e.request.url}`);
        return buildOfflineResponse(e.request.url);
      }
    })());
    return;
  }

  // ---------------------------------------------------------------
  // Strategy 2: STATIC APP ASSETS  →  cache-first
  // All files in preCacheFiles (HTML, JS, CSS, wheels, images,
  // resources.zip) have been pre-cached on install. Serve from
  // cache whenever possible; only go to the network for cache
  // misses, then store for next time.
  // ---------------------------------------------------------------
  e.respondWith((async () => {
    const cache = await caches.open(appCacheName);
    const cached = await cache.match(e.request);

    console.log(`[Service Worker] Static asset request (cache-first): ${e.request.url}`
                + (cached ? ' [CACHE HIT]' : ' [CACHE MISS]'));

    if (cached) {
      return cached;
    }

    try {
      const response = await fetch(e.request);
      if (!response.ok && !(response.type == 'opaque')) {
        console.warn(`[Service Worker] Static fetch returned non-ok: ${response.status} for ${url.pathname}`);
        return response;
      }
      if (response.type === 'basic' || response.type === 'cors' || response.type === 'opaque') {
        try {
          await cache.put(e.request, response.clone());
          console.log(`[Service Worker] Cached new static asset: ${e.request.url}`);
        } catch (cacheErr) {
          console.warn(`[Service Worker] Failed to cache ${url.pathname}:`, cacheErr);
        }
      }
      return response;
    } catch (err) {
      console.warn(`[Service Worker] Static network fetch failed for ${url.pathname}:`, err);
      if (cached) {
        return cached;
      }
      throw err;
    }
  })());
});
