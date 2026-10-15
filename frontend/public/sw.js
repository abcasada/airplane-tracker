// App-shell service worker. API calls and live data are never cached.
const CACHE = 'shell-v1'

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/', '/manifest.webmanifest', '/icon.svg'])))
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET' || url.origin !== location.origin) return
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz') return

  if (url.pathname.startsWith('/assets/')) {
    // Hashed build assets never change: cache first.
    event.respondWith(
      caches.match(event.request).then(
        (hit) =>
          hit ||
          fetch(event.request).then((resp) => {
            const copy = resp.clone()
            caches.open(CACHE).then((c) => c.put(event.request, copy))
            return resp
          }),
      ),
    )
    return
  }

  if (event.request.mode === 'navigate') {
    // Network first so updates show up; fall back to the cached shell offline.
    event.respondWith(fetch(event.request).catch(() => caches.match('/')))
  }
})
