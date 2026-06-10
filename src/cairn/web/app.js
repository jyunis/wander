"use strict";

// --- state -----------------------------------------------------------------
let CONFIG = null;
let map, drawnItems, heatLayer, pinLayer, candidatesLayer, roadsLayer, canvasRenderer, gotoMarker;
let nearbyCgLayer; // ephemeral markers: the campgrounds near the open scored site
let selectedMarker = null; // marker for the currently-selected (saved) place
let baseLayers = []; // current basemap tile layer(s): imagery + optional labels
let bbox = null; // {min_lon,min_lat,max_lon,max_lat}
let activeRunId = null;

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
// Ownership categories colour-coded; Tax Forfeit is the highlight (often
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
  loadSaved();
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
  map.on("popupopen", (e) => keepPopupClearOfSheet(e.popup));

  drawnItems = new L.FeatureGroup().addTo(map);
  // Canvas renderer so thousands of candidate dots stay smooth.
  canvasRenderer = L.canvas({ padding: 0.5 });
  // roadsLayer holds Cairn's own drivable ways (OSM ∪ MVUM) as vector lines —
  // off by default. Added before candidates/pins so it renders beneath them.
  roadsLayer = new L.LayerGroup();
  candidatesLayer = new L.LayerGroup().addTo(map);
  pinLayer = new L.LayerGroup().addTo(map);
  nearbyCgLayer = new L.LayerGroup().addTo(map);

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

// Score -> colour on a red->amber->green ramp. Normalized to THIS run's score
// range so the spread is visible even when totals cluster; the absolute total
// is always shown in the breakdown panel.
function scoreColor(t) {
  const hue = 120 * Math.max(0, Math.min(1, t));
  return `hsl(${hue}, 68%, 47%)`;
}

function renderResults(gj) {
  candidatesLayer.clearLayers();
  pinLayer.clearLayers();
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  const feats = gj.features || [];
  if (!feats.length) {
    updateLayerVisibility();
    return;
  }

  const totals = feats.map((f) => f.properties.total);
  const min = Math.min(...totals), max = Math.max(...totals);
  const norm = (t) => (max > min ? (t - min) / (max - min) : 0.5);

  // Default lens: every passing candidate as a score-coloured dot.
  feats.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const dot = L.circleMarker([lat, lon], {
      renderer: canvasRenderer,
      radius: 4,
      weight: 0,
      fillColor: scoreColor(norm(p.total)),
      fillOpacity: 0.85,
    });
    dot.on("click", () => showSite(p, lat, lon));
    candidatesLayer.addLayer(dot);
  });

  // Numbered pins for the top 25 DISTINCT sites (spatial NMS, not adjacent
  // road samples). Fall back to plain rank for pre-NMS runs (no distinct_rank).
  const distinct = feats
    .filter((f) => f.properties.distinct && f.properties.distinct_rank != null)
    .sort((a, b) => a.properties.distinct_rank - b.properties.distinct_rank);
  const MAX_PINS = 60;
  const pinFeats = distinct.length ? distinct.slice(0, MAX_PINS) : feats.slice(0, MAX_PINS);
  pinFeats.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const label = p.distinct_rank != null ? p.distinct_rank : p.rank;
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "", html: `<div class="rank-pin">${label}</div>`, iconSize: [22, 22] }),
    });
    marker.on("click", () => showSite(p, lat, lon));
    pinLayer.addLayer(marker);
  });

  // Heatmap is built but only shown when its checkbox is on.
  const heat = feats.map((f) => [
    f.geometry.coordinates[1],
    f.geometry.coordinates[0],
    Math.max(0.05, f.properties.total),
  ]);
  heatLayer = L.heatLayer(heat, { radius: 22, blur: 16, max: 1.0 });

  updateLayerVisibility();
  const b = gj.run.bbox;
  map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [20, 20] });
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
function refreshActiveOverlays() {
  if (!overlayActive.size) return;
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
        : "site";
    return { href: url, label: `View on ${src}` };
  }
  if (/Federal/.test(p.category || "")) {
    return {
      href: "https://www.recreation.gov/search?inventory_type=camping",
      label: "Search Recreation.gov",
    };
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
    (href ? `<a class="cg-link" href="${esc(href)}" target="_blank" rel="noopener">${label} ↗</a>` : "") +
    `</div>`
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
  loadNearbyCampgrounds(lat, lon, body);
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
      return (
        `<div class="saved-row" data-i="${i}"><div class="saved-main">` +
        `<div class="saved-name">${icon} ${esc(label)}</div>` +
        `<div class="saved-coords">${coords}${score}</div></div>` +
        `<button class="saved-del" title="Remove">×</button></div>`
      );
    })
    .join("");
  host.querySelectorAll(".saved-row").forEach((row, i) => {
    row.querySelector(".saved-main").onclick = () => reopenSaved(savedPlaces[i]);
    row.querySelector(".saved-del").onclick = (e) => {
      e.stopPropagation();
      toggleFav(savedPlaces[i]);
    };
  });
}

function reopenSaved(s) {
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
    }" autocomplete="off" /></div>`;
  const coordsHtml =
    `<div class="bd-coords">📍 <span class="bd-ll">${coords}</span>` +
    `<a class="bd-gmaps" href="${gmaps}" target="_blank" rel="noopener">` +
    `Open in Google&nbsp;Maps ↗</a></div>`;
  // A dropped pin has no score — just name it, see nearby campgrounds, save it.
  if (!hasScore) {
    return (
      nameBar + coordsHtml +
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
    coordsHtml +
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

init();
