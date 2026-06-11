"use strict";

// --- state -----------------------------------------------------------------
let CONFIG = null;
let map, drawnItems, heatLayer, pinLayer, candidatesLayer, roadsLayer, canvasRenderer, gotoMarker;
let nearbyCgLayer; // ephemeral markers: the campgrounds near the open scored site
let tripLayer; // markers for the currently plotted trip's members
let tripPlotMode = false; // "Plot on map" is showing ONLY trip stops (results/overlays hidden)
let selectedMarker = null; // marker for the currently-selected (saved) place
let baseLayers = []; // current basemap tile layer(s): imagery + optional labels
let bbox = null; // {min_lon,min_lat,max_lon,max_lat}
let activeRunId = null;
let lastGj = null; // last run's geojson, kept for re-filtering on min-score change
let minScore = 0.45; // hide candidates/sites below this absolute total (noise floor)

// Keyless basemap sources. Add more here — the selector is data-driven.
// maxNativeZoom = the source's real max; maxZoom lets Leaflet over-zoom
// (blurry but navigable) so switching sources never loses the current zoom.
const BASEMAPS = [
  {
    id: "topo",
    name: "Topographic",
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    options: {
      maxNativeZoom: 17,
      maxZoom: 19,
      attribution:
        'Map © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA), ' +
        "data © OpenStreetMap contributors",
    },
  },
  {
    id: "natgeo",
    name: "Atlas (Nat Geo)",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
    options: {
      maxNativeZoom: 16,
      maxZoom: 19,
      attribution: "© National Geographic, Esri, and contributors",
    },
  },
  {
    id: "usgs",
    name: "USGS Topo (printed)",
    url: "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
    options: {
      maxNativeZoom: 16,
      maxZoom: 19,
      attribution: "USGS — The National Map",
    },
  },
  {
    id: "satellite",
    name: "Satellite (labeled)",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    options: {
      maxNativeZoom: 19,
      maxZoom: 19,
      attribution: "Imagery © Esri — Esri, Maxar, Earthstar Geographics, and the GIS community",
    },
    // Transparent reference overlays stacked on the imagery: roads +
    // place/water labels (the standard Esri imagery hybrid).
    labels: [
      "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
      "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    ],
  },
  {
    id: "streets",
    name: "Streets",
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: { maxNativeZoom: 19, maxZoom: 19, attribution: "© OpenStreetMap contributors" },
  },
];

// --- land overlays (display-only reference layers; TLC-3) ------------------
// Ownership categories color-coded; Tax Forfeit is the highlight (often
// open to dispersed use, the layer onX charges for). Boundaries draw as
// outlines. All off by default, fetched per current map view.
const OWNERSHIP_COLORS = {
  "Tax Forfeit": "#ff7a00",
  State: "#2e8b57",
  Federal: "#3a6ea5",
  "County Fee": "#8a5fb0",
  Tribal: "#0f9b8e",
};
const OWNERSHIP_FALLBACK = "#8a8a8a";
const BOUNDARY_COLORS = {
  national_forest: "#1f7a1f",
  state_forest: "#6b8e23",
  state_park: "#b5651d",
};
// Campground marker tint by managing tier.
const CAMPGROUND_COLORS = {
  "National / Federal": "#2e7d32",
  "State Park": "#b5651d",
  "State Forest": "#6b8e23",
};
// Below this zoom, ownership parcels are too numerous/tiny to fetch usefully.
const OWNERSHIP_MIN_ZOOM = 11;

let overlayDefs = []; // from GET /overlays
const overlayLayers = {}; // id -> L.GeoJSON
const overlayActive = new Set();
const overlayTruncated = new Set(); // layers whose last fetch hit the cap

const FEATURE_LABELS = {
  road_end: "Road end",
  road_character: "Forest road",
  remoteness: "Remoteness",
  water_proximity: "Water",
  solitude: "Solitude",
  flatness: "Flatness",
  tree_cover: "Trees",
};

// --- helpers ---------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

function areaKm2(b) {
  const midLat = ((b.min_lat + b.max_lat) / 2) * (Math.PI / 180);
  const kmLat = 111.32, kmLon = 111.32 * Math.cos(midLat);
  return Math.abs((b.max_lon - b.min_lon) * kmLon * (b.max_lat - b.min_lat) * kmLat);
}

function setStatus(msg, kind) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

function readWeights() {
  const w = {};
  CONFIG.feature_names.forEach((f) => {
    w[f] = parseFloat($("w-" + f).value);
  });
  return w;
}

// --- init ------------------------------------------------------------------
async function init() {
  CONFIG = await (await fetch("/config")).json();
  $("disclaimer").textContent = CONFIG.disclaimer;
  buildSliders();
  buildPresets();
  buildBasemaps();
  setupMap();
  await loadRuns();
  await initLandOverlays();

  $("draw-bbox").onclick = startDraw;
  $("use-view").onclick = useCurrentView;
  $("goto-btn").onclick = gotoCoords;
  $("goto-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") gotoCoords();
  });
  $("drop-pin").onclick = toggleDropPin;
  $("score-btn").onclick = runScore;
  $("run-history").onchange = (e) => {
    if (e.target.value) loadRun(parseInt(e.target.value, 10));
  };
  ["layer-pins", "layer-dots", "layer-heat", "layer-roads"].forEach((id) => {
    $(id).onchange = updateLayerVisibility;
  });
  $("min-score").oninput = (e) => {
    minScore = parseFloat(e.target.value);
    $("min-score-val").textContent = minScore.toFixed(2);
    renderCandidates(); // re-filter the existing run, no re-score
  };
  ["f-firstcome", "f-electric", "f-water", "f-pets", "f-ada", "f-rv"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", applyCampgroundFilters);
  });
  $("sheet-close").onclick = closeSite;
  // Sidebar: a left column on desktop (collapsible), an overlay drawer on
  // mobile (closed by default). One state class on #app; CSS interprets it
  // per viewport.
  setNav(!window.matchMedia("(max-width: 640px)").matches);
  $("nav-open").onclick = () => setNav(true);
  $("nav-close").onclick = () => setNav(false);
  $("nav-backdrop").onclick = () => setNav(false);
  // Delegated ★ handler — works for the site sheet and every campground card.
  document.addEventListener("click", (e) => {
    const star = e.target.closest(".star");
    if (!star) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      toggleFav(JSON.parse(star.dataset.fav), star);
    } catch {
      /* ignore malformed */
    }
  });
  // Delegated "＋ trip" handler — same reach as the ★ (sheet + campground cards).
  document.addEventListener("click", (e) => {
    const add = e.target.closest(".trip-add-btn");
    if (!add) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      addToTripFlow(JSON.parse(add.dataset.fav), add);
    } catch {
      /* ignore malformed */
    }
  });
  loadSaved();
  setupTabs();
}

function buildSliders() {
  const host = $("sliders");
  host.innerHTML = "";
  CONFIG.feature_names.forEach((f) => {
    const v = CONFIG.default_weights[f] ?? 0;
    const div = document.createElement("div");
    div.className = "slider";
    div.innerHTML =
      `<label>${FEATURE_LABELS[f] || f}<span class="val" id="v-${f}">${v.toFixed(
        2
      )}</span></label>` +
      `<input type="range" id="w-${f}" min="0" max="1" step="0.05" value="${v}">`;
    host.appendChild(div);
    div.querySelector("input").oninput = (e) => {
      $("v-" + f).textContent = parseFloat(e.target.value).toFixed(2);
      document.querySelectorAll(".preset").forEach((p) => p.classList.remove("active"));
    };
  });
}

function buildPresets() {
  const host = $("presets");
  host.innerHTML = "";
  Object.keys(CONFIG.presets).forEach((name) => {
    const b = document.createElement("button");
    b.className = "preset";
    b.textContent = name.replace(/_/g, " ");
    b.onclick = () => {
      const preset = CONFIG.presets[name];
      CONFIG.feature_names.forEach((f) => {
        const val = preset[f] ?? 0;
        $("w-" + f).value = val;
        $("v-" + f).textContent = val.toFixed(2);
      });
      document.querySelectorAll(".preset").forEach((p) => p.classList.remove("active"));
      b.classList.add("active");
    };
    host.appendChild(b);
  });
}

function setNav(open) {
  $("app").classList.toggle("nav-open", open);
  // The map container resized (desktop column show/hide) — let Leaflet re-lay.
  setTimeout(() => map && map.invalidateSize(), 260);
}

function setupMap() {
  // Zoom on the right so the floating menu button can live top-left.
  map = L.map("map", { zoomControl: false }).setView([47.9, -91.86], 11);
  L.control.zoom({ position: "topright" }).addTo(map);
  setBasemap(BASEMAPS[0].id);
  // Land overlays sit above the basemap but below roads/dots/pins.
  map.createPane("land");
  map.getPane("land").style.zIndex = 250;
  map.on("moveend", refreshActiveOverlays);
  // Keep info windows from opening hidden behind the bottom sheet.
  map.on("popupopen", (e) => {
    _popupOpen = true;
    keepPopupClearOfSheet(e.popup);
  });
  map.on("popupclose", () => {
    _popupOpen = false;
    if (_overlayPending) {
      _overlayPending = false;
      refreshActiveOverlays(); // run the refresh we deferred while it was open
    }
  });

  drawnItems = new L.FeatureGroup().addTo(map);
  // Canvas renderer so thousands of candidate dots stay smooth.
  canvasRenderer = L.canvas({ padding: 0.5 });
  // roadsLayer holds Cairn's own drivable ways (OSM ∪ MVUM) as vector lines —
  // off by default. Added before candidates/pins so it renders beneath them.
  roadsLayer = new L.LayerGroup();
  candidatesLayer = new L.LayerGroup().addTo(map);
  pinLayer = new L.LayerGroup().addTo(map);
  nearbyCgLayer = new L.LayerGroup().addTo(map);
  tripLayer = new L.LayerGroup().addTo(map);

  map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    setBboxFromBounds(e.layer.getBounds());
  });
}

