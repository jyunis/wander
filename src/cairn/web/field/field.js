// Wander Field — offline map + live GPS. "Plan online, navigate offline."
//
// The contract this file has to keep: once a region is downloaded, NOTHING here
// touches the network. Tiles come out of a blob in the Cache API, the position
// comes from the device GPS, and the region list is read back out of that same
// cache. A cold start in airplane mode, days later, draws a map and a blue dot.
//
// Two MapLibre + PMTiles traps are load-bearing below; both silently blank the
// map rather than erroring, so they're marked where they bite.

(() => {
  'use strict';

  const DATA_CACHE = 'wander-field-regions-v1';
  const PMTILES_HEADER_LEN = 127;

  const $ = (id) => document.getElementById(id);
  const fmtBytes = (n) => (n == null ? '—' : (n / 1048576).toFixed(1) + ' MB');
  const ago = (ts) => {
    if (!ts) return 'downloaded';
    const d = (Date.now() - ts) / 86400000;
    return d < 1 ? Math.round(d * 24) + 'h ago' : d.toFixed(1) + 'd ago';
  };
  const titleize = (id) => id.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

  const archiveUrl = (id) => `/field/regions/${id}.pmtiles`;
  const metaUrl = (id) => `/field/regions/${id}.meta.json`; // cache key only; never fetched

  let map = null;
  let activeRegion = null;
  let activeSource = null; // 'offline' (from the cached blob) | 'stream' (over the wire)
  let fix = null; // last GeolocationPosition
  // Off until there's a fix to follow. It used to start true, which did nothing
  // (the first fix lands before the map exists) while the button read "off".
  let following = false;
  let protocol = null;

  // iOS is the only platform where an un-installed PWA gets the flimsy storage
  // bucket, so it's the only one worth nagging. iPadOS lies and reports MacIntel.
  const IS_IOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  const bboxPolygon = ([w, s, e, n]) => ({
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
  });

  // The whole world with the region punched out of it, so everywhere you have no
  // data reads as "no data" instead of "the map is broken".
  const maskPolygon = ([w, s, e, n]) => ({
    type: 'Feature',
    properties: {},
    geometry: {
      type: 'Polygon',
      coordinates: [
        [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]],
        [[w, s], [w, n], [e, n], [e, s], [w, s]], // hole, wound the other way
      ],
    },
  });

  // Protomaps packs lakes (polygons), rivers (lines) and label anchors (points)
  // into ONE `water` source-layer, and MapLibre does not filter a layer's bucket
  // by geometry type: a `fill` layer handed a LineString earcuts it into a long
  // tapering sliver across the map, and a `line` layer strokes every lake's
  // outline. Every basemap layer below therefore states the geometry it wants.
  const POLYGONS = () => ['==', ['geometry-type'], 'Polygon']; // also matches MultiPolygon
  const LINES = () => ['==', ['geometry-type'], 'LineString']; // also matches MultiLineString

  const insideBbox = ([w, s, e, n], lon, lat) => lon >= w && lon <= e && lat >= s && lat <= n;

  const fixIsInsideRegion = () =>
    !!fix && !!activeRegion && insideBbox(activeRegion.bbox, fix.coords.longitude, fix.coords.latitude);

  // --- what we've downloaded ------------------------------------------------
  //
  // The Cache API is the ONLY source of truth for "do I have this region." An
  // earlier cut kept the index in localStorage, which cost a whole offline cold
  // start: the 31 MB archive was sitting right there, but the receipt was gone,
  // so the app said "no offline map." They're separately-evictable storage
  // classes and localStorage is what iOS drops first. So a region's metadata is
  // written NEXT TO ITS BYTES, in the same cache, and rides the same eviction.
  //
  // Even that is only an optimization: a PMTiles archive is self-describing, so
  // if the metadata is missing we recover bbox/zooms from the file's own header
  // (the same 127 bytes cairn.field parses server-side). Losing it costs the
  // display name, never the map.

  async function readPmtilesHeader(blob) {
    if (blob.size < PMTILES_HEADER_LEN) return null;
    const buf = await blob.slice(0, PMTILES_HEADER_LEN).arrayBuffer();
    const view = new DataView(buf);
    const magic = new TextDecoder().decode(new Uint8Array(buf, 0, 7));
    if (magic !== 'PMTiles' || view.getUint8(7) !== 3) return null;
    const e7 = (off) => view.getInt32(off, true) / 1e7;
    return {
      bbox: [e7(102), e7(106), e7(110), e7(114)],
      center: [e7(119), e7(123)],
      min_zoom: view.getUint8(100),
      max_zoom: view.getUint8(101),
    };
  }

  async function cachedRegions() {
    if (!('caches' in window)) return [];
    const cache = await caches.open(DATA_CACHE);
    const out = [];
    for (const req of await cache.keys()) {
      const path = new URL(req.url).pathname;
      if (!path.endsWith('.pmtiles')) continue;
      const id = path.slice(path.lastIndexOf('/') + 1, -'.pmtiles'.length);
      const res = await cache.match(req);
      if (!res) continue;
      const blob = await res.blob(); // lazy — .size and .slice() don't read 31 MB

      let meta = null;
      const metaRes = await cache.match(metaUrl(id));
      if (metaRes) {
        try { meta = await metaRes.json(); } catch { /* fall back to the header */ }
      }
      const head = meta || (await readPmtilesHeader(blob));
      if (!head) continue; // truncated/corrupt archive — don't offer it

      out.push({
        id,
        url: path,
        name: meta?.name || titleize(id),
        bbox: head.bbox,
        center: head.center,
        min_zoom: head.min_zoom,
        max_zoom: head.max_zoom,
        size_bytes: blob.size,
        ts: meta?.ts,
      });
    }
    return out.sort((a, b) => a.name.localeCompare(b.name));
  }

  async function cachedBlob(region) {
    const cache = await caches.open(DATA_CACHE);
    const res = await cache.match(region.url);
    return res ? await res.blob() : null;
  }

  // --- map ------------------------------------------------------------------

  // A no-label style. GOTCHA: `glyphs`/`sprite` must be OMITTED, not set to
  // undefined — MapLibre's style validation rejects `undefined` ("string
  // expected"), aborts the style load, and never registers the source. The map
  // then renders as a blank canvas with no error in the console.
  function styleFor(sourceKey, region) {
    return {
      version: 8,
      sources: {
        pm: { type: 'vector', url: `pmtiles://${sourceKey}` },
        me: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        meacc: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
        bounds: { type: 'geojson', data: bboxPolygon(region.bbox) },
        mask: { type: 'geojson', data: maskPolygon(region.bbox) },
      },
      layers: [
        { id: 'bg', type: 'background', paint: { 'background-color': '#eae6da' } },
        {
          id: 'earth', type: 'fill', source: 'pm', 'source-layer': 'earth',
          filter: POLYGONS(), paint: { 'fill-color': '#e6e1d3' },
        },
        {
          id: 'landcover', type: 'fill', source: 'pm', 'source-layer': 'landcover',
          filter: POLYGONS(),
          paint: {
            'fill-color': ['match', ['get', 'kind'],
              ['forest', 'scrub', 'wood'], '#cdddba',
              ['grassland', 'grass', 'meadow'], '#dbe6c8',
              ['glacier', 'ice'], '#e8eef2',
              '#e2ddce'],
            'fill-opacity': 0.7,
          },
        },
        {
          id: 'landuse', type: 'fill', source: 'pm', 'source-layer': 'landuse',
          filter: POLYGONS(),
          paint: {
            'fill-color': ['match', ['get', 'kind'],
              ['park', 'national_park', 'forest', 'nature_reserve', 'protected_area'], '#c9dcb2',
              ['residential', 'industrial'], '#e4ded0',
              '#e2ddce'],
            'fill-opacity': 0.5,
          },
        },
        {
          id: 'water', type: 'fill', source: 'pm', 'source-layer': 'water',
          filter: POLYGONS(), paint: { 'fill-color': '#9fc4de' },
        },
        {
          id: 'water-line', type: 'line', source: 'pm', 'source-layer': 'water',
          filter: LINES(), // rivers and streams; NOT an outline around every lake
          paint: { 'line-color': '#9fc4de', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.4, 14, 1.6] },
        },
        // GOTCHA: line-dasharray takes constants or zoom stops only — it CANNOT
        // be data-driven (['match', ['get', 'kind'], …]). Dashed features get
        // their own layer with a constant dash instead of one filtered layer.
        {
          id: 'r-path', type: 'line', source: 'pm', 'source-layer': 'roads',
          filter: ['all', LINES(), ['==', ['get', 'kind'], 'path']],
          paint: {
            'line-color': '#b58a5a',
            'line-width': ['interpolate', ['linear'], ['zoom'], 11, 0.5, 14, 1.6],
            'line-dasharray': [2, 2],
          },
        },
        {
          id: 'r-minor', type: 'line', source: 'pm', 'source-layer': 'roads',
          filter: ['all', LINES(), ['match', ['get', 'kind'], ['minor_road', 'other', 'service'], true, false]],
          paint: { 'line-color': '#c8bfb0', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.4, 14, 2] },
        },
        {
          id: 'r-medium', type: 'line', source: 'pm', 'source-layer': 'roads',
          filter: ['all', LINES(), ['==', ['get', 'kind'], 'medium_road']],
          paint: { 'line-color': '#efe4cf', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.6, 14, 3] },
        },
        {
          id: 'r-major', type: 'line', source: 'pm', 'source-layer': 'roads',
          filter: ['all', LINES(), ['match', ['get', 'kind'], ['major_road', 'highway'], true, false]],
          paint: {
            'line-color': ['match', ['get', 'kind'], 'highway', '#f2a95f', '#f4c983'],
            'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.8, 14, 4],
          },
        },
        {
          id: 'boundaries', type: 'line', source: 'pm', 'source-layer': 'boundaries',
          filter: LINES(),
          paint: { 'line-color': '#9b86a8', 'line-width': 0.8, 'line-dasharray': [3, 2], 'line-opacity': 0.6 },
        },
        // Where the downloaded data stops. Without these, the edge of a region is
        // indistinguishable from "the map is broken".
        {
          id: 'region-mask', type: 'fill', source: 'mask',
          paint: { 'fill-color': '#10151a', 'fill-opacity': 0.32 },
        },
        {
          id: 'region-bounds', type: 'line', source: 'bounds',
          paint: { 'line-color': '#2f7fd6', 'line-width': 2, 'line-dasharray': [4, 3], 'line-opacity': 0.8 },
        },
        {
          id: 'me-acc-fill', type: 'fill', source: 'meacc',
          paint: { 'fill-color': '#4a90d9', 'fill-opacity': 0.14 },
        },
        {
          id: 'me-acc-line', type: 'line', source: 'meacc',
          paint: { 'line-color': '#4a90d9', 'line-width': 1, 'line-opacity': 0.5 },
        },
        {
          id: 'me-dot', type: 'circle', source: 'me',
          paint: { 'circle-radius': 7, 'circle-color': '#2f7fd6', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2.5 },
        },
      ],
    };
  }

  // Register a PMTiles archive under `key` and return that key. Cached regions
  // read from a local blob (FileSource keys off the File's name); an
  // un-downloaded one is read over HTTP range requests, so you can look at a
  // region before committing 30 MB to it.
  async function registerSource(region) {
    if (!protocol) {
      protocol = new pmtiles.Protocol();
      maplibregl.addProtocol('pmtiles', protocol.tile);
    }
    const blob = await cachedBlob(region);
    if (blob) {
      const file = new File([blob], region.id); // FileSource.getKey() === file.name
      protocol.add(new pmtiles.PMTiles(new pmtiles.FileSource(file)));
      activeSource = 'offline';
      return region.id;
    }
    if (!navigator.onLine) return null;
    const url = new URL(region.url, location.href).href;
    protocol.add(new pmtiles.PMTiles(url));
    activeSource = 'stream';
    return url;
  }

  async function showRegion(region) {
    const key = await registerSource(region);
    if (!key) return false;
    const style = styleFor(key, region);
    const bounds = [[region.bbox[0], region.bbox[1]], [region.bbox[2], region.bbox[3]]];
    const switching = !activeRegion || activeRegion.id !== region.id;

    if (map) {
      map.setStyle(style);
      map.once('styledata', drawFix);
    } else {
      map = new maplibregl.Map({
        container: 'map',
        style,
        center: region.center,
        zoom: 8,
        maxZoom: 17, // overzoom past the archive's z14 rather than go blank
        attributionControl: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
      map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-left');
      // A drag is the user taking the wheel — stop yanking the camera back.
      map.on('dragstart', (e) => {
        if (e.originalEvent) setFollowing(false);
      });
      // A fix that arrived before the map existed has to be replayed, or the
      // dot never appears for someone standing still.
      map.once('load', drawFix);
      window.fieldMap = map; // `window.map` resolves to the #map div, not this
    }
    if (switching) map.fitBounds(bounds, { padding: 20, duration: 0 });
    activeRegion = region;
    renderRegions();
    renderChips();
    renderStatus();
    return true;
  }

  // --- GPS ------------------------------------------------------------------

  // A geodesic circle, in real metres. MapLibre's circle-radius is in *pixels*,
  // so a fixed radius would lie about accuracy at every zoom but one.
  function circlePolygon(lon, lat, meters, steps = 64) {
    const R = 6378137;
    const d = meters / R;
    const lat1 = (lat * Math.PI) / 180;
    const lon1 = (lon * Math.PI) / 180;
    const ring = [];
    for (let i = 0; i <= steps; i++) {
      const brng = (2 * Math.PI * i) / steps;
      const lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(brng));
      const lon2 = lon1 + Math.atan2(Math.sin(brng) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
      ring.push([(lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI]);
    }
    return { type: 'Polygon', coordinates: [ring] };
  }

  function drawFix() {
    if (!map || !fix) return;
    const { longitude: lon, latitude: lat, accuracy } = fix.coords;
    const me = map.getSource('me');
    const acc = map.getSource('meacc');
    if (!me || !acc) return;
    me.setData({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: {} });
    acc.setData({
      type: 'Feature',
      geometry: circlePolygon(lon, lat, Math.max(accuracy || 0, 5)),
      properties: {},
    });
  }

  function setFollowing(on) {
    following = on && !!fix;
    $('follow').setAttribute('aria-pressed', String(following));
    if (following) recenter();
    renderChips();
  }

  function recenter() {
    if (!map || !fix) return;
    map.easeTo({ center: [fix.coords.longitude, fix.coords.latitude], zoom: Math.max(map.getZoom(), 13) });
  }

  function showActiveRegion() {
    if (!map || !activeRegion) return;
    setFollowing(false);
    const [w, s, e, n] = activeRegion.bbox;
    map.fitBounds([[w, s], [e, n]], { padding: 40 }); // room to see the boundary
  }

  function startGPS() {
    if (!navigator.geolocation) return;
    navigator.geolocation.watchPosition(
      (pos) => {
        const isFirst = !fix;
        fix = pos;
        drawFix(); // no-ops until the map exists; map.once('load') replays it
        if (following) recenter();
        // Don't fly to a first fix: it's usually your driveway, hundreds of km
        // from the region you downloaded, and the map there is empty.
        if (isFirst) $('follow').disabled = false;
        renderChips();
        renderDiagnostics();
      },
      () => renderDiagnostics(),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 },
    );
  }

  // --- download / delete ----------------------------------------------------

  async function download(region, btn, bar) {
    if (!navigator.onLine) return setStatus('bad', 'No connection — reconnect to download.');
    btn.disabled = true;
    bar.parentElement.hidden = false;
    try {
      // Ask before we fill the box: persisted storage is the strongest signal
      // iOS gives us that a 30 MB archive won't be evicted under pressure.
      if (navigator.storage?.persist) {
        try { await navigator.storage.persist(); } catch { /* best effort */ }
      }
      const res = await fetch(region.url, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const total = +res.headers.get('content-length') || region.size_bytes || 0;
      const reader = res.body.getReader();
      const chunks = [];
      let got = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        got += value.length;
        if (total) bar.style.width = Math.min(100, (got / total) * 100) + '%';
      }
      const blob = new Blob(chunks, { type: 'application/octet-stream' });
      const cache = await caches.open(DATA_CACHE);
      await cache.put(
        region.url,
        new Response(blob, { headers: { 'content-type': 'application/octet-stream', 'content-length': String(blob.size) } }),
      );
      // The receipt goes in the same cache as the bytes, so the two can't be
      // evicted apart and leave us disowning an archive we still have.
      await cache.put(
        metaUrl(region.id),
        new Response(JSON.stringify({ ...region, ts: Date.now() }), {
          headers: { 'content-type': 'application/json' },
        }),
      );
      await showRegion(region);
    } catch (e) {
      setStatus('bad', 'Download failed: ' + e.message);
    } finally {
      btn.disabled = false;
      bar.parentElement.hidden = true;
      bar.style.width = '0';
      refresh();
    }
  }

  async function remove(region) {
    if (!confirm(`Delete the offline map for ${region.name}? You'll need a connection to get it back.`)) return;
    const cache = await caches.open(DATA_CACHE);
    await cache.delete(region.url);
    await cache.delete(metaUrl(region.id));
    if (activeRegion?.id === region.id) activeRegion = null;
    refresh();
  }

  // --- rendering ------------------------------------------------------------

  function setStatus(cls, txt) {
    const s = $('status');
    s.className = cls;
    s.textContent = txt;
  }

  let regions = []; // catalogue: what the server offers ∪ what we already hold
  let cachedList = [];
  let cachedIds = new Set();

  function renderRegions() {
    const host = $('regions');
    host.innerHTML = '';
    $('regions-empty').hidden = regions.length > 0;

    for (const r of regions) {
      const cached = cachedIds.has(r.id);
      const el = document.createElement('div');
      el.className = 'region' + (activeRegion?.id === r.id ? ' active' : '');

      const info = document.createElement('div');
      info.className = 'meta';
      const sub = cached
        ? `<span class="cached">✓ offline</span><span class="dot">·</span>${fmtBytes(r.size_bytes)}<span class="dot">·</span>${ago(r.ts)}`
        : `${fmtBytes(r.size_bytes)}<span class="dot">·</span>z${r.min_zoom}–${r.max_zoom}`;
      info.innerHTML = `<div class="name">${r.name}</div><div class="sub">${sub}</div>`;

      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.hidden = true;
      bar.innerHTML = '<i></i>';
      info.appendChild(bar);

      const btn = document.createElement('button');
      if (cached) {
        btn.textContent = activeRegion?.id === r.id ? 'Showing' : 'Show';
        btn.disabled = activeRegion?.id === r.id;
        btn.onclick = () => showRegion(r);
      } else {
        btn.textContent = 'Download';
        btn.disabled = !navigator.onLine;
        btn.onclick = () => download(r, btn, bar.firstElementChild);
      }

      el.append(info, btn);
      if (cached) {
        const del = document.createElement('button');
        del.className = 'sec';
        del.textContent = '🗑';
        del.title = `Delete ${r.name}`;
        del.onclick = () => remove(r);
        el.appendChild(del);
      }
      host.appendChild(el);
    }
  }

  async function renderDiagnostics() {
    if ($('diag').hidden) return;
    const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
    let persisted = false;
    let est = {};
    try { persisted = navigator.storage?.persisted ? await navigator.storage.persisted() : false; } catch { /* unsupported */ }
    try { est = navigator.storage?.estimate ? await navigator.storage.estimate() : {}; } catch { /* unsupported */ }
    const totalCached = cachedList.reduce((n, r) => n + r.size_bytes, 0);

    const rows = {
      'Regions offline': [cachedIds.size ? `${cachedIds.size} (${fmtBytes(totalCached)})` : 'none', cachedIds.size ? 'g' : 'r'],
      'Installed to Home Screen': standalone ? ['yes', 'g'] : ['no — install for durable storage', 'w'],
      'Storage persisted()': persisted ? ['granted', 'g'] : ['not granted', 'w'],
      'Storage used / quota': [`${fmtBytes(est.usage)} / ${fmtBytes(est.quota)}`, ''],
      'Service worker': navigator.serviceWorker?.controller ? ['active', 'g'] : ['not controlling', 'w'],
      Network: [navigator.onLine ? 'online' : 'OFFLINE', navigator.onLine ? '' : 'g'],
      'Last GPS fix': [
        fix ? `${fix.coords.latitude.toFixed(5)}, ${fix.coords.longitude.toFixed(5)} ±${Math.round(fix.coords.accuracy)}m` : '—',
        '',
      ],
    };
    $('diag').innerHTML = Object.entries(rows)
      .map(([k, [v, c]]) => `<div class="row"><span class="k">${k}</span><span class="v ${c}">${v}</span></div>`)
      .join('');
  }

  // The two things you can't tell by looking at a map: whether these tiles came
  // off the device or the network, and whether you're standing inside the part
  // of the world that was downloaded.
  function renderChips() {
    const host = $('chips');
    host.innerHTML = '';
    if (!activeRegion) return;

    const src = document.createElement('div');
    const offline = activeSource === 'offline';
    src.className = 'chip' + (offline ? ' ok' : '');
    src.textContent = offline ? '📴 Offline copy' : '📡 Streaming — not downloaded';
    host.appendChild(src);

    if (fix && !fixIsInsideRegion()) {
      const warn = document.createElement('div');
      warn.className = 'chip warn';
      warn.innerHTML = "<span>You're outside this map</span>";
      const back = document.createElement('button');
      back.textContent = 'Show map';
      back.onclick = showActiveRegion;
      warn.appendChild(back);
      host.appendChild(warn);
    }
  }

  function renderStatus() {
    const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
    $('offline-badge').hidden = navigator.onLine;
    if (!('caches' in window)) return setStatus('bad', '⛔ Offline storage unavailable — this page must be served over HTTPS.');
    if (!cachedIds.size) {
      return activeRegion
        ? setStatus('warn', "📡 Streaming this map over the network. Tap Download and it'll work with no signal.")
        : setStatus('bad', '⛔ No offline map yet — connect to signal and download one below.');
    }
    // Only iOS punishes an un-installed PWA's storage; on a Mac this is noise.
    if (IS_IOS && !standalone) {
      return setStatus('warn', '⚠️ Map downloaded, but not installed. Share → Add to Home Screen, then re-download — iOS gives an installed app its own, more durable storage.');
    }
    setStatus('ok', navigator.onLine ? '✅ Offline map ready — this map works with no signal.' : '✅ Offline — running from cache.');
  }

  // --- boot -----------------------------------------------------------------

  // What we hold comes first — an offline start must still list (and show) it.
  // The server's catalogue then adds regions we could download, and upgrades
  // names/sizes for the ones we have.
  let serverRegions = [];

  function mergeRegions() {
    const byId = new Map(cachedList.map((r) => [r.id, r]));
    for (const s of serverRegions) {
      const held = byId.get(s.id);
      byId.set(s.id, held ? { ...s, size_bytes: held.size_bytes, ts: held.ts } : s);
    }
    regions = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  async function refresh() {
    cachedList = await cachedRegions();
    cachedIds = new Set(cachedList.map((r) => r.id));
    mergeRegions();
    renderRegions();
    renderChips();
    renderStatus();
    renderDiagnostics();
  }

  async function boot() {
    if (!('caches' in window)) {
      renderStatus();
      return;
    }
    // Offline-first: draw what's on the device before touching the network.
    await refresh();
    if (cachedList.length) await showRegion(cachedList[0]);
    renderRegions();

    try {
      const res = await fetch('/field/regions', { cache: 'no-store' });
      if (res.ok) serverRegions = (await res.json()).regions || [];
    } catch { /* nothing reachable — what we hold is the whole catalogue */ }
    await refresh();
    // Nothing downloaded but the server offered a region: show it over the wire
    // so you can look before committing 30 MB.
    if (!map && serverRegions.length && navigator.onLine) await showRegion(serverRegions[0]);
  }

  $('toggle').onclick = () => {
    const sheet = $('sheet');
    const min = sheet.classList.toggle('min');
    $('toggle').textContent = min ? 'show' : 'hide';
    $('toggle').setAttribute('aria-expanded', String(!min));
  };
  $('diag-toggle').onclick = () => {
    const d = $('diag');
    d.hidden = !d.hidden;
    $('diag-toggle').textContent = (d.hidden ? '▸' : '▾') + ' Diagnostics';
    $('diag-toggle').setAttribute('aria-expanded', String(!d.hidden));
    renderDiagnostics();
  };
  $('follow').onclick = () => setFollowing(!following);
  window.addEventListener('online', refresh);
  window.addEventListener('offline', refresh);

  if ('serviceWorker' in navigator) {
    // The page you're looking at was served by the OLD worker, so a deploy is
    // invisible until the next load — "I updated and nothing changed." When the
    // new worker takes over, reload once so you're actually running it.
    const hadController = !!navigator.serviceWorker.controller;
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadController || reloading) return; // first install: nothing to replace
      reloading = true;
      location.reload();
    });
    navigator.serviceWorker.register('./sw.js', { scope: './' });
  }
  startGPS();
  boot();
})();
