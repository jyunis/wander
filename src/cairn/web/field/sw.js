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

// Precache the EXACT urls index.html asks for, query string and all. Precaching
// bare './field.js' and matching with ignoreSearch made the ?v= token inert.
const SHELL_URLS = [
  './',
  `./field.css?v=${BUILD}`,
  `./field.js?v=${BUILD}`,
  './manifest.webmanifest',
  './vendor/maplibre-gl.js',
  './vendor/maplibre-gl.css',
  './vendor/pmtiles.js',
  // Road-label glyphs. URL-encoded exactly as MapLibre requests them
  // ({fontstack} = "Noto Sans Medium"), so the cache match is exact.
  './vendor/fonts/Noto%20Sans%20Medium/0-255.pbf',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

// Ask a page whether it's busy. It answers on a private port; a page running an
// older build has no listener, so we time out and treat silence as "not busy".
function isBusy(client) {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const giveUp = setTimeout(() => resolve(false), 300);
    channel.port1.onmessage = (event) => {
      clearTimeout(giveUp);
      resolve(!!event.data?.busy);
    };
    try {
      client.postMessage({ type: 'wf-upgrade-check' }, [channel.port2]);
    } catch {
      clearTimeout(giveUp);
      resolve(false);
    }
  });
}

self.addEventListener('activate', (e) => {
  e.waitUntil(
    (async () => {
      const keys = await caches.keys();
      const stale = keys.filter((k) => k.startsWith('wander-field-shell-') && k !== SHELL);
      await Promise.all(stale.map((k) => caches.delete(k)));
      await self.clients.claim();

      // An UPGRADE, not a first install. The page open right now was served by
      // the previous worker and is running the previous build's field.js, so it
      // cannot reload itself — whatever update-handling code we add to the page
      // only ships in the build that the stale page isn't running. ("I deployed
      // and reloaded and nothing changed.") The worker is the only participant
      // that is always current, so it does the reloading.
      if (!stale.length) return;
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of windows) {
        // Never yank a page that is mid-download: a reload throws away however
        // much of a 31 MB archive it had, and on a tethered phone that hurts.
        // A page running an older build won't answer; it has no download to
        // lose that we know of, and rescuing it is the whole point.
        if (await isBusy(client)) continue;
        try {
          await client.navigate(client.url);
        } catch {
          /* not all browsers allow it; then it's a two-load update, as before */
        }
      }
    })(),
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
  // Exact match first (the versioned asset we precached). The ignoreSearch
  // fallback is safe because each build gets its own cache, so it can only ever
  // match this build's copy.
  const hit = (await cache.match(request)) || (await cache.match(request, { ignoreSearch: true }));
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

  // The update check must reach the server or fail — never be answered from
  // cache, or an unreachable server would look like "you're up to date".
  if (url.pathname === '/field/version') return;

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
  // Everything under /field/regions/ (archives, outlines) is the page's business:
  // it stores them in its own cache, under its own lifecycle, and deletes them
  // together. Two more reasons to stay out of the way: the Cache API cannot
  // answer a Range request (we'd hand back a whole 200 where MapLibre asked for a
  // 206 slice, corrupting every tile read), and a 31 MB archive has no place in
  // the shell cache we wipe on every deploy.
  if (url.pathname.startsWith('/field/regions/') || req.headers.has('range')) return;

  e.respondWith(cacheFirst(req));
});