function buildBasemaps() {
  const sel = $("basemap");
  sel.innerHTML = "";
  BASEMAPS.forEach((b) => {
    const opt = document.createElement("option");
    opt.value = b.id;
    opt.textContent = b.name;
    sel.appendChild(opt);
  });
  sel.value = BASEMAPS[0].id;
  sel.onchange = (e) => setBasemap(e.target.value);
}

function setBasemap(id) {
  const def = BASEMAPS.find((b) => b.id === id) || BASEMAPS[0];
  // Build the new layer set: imagery/base first, then any transparent label
  // overlays on top of it. All live in Leaflet's tilePane, which sits below
  // the markers/canvas (dots + pins), so the data stays on top.
  const next = [L.tileLayer(def.url, def.options)];
  (def.labels || []).forEach((u) => {
    next.push(L.tileLayer(u, { maxNativeZoom: 18, maxZoom: 19, attribution: def.options.attribution }));
  });
  next.forEach((l) => l.addTo(map)); // add new (on top of old) ...
  baseLayers.forEach((l) => map.removeLayer(l)); // ... then drop the old set
  baseLayers = next;
}

let drawHandler = null;
function startDraw() {
  if (drawHandler) drawHandler.disable();
  drawHandler = new L.Draw.Rectangle(map, { shapeOptions: { color: "#5b9c5a" } });
  drawHandler.enable();
}

// Parse "lat, lon" forgivingly — grabs the first two numbers (commas, spaces,
// or labels all fine). Returns [lat, lon] or null.
function parseLatLon(s) {
  const nums = (s || "").match(/-?\d+(?:\.\d+)?/g);
  if (!nums || nums.length < 2) return null;
  const lat = parseFloat(nums[0]);
  const lon = parseFloat(nums[1]);
  if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    return null;
  }
  return [lat, lon];
}

async function gotoCoords() {
  const input = $("goto-input");
  const raw = (input.value || "").trim();
  if (!raw) return;
  input.classList.remove("bad");

  // Fast path: a pasted "lat, lon" jumps immediately, no network.
  const ll = parseLatLon(raw);
  if (ll) {
    dropGotoMarker(ll[0], ll[1], `${ll[0].toFixed(6)}, ${ll[1].toFixed(6)}`, 15);
    return;
  }

  // Otherwise treat it as a place name and geocode (Nominatim, via the API).
  // Bias toward the current view so "state park" near you ranks first.
  const b = map.getBounds();
  const viewbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
  input.classList.add("loading");
  setStatus(`Searching “${raw}”…`);
  try {
    const r = await fetch(
      `/geocode?q=${encodeURIComponent(raw)}&viewbox=${encodeURIComponent(viewbox)}`
    );
    const data = await r.json();
    const hit = (data.results || [])[0];
    if (!hit) {
      input.classList.add("bad");
      setStatus(`No place found for “${raw}”.`, "err");
      return;
    }
    if (hit.bbox) {
      // Fit a forest/city extent rather than a fixed zoom.
      map.fitBounds([[hit.bbox[1], hit.bbox[0]], [hit.bbox[3], hit.bbox[2]]], {
        padding: [20, 20],
        maxZoom: 15,
      });
    } else {
      map.setView([hit.lat, hit.lon], 13);
    }
    dropGotoMarker(hit.lat, hit.lon, hit.display_name || raw, null);
    setStatus("");
  } catch {
    input.classList.add("bad");
    setStatus("Geocoding failed — check the connection.", "err");
  } finally {
    input.classList.remove("loading");
  }
}

function dropGotoMarker(lat, lon, label, zoom) {
  if (zoom != null) map.setView([lat, lon], zoom);
  if (gotoMarker) map.removeLayer(gotoMarker);
  gotoMarker = L.circleMarker([lat, lon], {
    radius: 10,
    color: "#22d3ee",
    weight: 3,
    fill: false,
  })
    .addTo(map)
    .bindPopup(label)
    .openPopup();
}

// --- drop a pin (mark + save an arbitrary spot, not just scored sites) ------
// Click "Drop a pin" to enter placement mode, then tap the map. The pin opens
// the same bottom sheet a scored site does — minus the score — so it inherits
// name suggestion, the Google Maps link, nearby campgrounds, and the ★ save.
let pinPlacing = false;
function toggleDropPin() {
  if (pinPlacing) return cancelDropPin();
  pinPlacing = true;
  $("drop-pin").classList.add("active");
  map.getContainer().style.cursor = "crosshair";
  setStatus("Tap the map to drop a pin.");
  if (window.matchMedia("(max-width: 640px)").matches) setNav(false); // reveal the map
  map.on("click", onPinClick);
  document.addEventListener("keydown", onPinKey);
}

function cancelDropPin() {
  pinPlacing = false;
  $("drop-pin").classList.remove("active");
  map.getContainer().style.cursor = "";
  setStatus("");
  map.off("click", onPinClick);
  document.removeEventListener("keydown", onPinKey);
}

function onPinKey(e) {
  if (e.key === "Escape") cancelDropPin();
}

function onPinClick(e) {
  cancelDropPin();
  dropPin(e.latlng.lat, e.latlng.lng);
}

function dropPin(lat, lon) {
  showSite({}, lat, lon); // plain sheet: suggests a name, lists nearby campgrounds, offers ★
  // A draggable marker so the spot can be nudged exactly before saving.
  selectedMarker = L.marker([lat, lon], {
    draggable: true,
    icon: L.divIcon({ className: "", html: `<div class="drop-pin">📍</div>`,
      iconSize: [30, 30], iconAnchor: [15, 28] }),
  }).addTo(map);
  selectedMarker.on("dragend", () => {
    const ll = selectedMarker.getLatLng();
    const keep = selectedMarker;
    selectedMarker = null; // keep the dragged marker through the sheet re-render
    showSite({}, ll.lat, ll.lng);
    selectedMarker = keep;
  });
}

function useCurrentView() {
  drawnItems.clearLayers();
  const b = map.getBounds();
  drawnItems.addLayer(L.rectangle(b, { color: "#5b9c5a", weight: 1, dashArray: "4" }));
  setBboxFromBounds(b);
}

function setBboxFromBounds(b) {
  bbox = {
    min_lon: b.getWest(),
    min_lat: b.getSouth(),
    max_lon: b.getEast(),
    max_lat: b.getNorth(),
  };
  const a = areaKm2(bbox);
  const ok = a <= CONFIG.max_bbox_area_km2;
  $("bbox-readout").innerHTML =
    `Area: <b>${a.toFixed(1)} km²</b> ${
      ok ? "" : `<span style="color:#e8786b">(over ${CONFIG.max_bbox_area_km2} km² cap)</span>`
    }`;
  $("score-btn").disabled = !ok;
}

// --- scoring flow ----------------------------------------------------------
async function runScore() {
  if (!bbox) return;
  if (areaKm2(bbox) > CONFIG.max_bbox_area_km2) {
    // client-side mirror of TLC-4; the API is still the authority.
    setStatus("Area exceeds the cap — draw a smaller box.", "err");
    return;
  }
  $("score-btn").disabled = true;
  setStatus("Scoring…");
  try {
    const resp = await fetch("/score", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        bbox: [bbox.min_lon, bbox.min_lat, bbox.max_lon, bbox.max_lat],
        weights: readWeights(),
      }),
    });
    if (resp.status === 422) {
      const err = await resp.json();
      setStatus(err.detail || "Invalid request", "err");
      $("score-btn").disabled = false;
      return;
    }
    let run = await resp.json();
    while (run.status === "pending" || run.status === "running") {
      await new Promise((r) => setTimeout(r, 1000));
      run = await (await fetch(`/runs/${run.id}`)).json();
    }
    await loadRuns();
    if (run.status === "failed") {
      setStatus("Run failed: " + run.failure_reason, "err");
    } else {
      setStatus(
        `Done — ${run.distinct_count} distinct sites (${run.candidate_count} scored points).`,
        "ok"
      );
      await loadRun(run.id);
    }
  } catch (e) {
    setStatus("Error: " + e.message, "err");
  } finally {
    $("score-btn").disabled = false;
  }
}

