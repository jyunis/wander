// Wander Field — service worker for the offline shell.
//
// Scope is /field/ only; the main Leaflet app at / is never intercepted.
//
// The build token is substituted server-side (see api.py `field_sw`), so these
// bytes change whenever the shell changes — that's what makes the browser
// install a new worker and re-precache. A static sw.js would pin an installed
// phone to the first shell it ever saw.
const BUILD = '__BUILD__';
const SHELL = `wander-field-shell-${BUILD}`;

const SHELL_URLS = [
  './',
  './field.css',
  './field.js',
  './manifest.webmanifest',
  './vendor/maplibre-gl.js',
  './vendor/maplibre-gl.css',
  './vendor/pmtiles.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k.startsWith('wander-field-shell-') && k !== SHELL).map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request, fallbackUrl) {
  try {
    const fresh = await fetch(request);
    if (fresh.ok) {
      const cache = await caches.open(SHELL);
      cache.put(request, fresh.clone());
    }
    return fresh;
  } catch {
    const cache = await caches.open(SHELL);
    const hit = await cache.match(request, { ignoreSearch: true });
    return hit || (fallbackUrl && (await cache.match(fallbackUrl))) || Response.error();
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(SHELL);
  // ignoreSearch: the page requests ./field.js?v=<token>, we precached ./field.js.
  const hit = await cache.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh.ok) cache.put(request, fresh.clone());
  return fresh;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (!url.pathname.startsWith('/field/')) return;

  // Region archives are the page's business, not ours. Two reasons to stay out
  // of the way: the page stores them in its own cache under its own lifecycle,
  // and the Cache API cannot answer a Range request (it would hand back a whole
  // 200 where MapLibre asked for a 206 slice, corrupting every tile read).
  if (url.pathname.endsWith('.pmtiles') || req.headers.has('range')) return;

  if (req.mode === 'navigate') {
    e.respondWith(networkFirst(req, './'));
    return;
  }
  // The region list is live data — prefer the network, fall back to the last
  // good copy so an offline cold start still knows what a region is called.
  if (url.pathname === '/field/regions') {
    e.respondWith(networkFirst(req));
    return;
  }
  e.respondWith(cacheFirst(req));
});
