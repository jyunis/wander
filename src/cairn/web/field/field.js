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
  const outlineUrl = (id) => `/field/regions/${id}/outline.geojson`;
  const pinsUrl = (id) => `/field/regions/${id}/pins.geojson`;
  const roadsUrl = (id) => `/field/regions/${id}/roads.geojson`;
  const EMPTY_FC = () => ({ type: 'FeatureCollection', features: [] });

  let map = null;
  let activeRegion = null;
  let outlineData = null; // the active region's real boundary, if we have one
  let pinsData = null; // the active region's saved sites/campgrounds, if any
  let roadsData = null; // the active region's Wander roads (OSM ∪ MVUM), if any
  let selectedPinId = null; // the pin whose card is open, for the selection ring
  let selectedRoadId = null; // the road whose card is open, for its highlight
  let downloading = false;
  let upgradeDeferred = false; // a new build landed while we were downloading
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

  // The whole world with the downloaded rectangle punched out of it.
  //
  // This mask is OPAQUE on purpose. `pmtiles extract` keeps whole tiles, so an
  // archive still contains the low-zoom tiles that cover the bbox — and those
  // tiles span half a continent. Zoomed out you saw the entire US; zooming in,
  // each tile that no longer intersects the bbox drops out, so the country
  // disintegrated chunk by chunk. Every one of those pixels is outside what you
  // downloaded. Painting them out is the difference between a map with a defined
  // edge and a map that appears to be falling apart as you use it.
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
  const POINTS = () => ['==', ['geometry-type'], 'Point']; // also matches MultiPoint

  // A saved place's dot, colored like the online marker. Scored sites ride the
  // same absolute red→amber→green ramp the Leaflet app uses (scoreColor():
  // hsl(120·x,68%,47%) over totals 0.4–0.8); campgrounds, dispersed refs and
  // dropped pins have no score, so they get a fixed hue by kind.
  const PIN_COLOR = [
    'case',
    ['has', 'score'],
    ['interpolate', ['linear'], ['get', 'score'],
      0.4, '#c92727', 0.6, '#c9c927', 0.8, '#27c927'],
    ['==', ['get', 'kind'], 'campground'], '#3f7a8c',
    ['==', ['get', 'kind'], 'dispersed'], '#c97b4a',
    '#8a8f98', // manually dropped pin / unscored site
  ];

  // Big enough to see against the topo and to hit with a fingertip. The real
  // touch target is the separate invisible `pins-hit` layer (far fatter), so
  // the visible dot can stay tasteful without being a pain to tap.
  const PIN_RADIUS = ['interpolate', ['linear'], ['zoom'], 8, 5, 11, 7, 14, 9];
  // Matches only the pin whose card is open (or nothing, when id is null).
  const selectedFilter = (id) =>
    ['all', POINTS(), ['==', ['get', 'id'], id == null ? -1 : id]];

  // Wander's own roads, over the basemap's. The basemap draws OSM roads as bare
  // lines; these add the forest roads OSM is missing (MVUM, MN DNR) and are
  // tappable for number/class/season/surface/mud. `style` is decided server-side
  // (cairn.field.road_style): private > MVUM special > forest road > other.
  // Colors follow the online overlay, darkened a step for a light topo.
  const ROAD_COLOR = ['match', ['get', 'style'],
    'private', '#c0392b', 'special', '#8e44ad', 'forest', '#e8710a', '#7d6b55'];
  const ROAD_WIDTH = ['interpolate', ['linear'], ['zoom'],
    10, ['match', ['get', 'style'], 'forest', 1.4, 1],
    14, ['match', ['get', 'style'], 'forest', 3.5, 2.5],
    17, ['match', ['get', 'style'], 'forest', 7, 5.5]];
  const ROADS_MIN_ZOOM = 10; // below this they're a hairball over the basemap
  // Mutually exclusive, so each road is drawn by exactly one of the three
  // (line-dasharray can't be data-driven — see the GOTCHA in styleFor).
  const ROAD_PRIVATE = () => ['all', LINES(), ['has', 'private']];
  const ROAD_ROUGH = () => ['all', LINES(), ['!', ['has', 'private']], ['has', 'rough']];
  const ROAD_PLAIN = () => ['all', LINES(), ['!', ['has', 'private']], ['!', ['has', 'rough']]];
  const selectedRoadFilter = (id) =>
    ['all', LINES(), ['==', ['get', 'osm_id'], id == null ? -1 : id]];

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
        outline: { type: 'geojson', data: outlineData || { type: 'FeatureCollection', features: [] } },
        pins: { type: 'geojson', data: pinsData || EMPTY_FC() },
        wroads: { type: 'geojson', data: roadsData || EMPTY_FC() },
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
        // Wander roads: above the basemap, below the mask (so nothing draws
        // outside the download) and below the pins. The highlight sits under
        // the tapped road so the road itself stays readable.
        {
          id: 'wr-selected', type: 'line', source: 'wroads', minzoom: ROADS_MIN_ZOOM,
          filter: selectedRoadFilter(selectedRoadId),
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#2f7fd6', 'line-opacity': 0.45,
            'line-width': ['interpolate', ['linear'], ['zoom'], 10, 7, 14, 11, 17, 16] },
        },
        {
          id: 'wr-plain', type: 'line', source: 'wroads', minzoom: ROADS_MIN_ZOOM,
          filter: ROAD_PLAIN(),
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': ROAD_COLOR, 'line-width': ROAD_WIDTH, 'line-opacity': 0.9 },
        },
        {
          id: 'wr-rough', type: 'line', source: 'wroads', minzoom: ROADS_MIN_ZOOM,
          filter: ROAD_ROUGH(),
          paint: { 'line-color': ROAD_COLOR, 'line-width': ROAD_WIDTH, 'line-opacity': 0.9,
            'line-dasharray': [2.5, 1.5] },
        },
        {
          id: 'wr-private', type: 'line', source: 'wroads', minzoom: ROADS_MIN_ZOOM,
          filter: ROAD_PRIVATE(),
          layout: { 'line-cap': 'round' },
          paint: { 'line-color': ROAD_COLOR, 'line-width': ROAD_WIDTH, 'line-opacity': 0.9,
            'line-dasharray': [0.3, 2] },
        },
        // The tap target, like pins-hit: a thin road is a hard thing to hit.
        {
          id: 'wr-hit', type: 'line', source: 'wroads', minzoom: ROADS_MIN_ZOOM,
          filter: LINES(),
          paint: { 'line-color': '#000', 'line-opacity': 0, 'line-width': 22 },
        },
        // Everything you did not download, painted out. See maskPolygon().
        {
          id: 'region-mask', type: 'fill', source: 'mask',
          paint: { 'fill-color': '#131a21', 'fill-opacity': 1 },
        },
        // The forest itself, drawn ABOVE the mask so you can see where it runs on
        // past the edge of your download rather than wondering where it went.
        {
          id: 'forest-outline', type: 'line', source: 'outline',
          paint: { 'line-color': '#5f9e5f', 'line-width': 2, 'line-opacity': 0.95 },
        },
        {
          id: 'region-bounds', type: 'line', source: 'bounds',
          paint: { 'line-color': '#2f7fd6', 'line-width': 1.5, 'line-dasharray': [4, 3], 'line-opacity': 0.7 },
        },
        // Your saved sites/campgrounds on the offline basemap (#45 W1). Drawn
        // above the mask/outline so content inside the download reads clearly,
        // but below the GPS dot so "you" is never hidden by a pin. Point-only:
        // give each layer a geometry filter, like every basemap layer does.
        //
        // A ring under the tapped pin, so it's obvious which one the card is for.
        {
          id: 'pins-selected', type: 'circle', source: 'pins',
          filter: selectedFilter(selectedPinId),
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 10, 14, 15],
            'circle-color': 'rgba(0,0,0,0)',
            'circle-stroke-color': '#2f7fd6',
            'circle-stroke-width': 3,
          },
        },
        {
          id: 'pins', type: 'circle', source: 'pins', filter: POINTS(),
          paint: {
            'circle-radius': PIN_RADIUS,
            'circle-color': PIN_COLOR,
            'circle-stroke-color': '#fff',
            'circle-stroke-width': 2,
            'circle-opacity': 0.96,
          },
        },
        // A fat, invisible tap target: a fingertip near a small dot still lands
        // on it, and a near-miss no longer dismisses the card. This is the
        // topmost pins layer, so click/hover/query hit it first; opacity:0
        // features are still returned by queryRenderedFeatures.
        {
          id: 'pins-hit', type: 'circle', source: 'pins', filter: POINTS(),
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 16, 14, 22],
            'circle-color': '#000',
            'circle-opacity': 0,
          },
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

  // A region's sidecars (outline, pins, roads) are cached beside its archive, so
  // they're still on the map with no signal. Cache first: an offline start must
  // draw from what's on the device without waiting on a network that isn't
  // there. Fetched (and cached) only when missing — `revalidateSidecars` keeps
  // them fresh while there's signal. Absent = that layer draws nothing; never a
  // hard failure.
  async function loadSidecar(url) {
    if (!('caches' in window)) return null;
    const cache = await caches.open(DATA_CACHE);
    let res = await cache.match(url);
    if (!res && navigator.onLine) {
      try {
        const fresh = await fetch(url, { cache: 'no-store' });
        if (fresh.ok) {
          await cache.put(url, fresh.clone());
          res = fresh;
        }
      } catch { /* nothing reachable — draw the map without it */ }
    }
    if (!res) return null;
    try { return await res.json(); } catch { return null; }
  }

  // On signal, re-pull the sidecars that change as you use the main app — pins
  // (saved a site since downloading) and roads (scored somewhere new inside the
  // region) — and swap them in live. A failure keeps the cached copy.
  async function revalidateSidecars(region) {
    if (!navigator.onLine || !('caches' in window)) return;
    const cache = await caches.open(DATA_CACHE);
    for (const [url, source, set] of [
      [pinsUrl(region.id), 'pins', (d) => { pinsData = d; }],
      [roadsUrl(region.id), 'wroads', (d) => { roadsData = d; }],
    ]) {
      try {
        const fresh = await fetch(url, { cache: 'no-store' });
        if (!fresh.ok) continue;
        const data = await fresh.clone().json();
        await cache.put(url, fresh);
        if (activeRegion?.id !== region.id) return; // switched away meanwhile
        set(data);
        map?.getSource(source)?.setData(data);
      } catch { /* keep what we have */ }
    }
    renderChips();
    renderDiagnostics();
  }

  async function showRegion(region) {
    const key = await registerSource(region);
    if (!key) return false;
    [outlineData, pinsData, roadsData] = await Promise.all([
      loadSidecar(outlineUrl(region.id)),
      loadSidecar(pinsUrl(region.id)),
      loadSidecar(roadsUrl(region.id)),
    ]);
    const switching = !activeRegion || activeRegion.id !== region.id;
    // A card/ring from the old region has no meaning on the new one; clear it
    // before styleFor bakes the selection filter into the fresh style.
    if (switching) closeCard();
    const style = styleFor(key, region);
    const bounds = [[region.bbox[0], region.bbox[1]], [region.bbox[2], region.bbox[3]]];

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
      // One tap handler, so precedence is explicit: a saved pin beats the road
      // it sits on (pins are usually AT a road end), a road beats bare map, and
      // bare map dismisses the card. Both cards are built from cached data —
      // zero network. The listener lives on the map, not the style, so it
      // survives setStyle.
      map.on('click', (e) => {
        const hit = (layer) => (map.getLayer(layer)
          ? map.queryRenderedFeatures(e.point, { layers: [layer] })[0] : null);
        const pin = hit('pins-hit');
        if (pin) return openPinCard(pin.properties, pin.geometry && pin.geometry.coordinates);
        const road = hit('wr-hit');
        if (road) return openRoadCard(road.properties);
        closeCard();
      });
      for (const layer of ['pins-hit', 'wr-hit']) {
        map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
      }
      window.fieldMap = map; // `window.map` resolves to the #map div, not this
    }
    if (switching) map.fitBounds(bounds, { padding: 20, duration: 0 });
    activeRegion = region;
    renderRegions();
    renderChips();
    renderStatus();
    revalidateSidecars(region); // background; no-op offline
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
    downloading = true;
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
      downloading = false;
      btn.disabled = false;
      bar.parentElement.hidden = true;
      bar.style.width = '0';
      await refresh();
      // The worker held off upgrading us so the download could finish. It's safe now.
      if (upgradeDeferred) location.reload();
    }
  }

  async function remove(region) {
    if (!confirm(`Delete the offline map for ${region.name}? You'll need a connection to get it back.`)) return;
    const cache = await caches.open(DATA_CACHE);
    await cache.delete(region.url);
    await cache.delete(metaUrl(region.id));
    await cache.delete(outlineUrl(region.id));
    await cache.delete(pinsUrl(region.id));
    await cache.delete(roadsUrl(region.id));
    if (activeRegion?.id === region.id) { activeRegion = null; closeCard(); }
    refresh();
  }

  // --- pin card (M3) --------------------------------------------------------
  //
  // A saved place's card, built entirely from the payload the server inlined
  // into the pin — so it opens with no signal. A scored site's payload is the
  // whole scoring record (total + per-feature contributions); a campground's is
  // its meta + booking url. We only read, never fetch.

  const esc = (s) =>
    String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // Enough of the online FEATURE_LABELS to make a contribution row readable
  // offline; an unmapped key just shows its raw name.
  const FEATURE_LABELS = {
    road_score: 'Road access', dist_water_m: 'Water', dist_forest_m: 'Forest cover',
    dist_solitude_m: 'Solitude', slope_deg: 'Flatness', dist_terminus_m: 'Road-end',
    near_campsite: 'Known campsite',
  };

  const KIND_ICON = { campground: '🏕', dispersed: '🔥', site: '⛺' };

  function pinIcon(props) {
    if (props.kind === 'site') return props.score != null ? '⛺' : '📍';
    return KIND_ICON[props.kind] || '📍';
  }

  // MapLibre hands back feature properties with objects JSON-stringified, so the
  // inlined payload arrives as a string; parse it back. Primitives (score,
  // rating) come through with their types intact.
  function parsePayload(props) {
    const p = props.payload;
    if (p == null) return null;
    if (typeof p === 'object') return p;
    try { return JSON.parse(p); } catch { return null; }
  }

  // Move the selection ring to a pin / the highlight to a road (or clear them)
  // without a full style reload. One card at a time, so selecting one clears
  // the other.
  function setSelected(pinId, roadId) {
    selectedPinId = pinId == null ? null : pinId;
    selectedRoadId = roadId == null ? null : roadId;
    if (map && map.getLayer('pins-selected')) {
      map.setFilter('pins-selected', selectedFilter(selectedPinId));
    }
    if (map && map.getLayer('wr-selected')) {
      map.setFilter('wr-selected', selectedRoadFilter(selectedRoadId));
    }
  }

  function openPinCard(props, coords) {
    const card = $('pin-card');
    if (!card) return;
    setSelected(props.id, null);
    const lon = Array.isArray(coords) ? +coords[0] : null;
    const lat = Array.isArray(coords) ? +coords[1] : null;
    const hasLL = Number.isFinite(lat) && Number.isFinite(lon);
    const payload = parsePayload(props) || {};
    const score = props.score != null ? +props.score : (payload.total != null ? +payload.total : null);
    const rating = props.rating != null ? +props.rating : null;
    const name = props.name || (props.kind === 'campground' ? 'Campground'
      : props.kind === 'dispersed' ? 'Dispersed site'
      : score != null ? 'Scored site' : 'Dropped pin');

    const parts = [];
    parts.push(`<div class="pc-head"><span class="pc-name">${pinIcon(props)} ${esc(name)}</span>` +
      `<button class="pc-close" title="Close">×</button></div>`);

    const meta = [];
    if (score != null) meta.push(`<span class="pc-score">score ${score.toFixed(2)}</span>`);
    if (rating) meta.push(`<span class="pc-stars">${'★'.repeat(Math.round(rating))}</span>`);
    if (meta.length) parts.push(`<div class="pc-meta">${meta.join('<span class="dot">·</span>')}</div>`);

    if (props.note) parts.push(`<div class="pc-note">${esc(props.note)}</div>`);

    // Top score contributions, from the cached payload (no "why this score"
    // fetch). Kept short — the field card is a glance, not the full breakdown.
    const contribs = Array.isArray(payload.contributions) ? payload.contributions : [];
    if (contribs.length) {
      const rows = contribs
        .slice()
        .sort((a, b) => (b.contribution || 0) - (a.contribution || 0))
        .slice(0, 4)
        .map((c) => `<div class="pc-row"><span>${esc(FEATURE_LABELS[c.feature] || c.feature)}</span>` +
          `<b>${(c.contribution || 0).toFixed(2)}</b></div>`)
        .join('');
      parts.push(`<div class="pc-contribs">${rows}</div>`);
    }

    // Getting there is Google Maps' job, not ours (#40: last-mile follow-the-map
    // only, never turn-by-turn). This works with no signal too: the URL opens the
    // Google Maps app, which routes on its own downloaded offline maps. The coords
    // sit alongside and are copyable for everything Maps can't do — keying a
    // handheld GPS, or reading a position over the radio.
    if (hasLL) {
      const ll = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
      const gmaps = `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
      parts.push(
        `<div class="pc-coords"><button type="button" class="pc-ll" title="Copy coordinates">📍 ${esc(ll)}</button></div>` +
        `<a class="pc-link pc-gmaps" href="${gmaps}" target="_blank" rel="noopener">Open in Google Maps ↗</a>`,
      );
    }

    // Campground fallback: link out to book (opens when you're back on signal).
    const url = payload.url;
    if (props.kind === 'campground' && url) {
      parts.push(`<a class="pc-link" href="${esc(url)}" target="_blank" rel="noopener">Book / details ↗</a>`);
    }

    parts.push('<div class="pc-disc">Reference only — not a claim it’s legal to camp here.</div>');

    card.innerHTML = parts.join('');
    card.hidden = false;
    card.querySelector('.pc-close').onclick = closeCard;

    if (hasLL) {
      const ll = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
      const llBtn = card.querySelector('.pc-ll');
      if (llBtn) {
        llBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(ll);
            llBtn.textContent = '✓ copied';
            setTimeout(() => { llBtn.textContent = `📍 ${ll}`; }, 1400);
          } catch {
            // No clipboard permission — select the text so it can be copied by hand.
            const r = document.createRange();
            r.selectNodeContents(llBtn);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
          }
        };
      }
      // NB: never intercept this link when offline. On a phone the maps URL
      // deep-links into the Google Maps APP, which navigates fine on its own
      // downloaded offline maps — exactly the no-signal case this app exists
      // for. An earlier cut "helpfully" preventDefault()ed it offline and broke
      // the one handoff that still worked out there. Let it through, always.
    }
  }

  // A road's card: what the MVUM / OSM say about it, from the cached sidecar.
  // It answers "can I drive this, and will I regret it" — it does not route.
  const MUD_CLASS = { Firm: 'firm', Variable: 'variable', Soft: 'soft' };

  function openRoadCard(props) {
    const card = $('pin-card');
    if (!card) return;
    setSelected(null, props.osm_id);
    const icon = props.style === 'forest' || props.style === 'special' ? '🌲' : '🛣';
    const parts = [];
    parts.push(`<div class="pc-head"><span class="pc-name">${icon} ${esc(props.label || 'Road')}</span>` +
      `<button class="pc-close" title="Close">×</button></div>`);
    const sub = [props.name, props.highway].filter(Boolean).map(esc);
    if (sub.length) parts.push(`<div class="pc-meta">${sub.join('<span class="dot">·</span>')}</div>`);

    const rows = [];
    if (props.private) {
      const raw = props.access ? ` <span class="pc-sub">(${esc(props.access)})</span>` : '';
      rows.push(`<div class="pc-warn">🔒 Private — no public access${raw}</div>`);
    }
    if (props.mvum_class) {
      rows.push(`<div class="pc-row"><span>MVUM</span><b>${esc(props.mvum_class)}` +
        `${props.season ? ' · ' + esc(props.season) : ''}</b></div>`);
    }
    if (props.surface) rows.push(`<div class="pc-row"><span>Surface</span><b>${esc(props.surface)}</b></div>`);
    if (props.mud_tier) {
      rows.push(`<div class="pc-row"><span>Mud when wet</span>` +
        `<b class="mud-${MUD_CLASS[props.mud_tier] || ''}">${esc(props.mud_tier)}</b></div>`);
    }
    if (props.rough) rows.push('<div class="pc-warn">⚠ Rough — high clearance</div>');
    if (rows.length) parts.push(`<div class="pc-contribs">${rows.join('')}</div>`);

    // The MVUM says a road is open to DRIVING; dispersed-camping corridors and
    // dates live only on the MVUM itself.
    parts.push(props.mvum_class
      ? '<div class="pc-disc">Open to driving as shown on the MVUM — check the MVUM for camping corridors and dates.</div>'
      : '<div class="pc-disc">Reference only — not a claim it’s open or legal to camp.</div>');

    card.innerHTML = parts.join('');
    card.hidden = false;
    card.querySelector('.pc-close').onclick = closeCard;
  }

  function closeCard() {
    const card = $('pin-card');
    if (card) card.hidden = true;
    setSelected(null, null);
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
  let lastRegionsSig = null; // last-rendered list state, to skip no-op rebuilds

  function renderRegions() {
    // The foreground refresh (visibilitychange → syncServer → refresh) re-runs
    // this on EVERY return to the app. Rebuilding replaces every button node —
    // and a tap that lands in that same moment hits a node being swapped out, so
    // it "does nothing". Almost every foreground finds the list unchanged, so
    // fingerprint it and bail when nothing moved: returning to the app then never
    // eats the tap you make as you arrive.
    const sig = JSON.stringify([
      navigator.onLine,
      activeRegion?.id || null,
      regions.map((r) => [r.id, cachedIds.has(r.id), r.name, r.size_bytes, r.ts, r.min_zoom, r.max_zoom]),
    ]);
    if (sig === lastRegionsSig) return;
    lastRegionsSig = sig;

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
        // Never a disabled dead-end: the region you're already showing gets a
        // "Recenter" that re-frames the map, so tapping it always does something
        // (a disabled "Showing" button reads as "the app is broken").
        const isActive = activeRegion?.id === r.id;
        btn.textContent = isActive ? 'Recenter' : 'Show';
        btn.onclick = isActive ? showActiveRegion : () => showRegion(r);
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
      'Wander roads (active)': [
        roadsData ? `${roadsData.features.length}${roadsData.truncated ? ' — truncated' : ''}` : 'none cached',
        roadsData?.features?.length ? 'g' : 'w',
      ],
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
  let lastChipsSig = null;

  function renderChips() {
    // Same reason as renderRegions: this runs on every GPS tick and every
    // foreground, and its "Show map" button shouldn't be swapped out mid-tap.
    const nRoads = roadsData?.features?.length || 0;
    const sig = JSON.stringify([
      activeRegion?.id || null, activeSource, !!fix, !!fix && fixIsInsideRegion(), nRoads,
    ]);
    if (sig === lastChipsSig) return;
    lastChipsSig = sig;

    const host = $('chips');
    host.innerHTML = '';
    if (!activeRegion) return;

    const src = document.createElement('div');
    const offline = activeSource === 'offline';
    src.className = 'chip' + (offline ? ' ok' : '');
    src.textContent = offline ? '📴 Offline copy' : '📡 Streaming — not downloaded';
    host.appendChild(src);

    // Say when there are no tappable roads, so a bare basemap doesn't pass for
    // "Wander checked and there's nothing here" — it means nothing was ingested.
    const roads = document.createElement('div');
    roads.className = 'chip' + (nRoads ? '' : ' warn');
    roads.textContent = nRoads
      ? `🛣 ${nRoads.toLocaleString()} roads${roadsData.truncated ? ' (partial)' : ''}`
      : '🛣 No Wander roads here — basemap only';
    host.appendChild(roads);

    if (fix && !fixIsInsideRegion()) {
      const warn = document.createElement('div');
      warn.className = 'chip warn';
      warn.innerHTML = "<span>Outside the downloaded area</span>";
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

  // Re-pull the server catalogue (regions available to download) + re-render.
  // Called on boot, on reconnect, and when the PWA returns to the foreground, so
  // a region you extract in the main app shows up here without a manual reload —
  // iOS resumes an installed PWA from memory, so boot() alone would never re-run.
  let syncing = false;
  async function syncServer() {
    // Rapid focus flips (or online + visibility firing together) can stack
    // several of these; one at a time is enough and keeps refresh from thrashing.
    if (syncing) return;
    syncing = true;
    try {
      if (navigator.onLine) {
        try {
          const res = await fetch('/field/regions', { cache: 'no-store' });
          if (res.ok) serverRegions = (await res.json()).regions || [];
        } catch { /* unreachable — keep the catalogue we already hold */ }
      }
      await refresh();
    } finally {
      syncing = false;
    }
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

    await syncServer();
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
  $('follow').onclick = () => {
    if (fix) return setFollowing(!following);
    // No position yet — common on a desktop shortcut. Don't sit there dead:
    // explain, and actively ask for one so a granted permission starts the dot.
    if (!navigator.geolocation) return setStatus('bad', '📍 This device has no location.');
    setStatus('warn', '📍 Locating… allow location access to follow your position.');
    navigator.geolocation.getCurrentPosition(
      () => {},  // startGPS's watcher stores the fix and enables following
      (e) => setStatus('bad', '📍 Location unavailable — ' + (e.message || 'permission denied')),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 },
    );
  };
  window.addEventListener('online', syncServer);  // reconnected — re-pull the catalogue
  window.addEventListener('offline', refresh);
  // iOS resumes an installed PWA without reloading it, so re-pull the catalogue
  // whenever it returns to the foreground: a region extracted in the main app
  // then appears here without a force-quit (no-op offline; falls back to cache).
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') syncServer();
  });

  // Updates are handled entirely by the worker (see sw.js `activate`): a stale
  // page can't be trusted to upgrade itself, because the code that would do it
  // is exactly the code it isn't running yet. All we do is tell it when a reload
  // would cost us a half-finished 31 MB download.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data?.type !== 'wf-upgrade-check') return;
      if (downloading) upgradeDeferred = true;
      event.ports?.[0]?.postMessage({ busy: downloading });
    });
    navigator.serviceWorker.register('./sw.js', { scope: './' });
  }
  startGPS();
  boot();
})();