async function loadRuns() {
  const data = await (await fetch("/runs?limit=25")).json();
  const sel = $("run-history");
  sel.innerHTML = '<option value="">— recent runs —</option>';
  data.runs.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.id;
    const when = (r.created_at || "").replace("T", " ").slice(0, 16);
    opt.textContent = `#${r.id} · ${r.status} · ${r.candidate_count} · ${when}`;
    if (r.id === activeRunId) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function loadRun(runId) {
  exitTripPlot(); // loading a run leaves the trip-plot view
  activeRunId = runId;
  $("run-history").value = String(runId);
  const gj = await (await fetch(`/runs/${runId}/geojson`)).json();
  renderResults(gj);
  renderMeta(gj.run);
  loadRoads(runId);
}

// Cairn's own scored road network (OSM ∪ MVUM forest spurs) as vector lines.
// Far better than the satellite basemap's transportation overlay, which is
// blank in the forest and drops linework above z18. Best-effort: a roads
// fetch failure just leaves the overlay empty, never blocks results.
async function loadRoads(runId) {
  roadsLayer.clearLayers();
  let gj;
  try {
    gj = await (await fetch(`/runs/${runId}/roads`)).json();
  } catch {
    return;
  }
  (gj.features || []).forEach((f) => {
    const p = f.properties || {};
    const forest = p.forest_road;
    const line = L.geoJSON(f, {
      style: {
        color: forest ? "#ff7a00" : "#ffd23d",
        weight: forest ? 3 : 2,
        opacity: 0.9,
        dashArray: p.rough ? "5 4" : null,
        lineCap: "round",
      },
    });
    if (p.name) line.bindTooltip(String(p.name), { sticky: true });
    roadsLayer.addLayer(line);
  });
  updateLayerVisibility();
}

function renderMeta(run) {
  const prov = run.provenance || {};
  const osm = prov.osm || {}, elev = prov.elevation || {};
  const sites = `<b>${run.distinct_count}</b> distinct sites · ${run.candidate_count} scored points`;
  $("provenance").innerHTML = run.provenance
    ? `${sites}<br>OSM ${osm.from_cache ? "cached" : "fetched"} ${(osm.fetched_at || "").slice(0, 10)} · ` +
      `elevation z${(elev.covering || {}).zoom ?? "?"}${run.capped ? " · capped" : ""}`
    : sites;
  const deg = run.degraded_features || [];
  $("degraded").textContent = deg.length
    ? "⚠ degraded (scored on the rest): " + deg.map((d) => FEATURE_LABELS[d] || d).join(", ")
    : "";
}

// Score -> color on a red->amber->green ramp. ABSOLUTE (a given score always
// maps to the same color, comparable across runs — not per-run normalized), but
// stretched across the meaningful band so real sites show visible spread:
// totals cluster in ~0.4 (poor) .. 0.8 (excellent), and a linear 0-1 ramp would
// paint that whole band green. Below SCORE_LO is red, above SCORE_HI is green.
const SCORE_LO = 0.4;
const SCORE_HI = 0.8;
function scoreColor(t) {
  const x = Math.max(0, Math.min(1, (t - SCORE_LO) / (SCORE_HI - SCORE_LO)));
  return `hsl(${120 * x}, 68%, 47%)`;
}

function renderResults(gj) {
  lastGj = gj;
  renderCandidates();
  const feats = gj.features || [];
  if (feats.length) {
    const b = gj.run.bbox;
    map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [20, 20] });
  }
}

// (Re)draw the result layers from lastGj, hiding anything below the min-score
// bar. Called on a fresh run and whenever the min-score slider moves (no
// re-fetch — the geojson already carries each candidate's absolute total).
function renderCandidates() {
  candidatesLayer.clearLayers();
  pinLayer.clearLayers();
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  const feats = (lastGj && lastGj.features) || [];
  const visible = feats.filter((f) => f.properties.total >= minScore);

  // Every passing candidate as an absolute-score-colored dot.
  visible.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const dot = L.circleMarker([lat, lon], {
      renderer: canvasRenderer,
      radius: 4,
      weight: 0,
      fillColor: scoreColor(p.total),
      fillOpacity: 0.85,
    });
    dot.on("click", () => showSite(p, lat, lon));
    candidatesLayer.addLayer(dot);
  });

  // Numbered pins for the top DISTINCT sites above the bar (spatial NMS, not
  // adjacent road samples). Fall back to plain rank for pre-NMS runs.
  const distinct = visible
    .filter((f) => f.properties.distinct && f.properties.distinct_rank != null)
    .sort((a, b) => a.properties.distinct_rank - b.properties.distinct_rank);
  const MAX_PINS = 60;
  const pinFeats = distinct.length ? distinct.slice(0, MAX_PINS) : visible.slice(0, MAX_PINS);
  pinFeats.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const label = p.distinct_rank != null ? p.distinct_rank : p.rank;
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({
        className: "",
        html: `<div class="rank-pin" style="background:${scoreColor(p.total)}">${label}</div>`,
        iconSize: [22, 22],
      }),
    });
    marker.on("click", () => showSite(p, lat, lon));
    pinLayer.addLayer(marker);
  });

  // Heatmap (only shown when its checkbox is on) — also above the bar.
  const heat = visible.map((f) => [
    f.geometry.coordinates[1],
    f.geometry.coordinates[0],
    Math.max(0.05, f.properties.total),
  ]);
  heatLayer = visible.length ? L.heatLayer(heat, { radius: 22, blur: 16, max: 1.0 }) : null;

  updateResultCount(feats);
  updateLayerVisibility();
}

// "N of M sites ≥ bar" readout, counting DISTINCT sites (the numbered pins).
function updateResultCount(feats) {
  const el = $("result-count");
  if (!el) return;
  if (!feats.length) {
    el.textContent = "";
    return;
  }
  const total = feats.filter((f) => f.properties.distinct).length;
  const shown = feats.filter((f) => f.properties.distinct && f.properties.total >= minScore).length;
  el.textContent = `${shown} of ${total} sites ≥ ${minScore.toFixed(2)}`;
}

function updateLayerVisibility() {
  toggleLayer(pinLayer, $("layer-pins").checked);
  toggleLayer(candidatesLayer, $("layer-dots").checked);
  toggleLayer(heatLayer, $("layer-heat").checked);
  toggleLayer(roadsLayer, $("layer-roads").checked);
}

function toggleLayer(layer, on) {
  if (!layer) return;
  if (on && !map.hasLayer(layer)) map.addLayer(layer);
  else if (!on && map.hasLayer(layer)) map.removeLayer(layer);
}

// --- land overlays ---------------------------------------------------------
async function initLandOverlays() {
  let data;
  try {
    data = await (await fetch("/overlays")).json();
  } catch {
    return; // overlays unavailable — leave the Land section hidden
  }
  overlayDefs = data.layers || [];
  if (!overlayDefs.length) return;
  $("land-disclaimer").textContent = data.disclaimer || "";
  buildLandToggles();
  $("land-section").hidden = false;
}

function buildLandToggles() {
  const host = $("land-layers");
  host.innerHTML = "";
  overlayDefs.forEach((def) => {
    const id = "ov-" + def.id;
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" id="${id}"> ${def.title}`;
    host.appendChild(label);
    label.querySelector("input").onchange = (e) => toggleOverlay(def.id, e.target.checked);
  });
}

function toggleOverlay(layerId, on) {
  if (on) overlayActive.add(layerId);
  else overlayActive.delete(layerId);
  if (!on && overlayLayers[layerId]) {
    map.removeLayer(overlayLayers[layerId]);
    delete overlayLayers[layerId];
  }
  if (layerId === "campground" && $("cg-filters")) $("cg-filters").hidden = !on;
  if (on) fetchOverlay(layerId);
  renderLandLegend();
  updateLandHint();
}

let _overlayTimer = null;
let _popupOpen = false; // a Leaflet popup (campground info window) is open
let _overlayPending = false; // a refresh was deferred while a popup was open
function refreshActiveOverlays() {
  if (tripPlotMode) return; // trip-plot view: only trip stops, no land overlays
  if (!overlayActive.size) return;
  // A moveend refetch rebuilds the overlay layer (removeLayer + re-add), which
  // would close any popup anchored to one of its markers. Defer while a popup
  // is open; the popupclose handler runs the pending refresh.
  if (_popupOpen) {
    _overlayPending = true;
    return;
  }
  clearTimeout(_overlayTimer);
  _overlayTimer = setTimeout(() => {
    overlayActive.forEach((id) => fetchOverlay(id));
    updateLandHint();
  }, 300);
}

async function fetchOverlay(layerId) {
  // Ownership parcels are too dense to fetch when zoomed out.
  if (layerId === "ownership" && map.getZoom() < OWNERSHIP_MIN_ZOOM) {
    if (overlayLayers[layerId]) {
      map.removeLayer(overlayLayers[layerId]);
      delete overlayLayers[layerId];
    }
    return;
  }
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
  let gj;
  try {
    gj = await (await fetch(`/overlays/${layerId}?bbox=${bbox}`)).json();
  } catch {
    return;
  }
  if (!overlayActive.has(layerId)) return; // toggled off mid-fetch
  if (gj.truncated) overlayTruncated.add(layerId);
  else overlayTruncated.delete(layerId);
  renderOverlayLayer(layerId, gj);
}

let campgroundGj = null; // last fetched campground FeatureCollection (for re-filtering)

function renderOverlayLayer(layerId, gj) {
  if (overlayLayers[layerId]) map.removeLayer(overlayLayers[layerId]);
  const isOwn = layerId === "ownership";
  const isPoint =
    (gj.geom_type || overlayDefs.find((d) => d.id === layerId)?.geom_type) === "point";
  const gjLayer = L.geoJSON(gj, {
    pane: "land",
    filter: isPoint ? (feat) => matchesCampground(feat.properties) : undefined,
    style: isPoint ? undefined : (feat) => overlayStyle(layerId, feat),
    pointToLayer: isPoint
      ? (feat, latlng) => {
          const c = CAMPGROUND_COLORS[(feat.properties || {}).category] || "#d96c2c";
          return L.marker(latlng, {
            icon: L.divIcon({
              className: "",
              html: `<div class="cg-pin" style="--cg:${c}">🏕️</div>`,
              iconSize: [20, 20],
              iconAnchor: [10, 10],
            }),
          });
        }
      : undefined,
    onEachFeature: (feat, lyr) => {
      const p = feat.properties || {};
      if (isPoint) {
        const [clon, clat] = feat.geometry.coordinates;
        lyr.bindPopup(() => campgroundCard(p, clat, clon), { maxWidth: 250, className: "cg-popup" });
      } else {
        const tip = isOwn
          ? `${p.category || "—"}${p.name ? " · " + p.name + " Co." : ""}`
          : p.name || overlayDefs.find((d) => d.id === layerId)?.title || layerId;
        if (tip) lyr.bindTooltip(tip, { sticky: true });
      }
    },
  });
  overlayLayers[layerId] = gjLayer.addTo(map);
  if (layerId === "campground") {
    campgroundGj = gj;
    updateCgCount(gj);
  }
}

// --- campground filters (Phase 2; client-side over meta) -------------------
function campgroundFilters() {
  return {
    firstcome: $("f-firstcome").checked,
    electric: $("f-electric").checked,
    water: $("f-water").checked,
    pets: $("f-pets").checked,
    ada: $("f-ada").checked,
    rv: parseFloat($("f-rv").value) || 0,
  };
}

function matchesCampground(props) {
  if (!$("f-firstcome")) return true; // panel not built yet
  const f = campgroundFilters();
  const m = props.meta || {};
  // First-come = known not-reservable (RIDB) or a state-forest campground
  // (MN state-forest campgrounds are rustic / first-come, no reservation).
  if (f.firstcome && !(m.reservable === false || props.category === "State Forest")) return false;
  if (f.electric && !m.electric) return false;
  if (f.water && !m.water_front) return false;
  if (f.pets && !m.pets) return false;
  if (f.ada && !m.ada) return false;
  if (f.rv > 0 && !(m.max_rv_ft >= f.rv)) return false;
  return true;
}

function applyCampgroundFilters() {
  if (overlayActive.has("campground") && campgroundGj) {
    renderOverlayLayer("campground", campgroundGj);
  }
}

function updateCgCount(gj) {
  const el = $("cg-filt-count");
  if (!el) return;
  const feats = gj.features || [];
  const shown = feats.filter((f) => matchesCampground(f.properties)).length;
  const active = ["f-firstcome", "f-electric", "f-water", "f-pets", "f-ada"].some(
    (id) => $(id) && $(id).checked
  ) || ($("f-rv") && $("f-rv").value);
  el.textContent = active ? `${shown} of ${feats.length} shown` : "";
}

// A campground's link: the specific reservation/park page when we have a
// verified one, else a generic fallback to the managing agency's main site
// (so every campground is at least one click from its booking info).
function campgroundLink(p) {
  const url = p.url || "";
  if (url) {
    const src = /recreation\.gov/.test(url)
      ? "Recreation.gov"
      : /dnr\.state\.mn\.us/.test(url)
        ? "MN DNR"
        : /goingtocamp/.test(url)
          ? "GoingToCamp"
          : /dnr\.wisconsin\.gov/.test(url)
            ? "WI DNR"
            : "site";
    return { href: url, label: `View on ${src}` };
  }
  if (/Federal/.test(p.category || "")) {
    return {
      href: "https://www.recreation.gov/search?inventory_type=camping",
      label: "Search Recreation.gov",
    };
  }
  // Wisconsin state-park/forest sites (OSM-sourced) reserve via GoingToCamp.
  if ((p.meta || {}).state === "WI") {
    return { href: "https://wisconsin.goingtocamp.com/", label: "Reserve · GoingToCamp" };
  }
  const isForest = p.category === "State Forest" || /state forest/i.test(p.name || "");
  return isForest
    ? { href: "https://www.dnr.state.mn.us/state_forests/index.html", label: "MN DNR State Forests" }
    : { href: "https://www.dnr.state.mn.us/state_parks/index.html", label: "MN DNR State Parks" };
}

// Rich popup card for a campground: badges + facts + blurb + the link.
function campgroundCard(p, lat, lon) {
  const m = p.meta || {};
  const { href, label } = campgroundLink(p);
  const star =
    lat != null && lon != null
      ? starButton("campground", lat, lon, p.name, {
          category: p.category,
          name: p.name,
          url: p.url,
          meta: p.meta,
        })
      : "";
  const badges = [];
  if (m.sites) badges.push(`${m.sites} sites`);
  badges.push(...amenityBadges(m));
  if (m.fee) badges.push("💲 " + String(m.fee).replace(/\s*per night/i, "/night"));

  const tripBtn =
    lat != null && lon != null
      ? tripAddButton("campground", lat, lon, p.name, {
          category: p.category,
          name: p.name,
          url: p.url,
          meta: p.meta,
        })
      : "";

  const row = (cls, txt) => (txt ? `<div class="${cls}">${txt}</div>` : "");
  const badgeRow = badgeRowHtml(badges);
  const types = (m.site_types || []).map(esc).join(" · ");
  const acts = (m.activities || []).map(esc).join(" · ");

  return (
    `<div class="cg-pop">${star}<b>${esc(p.name) || "Campground"}</b>` +
    `<div class="cg-cat">${esc(p.category)}</div>` +
    badgeRow +
    row("cg-facts", types) +
    row("cg-facts cg-acts", acts) +
    row("cg-facts", m.phone ? "☎ " + esc(m.phone) : "") +
    row("cg-blurb", esc(m.blurb)) +
    `<div class="cg-actions">` +
    (href ? `<a class="cg-link" href="${esc(href)}" target="_blank" rel="noopener">${label} ↗</a>` : "") +
    tripBtn +
    `</div></div>`
  );
}

function overlayStyle(layerId, feat) {
  if (layerId === "ownership") {
    const c = OWNERSHIP_COLORS[(feat.properties || {}).category] || OWNERSHIP_FALLBACK;
    return { color: c, weight: 1, fillColor: c, fillOpacity: 0.35 };
  }
  const c = BOUNDARY_COLORS[layerId] || "#555";
  return { color: c, weight: 2, fillColor: c, fillOpacity: 0.08 };
}

function renderLandLegend() {
  const host = $("land-legend");
  host.innerHTML = "";
  const addKeys = (entries, round) => {
    Object.entries(entries).forEach(([cat, c]) => {
      const row = document.createElement("span");
      row.className = "land-key";
      const sw = round ? "land-swatch round" : "land-swatch";
      row.innerHTML = `<span class="${sw}" style="background:${c}"></span>${cat}`;
      host.appendChild(row);
    });
  };
  if (overlayActive.has("ownership")) addKeys(OWNERSHIP_COLORS, false);
  if (overlayActive.has("campground")) addKeys(CAMPGROUND_COLORS, true);
}

function updateLandHint() {
  const hint = $("land-hint");
  if (overlayActive.has("ownership") && map.getZoom() < OWNERSHIP_MIN_ZOOM) {
    hint.textContent = "Zoom in to load ownership parcels.";
  } else if (overlayTruncated.size) {
    hint.textContent = "Showing a capped subset — zoom in for full detail.";
  } else {
    hint.textContent = "";
  }
}

// Bind the "why this score" breakdown to a scored marker as a map popup, and
// fill in the nearest-campground line once the popup opens (lazy — TLC-3).
// Show a scored site's "why this score" in the bottom sheet — a persistent,
// mobile-friendly panel that stays put while you explore nearby campground
// popups (easy to return to), instead of a map popup.
function clearSelectedMarker() {
  if (selectedMarker) {
    map.removeLayer(selectedMarker);
    selectedMarker = null;
  }
}

function showSite(p, lat, lon, presetName) {
  clearSelectedMarker(); // any prior saved-place marker is no longer current
  const body = $("sheet-body");
  body.innerHTML = breakdownHtml(p, lat, lon);
  $("sheet").classList.remove("hidden");
  const btn = body.querySelector(".bd-toggle");
  const details = body.querySelector(".bd-details");
  if (btn && details) {
    btn.onclick = () => {
      const collapsed = details.classList.toggle("collapsed");
      btn.textContent = (collapsed ? "▸" : "▾") + " Why this score";
    };
  }
  wireSiteName(body.querySelector("#site-name"), lat, lon, presetName);
  body.querySelectorAll(".bd-rating .rstar").forEach((el, i) => {
    el.onclick = () => setRating(lat, lon, i + 1);
  });
  wireSiteNote(body.querySelector(".bd-note"), lat, lon);
  loadNearbyCampgrounds(lat, lon, body);
}

// Free-text note for the open site. Like rating, editing a note saves the site
// first; persists on blur (the textarea's change event).
function noteFieldHtml(lat, lon) {
  const id = savedByKey.get(savedKey("site", lat, lon));
  const note = id != null ? savedPlaces.find((s) => s.id === id)?.note || "" : "";
  return `<textarea class="bd-note" placeholder="Notes…" rows="4">${esc(note)}</textarea>`;
}

function wireSiteNote(noteEl, lat, lon) {
  if (!noteEl) return;
  let savedVal = noteEl.value;
  noteEl.addEventListener("change", async () => {
    const v = noteEl.value;
    if (v === savedVal) return;
    const id = await ensureSavedFromSheet(); // a note keeps the site
    if (id == null) return;
    const r = await fetch(`/saved/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: v.trim() || null }),
    });
    if (!r.ok) return;
    const u = await r.json();
    const i = savedPlaces.findIndex((x) => x.id === id);
    if (i >= 0) savedPlaces[i] = u;
    savedVal = v;
    renderSavedList();
  });
}

// A 1-5 star rating for the open site. Stars reflect the saved record's rating
// (0 if unsaved). Rating a site saves it first (rating implies you want it kept);
// clicking the current rating again clears it.
function ratingRowHtml(lat, lon) {
  const id = savedByKey.get(savedKey("site", lat, lon));
  const cur = id != null ? Math.round(savedPlaces.find((s) => s.id === id)?.rating || 0) : 0;
  const stars = [1, 2, 3, 4, 5]
    .map((n) => `<span class="rstar${n <= cur ? " on" : ""}" data-n="${n}">★</span>`)
    .join("");
  return `<div class="bd-rating" title="Rate this site">${stars}</div>`;
}

function paintRating(r) {
  const n = Math.round(r || 0);
  document
    .querySelectorAll(".bd-rating .rstar")
    .forEach((el, i) => el.classList.toggle("on", i < n));
}

async function setRating(lat, lon, n) {
  const id = await ensureSavedFromSheet(); // rating a site keeps it
  if (id == null) return;
  const s = savedPlaces.find((x) => x.id === id);
  const next = s && Math.round(s.rating || 0) === n ? null : n; // click current => clear
  const r = await fetch(`/saved/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: next }),
  });
  if (!r.ok) return;
  const updated = await r.json();
  const i = savedPlaces.findIndex((x) => x.id === id);
  if (i >= 0) savedPlaces[i] = updated;
  paintRating(updated.rating);
  renderSavedList();
}

// Ensure the open site is saved (saving it if needed via the sheet's ★ data),
// returning its saved id. Reuses toggleFav's save path (name dedupe, ★ flip).
async function ensureSavedFromSheet() {
  const btn = document.querySelector(".bd-namebar .star");
  if (!btn) return null;
  let fav;
  try {
    fav = JSON.parse(btn.dataset.fav);
  } catch {
    return null;
  }
  if (!isSaved(fav.kind, fav.lat, fav.lon)) {
    await toggleFav(fav, btn); // saves it
  }
  return savedByKey.get(savedKey(fav.kind, fav.lat, fav.lon)) ?? null;
}

// Editable site name: a saved place keeps its stored name; a fresh site is
// auto-suggested (nearest lake / road / reverse-geocode) unless the user types.
// Renaming while saved persists via PATCH.
function wireSiteName(nameEl, lat, lon, presetName) {
  if (!nameEl) return;
  let edited = false;
  nameEl.addEventListener("input", () => {
    edited = true;
  });
  if (presetName != null) {
    nameEl.value = presetName;
    edited = true;
  } else {
    fetch(`/suggest-name?lat=${lat}&lon=${lon}`)
      .then((r) => r.json())
      .then((d) => {
        if (!edited && d.name) nameEl.value = d.name;
      })
      .catch(() => {});
  }
  nameEl.addEventListener("change", () => {
    const id = savedByKey.get(savedKey("site", lat, lon));
    if (id == null) return; // not saved — name is used when you star
    const name = nameEl.value.trim() || null;
    fetch(`/saved/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(() => {
      const s = savedPlaces.find((x) => x.id === id);
      if (s) {
        s.name = name;
        renderSavedList();
      }
    });
  });
}

function closeSite() {
  $("sheet").classList.add("hidden");
  nearbyCgLayer.clearLayers();
  clearSelectedMarker();
  map.closePopup();
}

// A popup can open hidden behind the bottom sheet — the sheet floats above the
// map's z-index, so a marker sitting under it opens its info window out of
// sight. When an opening popup overlaps the sheet, nudge the map so it clears:
// past the sheet's right edge on desktop, above it on mobile (full-width sheet).
function keepPopupClearOfSheet(popup) {
  const sheet = $("sheet");
  if (!sheet || sheet.classList.contains("hidden")) return;
  const el = popup.getElement && popup.getElement();
  if (!el) return;
  const p = el.getBoundingClientRect();
  const s = sheet.getBoundingClientRect();
  const overlapX = Math.min(p.right, s.right) - Math.max(p.left, s.left);
  const overlapY = Math.min(p.bottom, s.bottom) - Math.max(p.top, s.top);
  if (overlapX <= 0 || overlapY <= 0) return; // not hidden by the sheet
  const margin = 14;
  if (window.matchMedia("(max-width: 640px)").matches) {
    map.panBy([0, p.bottom - s.top + margin], { animate: true }); // lift above the sheet
  } else {
    map.panBy([-(s.right - p.left + margin), 0], { animate: true }); // shift past its right edge
  }
}

// Emoji+label amenity badges from a campground's meta — the shared vocabulary
// used by both the map infowindow (campgroundCard) and the nearby-campground
// list, so a campground reads identically in either place.
function amenityBadges(meta) {
  const m = meta || {};
  const b = [];
  if (typeof m.reservable === "boolean") b.push(m.reservable ? "✅ reservable" : "🪧 first-come");
  if (m.electric) b.push("🔌 electric");
  if (m.max_rv_ft) b.push(`🚐 ${m.max_rv_ft} ft`);
  if (m.water_front) b.push("💧 waterfront");
  if (m.pets) b.push("🐾 pets");
  if (m.ada) b.push("♿ ADA");
  return b;
}

// Wrap badge strings in the shared pill markup (empty string if none).
function badgeRowHtml(badges) {
  return badges.length
    ? `<div class="cg-badges">${badges.map((b) => `<span>${esc(b)}</span>`).join("")}</div>`
    : "";
}

// --- saved places (favorites; server-side so they sync laptop↔phone) -------
let savedPlaces = [];
const savedByKey = new Map(); // key -> id
const savedKey = (kind, lat, lon) => `${kind}:${(+lat).toFixed(5)}:${(+lon).toFixed(5)}`;
const isSaved = (kind, lat, lon) => savedByKey.has(savedKey(kind, lat, lon));

async function loadSaved() {
  try {
    savedPlaces = (await (await fetch("/saved")).json()).saved || [];
  } catch {
    savedPlaces = [];
  }
  savedByKey.clear();
  savedPlaces.forEach((s) => savedByKey.set(savedKey(s.kind, s.lat, s.lon), s.id));
  renderSavedList();
}

// A ★/☆ toggle carrying its place as JSON (read by the delegated click handler).
function starButton(kind, lat, lon, name, payload) {
  const on = isSaved(kind, lat, lon);
  const fav = esc(JSON.stringify({ kind, name: name || null, lat, lon, payload: payload || null }));
  return `<button class="star${on ? " on" : ""}" title="Save" data-fav="${fav}">${on ? "★" : "☆"}</button>`;
}

// Make a name unique within the saved list by appending " (2)", " (3)"…
function uniqueName(base) {
  if (!base) return base;
  const taken = new Set(savedPlaces.map((s) => (s.name || "").toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  let n = 2;
  while (taken.has(`${base} (${n})`.toLowerCase())) n++;
  return `${base} (${n})`;
}

async function toggleFav(fav, btn) {
  // For a site, the live name input wins over the baked-in (stale) name.
  if (fav.kind === "site") {
    const nameEl = $("site-name");
    if (nameEl) fav.name = nameEl.value.trim() || null;
  }
  const key = savedKey(fav.kind, fav.lat, fav.lon);
  const id = savedByKey.get(key);
  if (id != null) {
    await fetch(`/saved/${id}`, { method: "DELETE" });
    savedPlaces = savedPlaces.filter((s) => s.id !== id);
    savedByKey.delete(key);
  } else {
    fav.name = uniqueName(fav.name); // dedupe "Near Mallard Lake" -> " (2)"
    const r = await fetch("/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fav),
    });
    if (r.ok) {
      const row = await r.json();
      savedPlaces.unshift(row);
      savedByKey.set(key, row.id);
      // Reflect the deduped name back into the sheet's name field.
      const nameEl = fav.kind === "site" && $("site-name");
      if (nameEl) nameEl.value = row.name || "";
    }
  }
  if (btn) {
    const on = savedByKey.has(key);
    btn.textContent = on ? "★" : "☆";
    btn.classList.toggle("on", on);
  }
  renderSavedList();
}

function renderSavedList() {
  const host = $("saved-list");
  if (!host) return;
  $("saved-section").hidden = savedPlaces.length === 0;
  host.innerHTML = savedPlaces
    .map((s, i) => {
      // Three kinds share the list: a campground, a Cairn-scored dispersed
      // site (payload carries a total), and a manually dropped pin (no score).
      const scored = s.kind === "site" && s.payload && s.payload.total != null;
      const icon = s.kind === "campground" ? "🏕" : scored ? "⛺" : "📍";
      const label = s.name || (scored ? "Scored site" : "Dropped pin");
      const coords = `${(+s.lat).toFixed(4)}, ${(+s.lon).toFixed(4)}`;
      const score = scored
        ? ` · <span class="saved-score">score ${s.payload.total.toFixed(2)}</span>`
        : "";
      const stars = s.rating
        ? ` · <span class="saved-stars">${"★".repeat(Math.round(s.rating))}</span>`
        : "";
      const noteLine = s.note ? `<div class="saved-note">${esc(s.note)}</div>` : "";
      return (
        `<div class="saved-row" data-i="${i}"><div class="saved-main">` +
        `<div class="saved-name">${icon} ${esc(label)}</div>` +
        `<div class="saved-coords">${coords}${score}${stars}</div>` +
        noteLine +
        `</div>` +
        `<button class="saved-add" title="Add to a trip">＋</button>` +
        `<button class="saved-del" title="Remove">×</button></div>`
      );
    })
    .join("");
  host.querySelectorAll(".saved-row").forEach((row, i) => {
    row.querySelector(".saved-main").onclick = () => reopenSaved(savedPlaces[i]);
    row.querySelector(".saved-add").onclick = (e) => {
      e.stopPropagation();
      openTripPickerAt(savedPlaces[i], e.currentTarget);
    };
    row.querySelector(".saved-del").onclick = (e) => {
      e.stopPropagation();
      toggleFav(savedPlaces[i]);
    };
  });
}

function reopenSaved(s) {
  // On mobile the panel is a full-height drawer over the map — close it so the
  // tapped place (its marker + bottom sheet) is actually visible.
  if (window.matchMedia("(max-width: 640px)").matches) setNav(false);
  map.setView([s.lat, s.lon], 14);
  if (s.kind === "campground") {
    closeSite(); // hide any open sheet (also clears the prior selected marker)
    const payload = s.payload || { name: s.name };
    const color = CAMPGROUND_COLORS[payload.category] || "#d96c2c";
    selectedMarker = L.marker([s.lat, s.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div class="cg-pin cg-near" style="--cg:${color}">🏕️</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }),
    }).bindPopup(() => campgroundCard(payload, s.lat, s.lon), {
      maxWidth: 250,
      className: "cg-popup",
    });
    selectedMarker.on("popupclose", clearSelectedMarker);
    selectedMarker.addTo(map).openPopup();
  } else {
    showSite(s.payload || {}, s.lat, s.lon, s.name || ""); // clears the old marker first
    selectedMarker = L.marker([s.lat, s.lon], {
      icon: L.divIcon({ className: "", html: `<div class="sel-pin">★</div>`,
        iconSize: [26, 26], iconAnchor: [13, 13] }),
    }).addTo(map);
  }
}

// List the nearest campgrounds in the sheet + show their markers on the map.
// A row or marker tap opens that campground's card as a normal map popup — the
// site stays in the sheet, so there's no popup conflict. Display-only (TLC-3).
async function loadNearbyCampgrounds(lat, lon, body) {
  let list;
  try {
    list = (await (await fetch(`/nearby-campgrounds?lat=${lat}&lon=${lon}&limit=5`)).json())
      .campgrounds;
  } catch {
    return;
  }
  nearbyCgLayer.clearLayers();
  if (!list || !list.length) return;

  const markers = list.map((cg) => {
    const color = CAMPGROUND_COLORS[cg.category] || "#d96c2c";
    const mk = L.marker([cg.lat, cg.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div class="cg-pin cg-near" style="--cg:${color}">🏕️</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }),
    }).bindPopup(() => campgroundCard(cg, cg.lat, cg.lon), { maxWidth: 250, className: "cg-popup" });
    nearbyCgLayer.addLayer(mk);
    return mk;
  });

  const nearEl = body.querySelector(".bd-nearby");
  if (!nearEl) return;
  nearEl.innerHTML =
    `<div class="bd-near-title">Nearby campgrounds</div>` +
    list
      .map((cg, i) => {
        const mi = (cg.distance_m / 1609.34).toFixed(1);
        return (
          `<div class="bd-cg" data-i="${i}">` +
          `<div class="bd-cg-head">` +
          `<span class="bd-cg-name">🏕 ${esc(cg.name)}</span>` +
          `<span class="bd-cg-dist">${mi} mi</span></div>` +
          badgeRowHtml(amenityBadges(cg.meta)) +
          `</div>`
        );
      })
      .join("");
  nearEl.querySelectorAll(".bd-cg").forEach((row, i) => {
    row.onclick = () => {
      map.panTo([list[i].lat, list[i].lon]);
      markers[i].openPopup();
    };
  });
}

function breakdownHtml(p, lat, lon, { expanded = false } = {}) {
  p = p || {};
  const hasScore = p.total != null;
  const coords = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
  const nameBar =
    `<div class="bd-namebar">${starButton("site", lat, lon, null, p)}` +
    `<input id="site-name" class="bd-name" placeholder="${
      hasScore ? "Name this site…" : "Name this location…"
    }" autocomplete="off" />${tripAddButton("site", lat, lon, null, p)}</div>`;
  const ratingRow = ratingRowHtml(lat, lon);
  const noteRow = noteFieldHtml(lat, lon);
  const coordsHtml =
    `<div class="bd-coords">📍 <span class="bd-ll">${coords}</span>` +
    `<a class="bd-gmaps" href="${gmaps}" target="_blank" rel="noopener">` +
    `Open in Google&nbsp;Maps ↗</a></div>`;
  // A dropped pin has no score — just name it, rate it, note it, see nearby, save.
  if (!hasScore) {
    return (
      nameBar + ratingRow + coordsHtml + noteRow +
      `<div class="bd-nearby"></div>` +
      `<div class="bd-raw bd-disc">${CONFIG.disclaimer}</div>`
    );
  }
  const rows = (p.contributions || [])
    .map((c) => {
      const pct = Math.round(c.normalized * 100);
      return (
        `<div class="bd-feature"><div class="bd-top">` +
        `<span>${FEATURE_LABELS[c.feature] || c.feature}</span>` +
        `<span>${c.normalized.toFixed(2)} × ${c.weight.toFixed(2)} = <b>${c.contribution.toFixed(
          3
        )}</b></span></div>` +
        `<div class="bd-bar"><span style="width:${pct}%"></span></div></div>`
      );
    })
    .join("");
  const roadPct = p.road_score == null ? "—" : Math.round(p.road_score * 100) + "%";
  const localKm = p.local_road_m == null ? "—" : (p.local_road_m / 1000).toFixed(1) + " km";
  const rough = p.rough ? ` <span class="rough-badge">⚠ rough / 4WD</span>` : "";
  const endM = p.dist_terminus_m == null ? "—" : fmt(p.dist_terminus_m, " m");
  const raw =
    `road ${roadPct} · ${endM} to road-end · ${localKm} road within 1.5 km · ` +
    `slope ${fmt(p.slope_deg, "°")} · water ${fmt(p.dist_water_m, " m")} · ` +
    `forest ${fmt(p.dist_forest_m, " m")} · solitude ${fmt(p.dist_solitude_m, " m")}` +
    (p.near_campsite ? " · near a known campsite" : "");
  return (
    nameBar +
    ratingRow +
    coordsHtml +
    noteRow +
    `<div class="bd-total">Total: ${p.total.toFixed(3)} (rank #${p.rank})${rough}</div>` +
    `<div class="bd-nearby"></div>` +
    `<button type="button" class="bd-toggle">${expanded ? "▾" : "▸"} Why this score</button>` +
    `<div class="bd-details${expanded ? "" : " collapsed"}">` +
    `${rows}<div class="bd-raw">${raw}</div></div>` +
    `<div class="bd-raw bd-disc">${CONFIG.disclaimer}</div>`
  );
}

// Display-only "book a spot" fallback: the nearest established campground to a
// scored dispersed site. Fetched lazily when the breakdown opens (keeps the run
// geojson lean); never part of scoring (TLC-3).
function fmt(v, unit) {
  return v === null || v === undefined ? "—" : Math.round(v) + unit;
}

// === Trips =================================================================
// Group saved places into a named, dated outing, plot them together, and
// export GPX/KML for the field (onX / Gaia / Google). A trip member references
// a saved place with a role: 'primary' (a free dispersed idea) or 'fallback'
// (a reservable campground). User data + display only (TLC-3).
let trips = []; // GET /trips — list rows with member_count
let tripsLoaded = false; // fetched /trips at least once this session
let activeTrip = null; // hydrated trip open in the detail view

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      if (tab.classList.contains("active")) return;
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const which = tab.dataset.tab;
      $("score-section").hidden = which !== "score";
      $("trips-section").hidden = which !== "trips";
      if (which === "trips") loadTrips();
      else exitTripPlot(); // back to Score: restore the normal map
    };
  });
  $("trip-new").onclick = () => toggleTripForm(true);
  $("trip-new-cancel").onclick = () => toggleTripForm(false);
  $("trip-new-form").onsubmit = (e) => {
    e.preventDefault();
    createTrip();
  };
  $("trip-back").onclick = showTripsList;
  $("trip-plot").onclick = plotTrip;
  $("trip-gpx").onclick = () => exportTrip("gpx");
  $("trip-kml").onclick = () => exportTrip("kml");
}

function tripDateLabel(t) {
  if (t.start_date && t.end_date) return `${t.start_date} → ${t.end_date}`;
  return t.start_date || t.end_date || "";
}

function toggleTripForm(show) {
  $("trip-new-form").hidden = !show;
  $("trip-form-err").textContent = "";
  if (show) {
    $("trip-name").value = "";
    $("trip-start").value = "";
    $("trip-end").value = "";
    $("trip-name").focus();
  }
}

async function loadTrips() {
  try {
    trips = (await (await fetch("/trips")).json()).trips || [];
  } catch {
    trips = [];
  }
  tripsLoaded = true;
  renderTripsList();
}

function renderTripsList() {
  const host = $("trips-list");
  if (!host) return;
  $("trips-empty").hidden = trips.length > 0;
  host.innerHTML = trips
    .map((t, i) => {
      const dates = tripDateLabel(t);
      const n = t.member_count || 0;
      return (
        `<div class="trip-row" data-i="${i}"><div class="trip-row-main">` +
        `<div class="trip-row-name">${esc(t.name)}</div>` +
        (dates ? `<div class="trip-row-sub">${esc(dates)}</div>` : "") +
        `</div><span class="trip-row-count">${n} place${n === 1 ? "" : "s"}</span></div>`
      );
    })
    .join("");
  host.querySelectorAll(".trip-row").forEach((row, i) => {
    row.onclick = () => openTrip(trips[i].id);
  });
}

async function createTrip() {
  const name = $("trip-name").value.trim();
  const errEl = $("trip-form-err");
  errEl.className = "status";
  if (!name) {
    errEl.textContent = "Give the trip a name.";
    errEl.className = "status err";
    return;
  }
  const body = { name };
  if ($("trip-start").value) body.start_date = $("trip-start").value;
  if ($("trip-end").value) body.end_date = $("trip-end").value;
  const r = await fetch("/trips", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    errEl.textContent = e.detail || "Could not create the trip.";
    errEl.className = "status err";
    return;
  }
  const trip = await r.json();
  toggleTripForm(false);
  await loadTrips();
  openTrip(trip.id);
}

async function openTrip(id) {
  const r = await fetch(`/trips/${id}`);
  if (!r.ok) return showTripsList();
  activeTrip = await r.json();
  $("trips-list-view").hidden = true;
  $("trip-detail").hidden = false;
  renderTripDetail();
}

function showTripsList() {
  $("trip-detail").hidden = true;
  $("trips-list-view").hidden = false;
  activeTrip = null;
  loadTrips();
}

function renderTripDetail() {
  const t = activeTrip;
  if (!t) return;
  const dates = tripDateLabel(t);
  const head = $("trip-detail-head");
  head.innerHTML =
    `<div class="trip-head-row"><div class="trip-head-main">` +
    `<h3>${esc(t.name)}</h3>` +
    (dates ? `<div class="trip-head-dates">${esc(dates)}</div>` : "") +
    `</div><div class="trip-head-btns">` +
    `<button class="trip-edit">Edit</button><button class="trip-del">Delete</button>` +
    `</div></div>` +
    `<form class="trip-form trip-edit-form" hidden>` +
    `<input class="trip-edit-name" type="text" value="${esc(t.name)}" />` +
    `<div class="trip-dates">` +
    `<label>Start <input class="trip-edit-start" type="date" value="${t.start_date || ""}" /></label>` +
    `<label>End <input class="trip-edit-end" type="date" value="${t.end_date || ""}" /></label>` +
    `</div><div class="row">` +
    `<button type="submit" class="btn primary">Save</button>` +
    `<button type="button" class="btn ghost trip-edit-cancel">Cancel</button>` +
    `</div><div class="status trip-edit-err"></div></form>`;
  const form = head.querySelector(".trip-edit-form");
  head.querySelector(".trip-edit").onclick = () => {
    form.hidden = !form.hidden;
  };
  head.querySelector(".trip-edit-cancel").onclick = () => {
    form.hidden = true;
  };
  head.querySelector(".trip-del").onclick = deleteTrip;
  form.onsubmit = (e) => {
    e.preventDefault();
    saveTripEdits(form);
  };
  renderTripMembers();
  renderAddPlaces();
}

function memberIcon(m) {
  const scored = m.kind === "site" && m.payload && m.payload.total != null;
  return {
    icon: m.kind === "campground" ? "🏕" : scored ? "⛺" : "📍",
    label: m.name || (m.kind === "campground" ? "Campground" : scored ? "Scored site" : "Dropped pin"),
  };
}

// What a member IS (kind) — independent of its role (first choice vs backup).
function kindWord(kind) {
  return kind === "campground" ? "campground" : kind === "site" ? "dispersed site" : "dropped pin";
}

function tripMemberRow(m) {
  const { icon, label } = memberIcon(m);
  const coords = `${(+m.lat).toFixed(4)}, ${(+m.lon).toFixed(4)}`;
  const geo = `geo:${m.lat},${m.lon}`;
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${m.lat},${m.lon}`;
  const isP = m.role === "primary";
  return (
    `<div class="trip-mem" data-id="${m.saved_id}"><div class="trip-mem-main">` +
    `<div class="trip-mem-name">${icon} ${esc(label)}</div>` +
    `<div class="trip-mem-coords">${coords} · <a href="${esc(geo)}">geo</a> · ` +
    `<a href="${esc(gmaps)}" target="_blank" rel="noopener">Maps ↗</a></div></div>` +
    `<button class="trip-role ${isP ? "is-primary" : "is-fallback"}" ` +
    `title="Switch role (primary ↔ fallback)">${isP ? "P" : "F"}</button>` +
    `<button class="trip-mem-del" title="Remove from trip">×</button></div>`
  );
}

function tripGroupHtml(title, members, emptyHint) {
  const rows = members.length
    ? members.map(tripMemberRow).join("")
    : `<div class="trip-group-empty">${emptyHint}</div>`;
  return `<div class="trip-group-title">${title}</div>${rows}`;
}

function renderTripMembers() {
  const host = $("trip-members");
  const members = activeTrip.members || [];
  host.innerHTML =
    tripGroupHtml(
      "Primary · first choice",
      members.filter((m) => m.role === "primary"),
      "No primary picks yet."
    ) +
    tripGroupHtml(
      "Fallback · backup",
      members.filter((m) => m.role === "fallback"),
      "No backups yet."
    );
  host.querySelectorAll(".trip-mem").forEach((row) => {
    const id = parseInt(row.dataset.id, 10);
    row.querySelector(".trip-role").onclick = () => toggleMemberRole(id);
    row.querySelector(".trip-mem-del").onclick = () => removeMember(id);
  });
}

// The member POST/PATCH endpoints return the hydrated trip; reuse it.
function applyTripUpdate(updated) {
  if (updated && updated.members) activeTrip = updated;
  renderTripMembers();
  renderAddPlaces();
}

async function addMember(savedId, role) {
  const r = await fetch(`/trips/${activeTrip.id}/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ saved_id: savedId, role }),
  });
  if (r.ok) applyTripUpdate(await r.json());
}

async function toggleMemberRole(savedId) {
  const m = (activeTrip.members || []).find((x) => x.saved_id === savedId);
  if (!m) return;
  addMember(savedId, m.role === "primary" ? "fallback" : "primary"); // upsert flips the role
}

async function removeMember(savedId) {
  const r = await fetch(`/trips/${activeTrip.id}/members/${savedId}`, { method: "DELETE" });
  if (!r.ok) return;
  const t = await (await fetch(`/trips/${activeTrip.id}`)).json();
  applyTripUpdate(t);
}

function renderAddPlaces() {
  const host = $("trip-add");
  const memberIds = new Set((activeTrip.members || []).map((m) => m.saved_id));
  const available = savedPlaces.filter((s) => !memberIds.has(s.id));
  host.innerHTML =
    `<button class="trip-add-toggle">＋ Add a saved place</button>` +
    `<div class="trip-add-list" hidden></div>`;
  const list = host.querySelector(".trip-add-list");
  host.querySelector(".trip-add-toggle").onclick = () => {
    list.hidden = !list.hidden;
    if (!list.hidden) fillAddList(list, available);
  };
}

function fillAddList(list, available) {
  if (!available.length) {
    list.innerHTML =
      `<div class="trip-add-empty">All your saved places are already in this trip. ` +
      `Star more sites or campgrounds (Score tab) to add them.</div>`;
    return;
  }
  list.innerHTML = available
    .map((s, i) => {
      const { icon, label } = memberIcon(s);
      return (
        `<div class="trip-add-row" data-i="${i}">` +
        `<span class="trip-add-name">${icon} ${esc(label)}</span>` +
        `<button data-role="primary" title="Add as a primary (first choice)">primary</button>` +
        `<button data-role="fallback" title="Add as a fallback (backup)">fallback</button></div>`
      );
    })
    .join("");
  list.querySelectorAll(".trip-add-row").forEach((row, i) => {
    row.querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => addMember(available[i].id, btn.dataset.role);
    });
  });
}

async function deleteTrip() {
  if (!activeTrip) return;
  if (!confirm(`Delete trip “${activeTrip.name}”? (Your saved places aren't deleted.)`)) return;
  await fetch(`/trips/${activeTrip.id}`, { method: "DELETE" });
  tripLayer.clearLayers();
  showTripsList();
}

async function saveTripEdits(form) {
  const name = form.querySelector(".trip-edit-name").value.trim();
  const errEl = form.querySelector(".trip-edit-err");
  errEl.className = "status";
  if (!name) {
    errEl.textContent = "Name can't be empty.";
    errEl.className = "status err";
    return;
  }
  const body = {
    name,
    start_date: form.querySelector(".trip-edit-start").value || null,
    end_date: form.querySelector(".trip-edit-end").value || null,
  };
  const r = await fetch(`/trips/${activeTrip.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    errEl.textContent = e.detail || "Could not save.";
    errEl.className = "status err";
    return;
  }
  activeTrip = await r.json();
  renderTripDetail();
}

function plotTrip() {
  if (!activeTrip) return;
  const pts = [];
  tripLayer.clearLayers();
  (activeTrip.members || []).forEach((m) => {
    const isP = m.role === "primary";
    // Glyph by KIND (what it is), ring/fill color by ROLE (first choice vs
    // backup) — the two are independent: a campground can be a primary, a
    // dispersed site can be a fallback.
    const { icon, label } = memberIcon(m);
    const html = `<div class="trip-pin ${isP ? "trip-primary" : "trip-fallback"}">${icon}</div>`;
    const mk = L.marker([m.lat, m.lon], {
      icon: L.divIcon({ className: "", html, iconSize: [26, 26], iconAnchor: [13, 13] }),
    }).bindPopup(
      `<b>${esc(m.name || label)}</b><br>` +
        `${isP ? "Primary" : "Fallback"} · ${esc(kindWord(m.kind))}`
    );
    tripLayer.addLayer(mk);
    pts.push([m.lat, m.lon]);
  });
  if (!pts.length) {
    setStatus("This trip has no places yet — add some first.", "err");
    return;
  }
  // Clear the map down to just the trip stops (results, roads, nearby
  // campgrounds, land overlays all hidden) so the trip reads cleanly.
  clearMapForTrip();
  if (!map.hasLayer(tripLayer)) map.addLayer(tripLayer);
  if (window.matchMedia("(max-width: 640px)").matches) setNav(false); // reveal the map
  if (pts.length === 1) map.setView(pts[0], 13);
  else map.fitBounds(pts, { padding: [50, 50], maxZoom: 14 });
}

// Hide everything that isn't a trip stop. Result layers keep their features
// (just detached from the map) so exitTripPlot can restore them; land overlays
// are removed but their toggles/overlayActive stay set, and refreshActiveOverlays
// is suppressed while tripPlotMode is on so a pan doesn't bring them back.
function clearMapForTrip() {
  tripPlotMode = true;
  closeSite(); // also clears nearbyCgLayer + any selected marker
  [pinLayer, candidatesLayer, heatLayer, roadsLayer, drawnItems].forEach((l) => {
    if (l && map.hasLayer(l)) map.removeLayer(l);
  });
  if (gotoMarker) {
    map.removeLayer(gotoMarker);
    gotoMarker = null;
  }
  Object.keys(overlayLayers).forEach((id) => {
    if (overlayLayers[id]) {
      map.removeLayer(overlayLayers[id]);
      delete overlayLayers[id];
    }
  });
}

// Leave the trip-plot view: drop the trip stops and restore the normal map —
// the scored-result layers (per their checkboxes) and any active land overlays.
function exitTripPlot() {
  if (!tripPlotMode) return;
  tripPlotMode = false;
  tripLayer.clearLayers();
  if (drawnItems && !map.hasLayer(drawnItems)) map.addLayer(drawnItems);
  updateLayerVisibility(); // restore checked result layers (pins/dots/heat/roads)
  refreshActiveOverlays(); // re-fetch any active land overlays
}

function exportTrip(fmt) {
  if (!activeTrip) return;
  // A plain navigation triggers the download via the Content-Disposition header.
  window.location = `/trips/${activeTrip.id}/export.${fmt}`;
}

// --- "Add to trip" — compose a trip while scoring / exploring ---------------
// Surfaced next to the ★ on the scored-site sheet, dropped pins, and every
// campground card, plus the ＋ on Saved rows. A member always references a
// saved_place, so this saves the place first if needed, then offers a small
// floating picker: which trip (or a new one, inline) and which role.

// A compact button mirroring the ★'s place payload, placed beside it.
function tripAddButton(kind, lat, lon, name, payload) {
  const fav = esc(JSON.stringify({ kind, name: name || null, lat, lon, payload: payload || null }));
  return `<button class="trip-add-btn" title="Add to a trip" data-fav="${fav}">＋ trip</button>`;
}

// From a map popup / sheet ★-payload: ensure it's saved, then open the picker.
async function addToTripFlow(fav, btn) {
  let id = savedByKey.get(savedKey(fav.kind, fav.lat, fav.lon));
  if (id == null) {
    const star = btn.parentElement && btn.parentElement.querySelector(".star");
    await toggleFav(fav, star || undefined); // saves it (it isn't saved yet)
    id = savedByKey.get(savedKey(fav.kind, fav.lat, fav.lon));
  }
  if (id == null) return;
  const place = savedPlaces.find((s) => s.id === id) || { id, ...fav };
  openTripPickerAt(place, btn);
}

let _tripPicker = null;
function closeTripPicker() {
  if (_tripPicker) {
    _tripPicker.remove();
    _tripPicker = null;
    document.removeEventListener("click", _onPickerOutside, true);
    if (map) map.off("movestart", closeTripPicker);
  }
}
function _onPickerOutside(e) {
  if (_tripPicker && !_tripPicker.contains(e.target)) closeTripPicker();
}

// A floating picker positioned under `anchorEl` (document-level, so it's never
// clipped by a Leaflet popup). Choose an existing trip or create one inline;
// pick the role (primary = first choice, fallback = backup; default primary,
// independent of the place's kind).
async function openTripPickerAt(place, anchorEl) {
  const same = _tripPicker && _tripPicker._for === place.id;
  closeTripPicker();
  if (same) return; // a second click toggles it closed
  if (!tripsLoaded) await loadTrips();

  const hasTrips = trips.length > 0;
  const opts = trips.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  const pick = document.createElement("div");
  pick.className = "trip-picker floating";
  pick._for = place.id;
  pick.innerHTML =
    (hasTrips
      ? `<select class="trip-picker-sel">${opts}<option value="__new__">＋ New trip…</option></select>`
      : "") +
    `<input class="trip-picker-new" type="text" placeholder="New trip name" ${
      hasTrips ? "hidden" : ""
    } autocomplete="off" />` +
    `<div class="trip-picker-roles">` +
    `<label><input type="radio" name="tp-role" value="primary" checked> primary</label>` +
    `<label><input type="radio" name="tp-role" value="fallback"> fallback</label></div>` +
    `<button class="btn trip-picker-add">Add to trip</button>` +
    `<div class="trip-picker-err"></div>`;
  document.body.appendChild(pick);
  _tripPicker = pick;
  positionPicker(pick, anchorEl);

  const sel = pick.querySelector(".trip-picker-sel");
  const newInput = pick.querySelector(".trip-picker-new");
  if (sel) {
    sel.onchange = () => {
      newInput.hidden = sel.value !== "__new__";
      if (!newInput.hidden) newInput.focus();
    };
  }
  // Defer wiring the outside-click closer so the opening click doesn't trip it;
  // also close if the map starts moving (the anchor would drift).
  setTimeout(() => document.addEventListener("click", _onPickerOutside, true), 0);
  if (map) map.on("movestart", closeTripPicker);

  pick.querySelector(".trip-picker-add").onclick = async (e) => {
    e.stopPropagation();
    const errEl = pick.querySelector(".trip-picker-err");
    const role = pick.querySelector('input[name="tp-role"]:checked').value;
    const makeNew = !sel || sel.value === "__new__";
    let tripId;
    if (makeNew) {
      const name = newInput.value.trim();
      if (!name) {
        errEl.textContent = "Name the new trip.";
        newInput.focus();
        return;
      }
      const cr = await fetch("/trips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!cr.ok) {
        errEl.textContent = "Couldn't create the trip.";
        return;
      }
      tripId = (await cr.json()).id;
    } else {
      tripId = parseInt(sel.value, 10);
    }
    const r = await fetch(`/trips/${tripId}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ saved_id: place.id, role }),
    });
    closeTripPicker();
    if (!r.ok) return;
    const updated = await r.json();
    const name = (trips.find((t) => t.id === tripId) || updated || {}).name || "trip";
    tripToast(`Added to ${name}`);
    if (tripsLoaded) loadTrips(); // refresh counts (+ pick up a new trip)
    if (activeTrip && activeTrip.id === tripId && !$("trip-detail").hidden) {
      applyTripUpdate(updated);
    }
  };
}

// Place the floating picker just below the anchor, clamped to the viewport.
function positionPicker(pick, anchorEl) {
  const r = anchorEl.getBoundingClientRect();
  const w = 212;
  pick.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
  pick.style.top = Math.min(r.bottom + 4, window.innerHeight - 8) + "px";
}

// A brief confirmation toast (top-center), auto-dismissed.
let _toastTimer = null;
function tripToast(msg) {
  let el = $("trip-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "trip-toast";
    el.className = "trip-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 1900);
}

init();
