"use strict";

// --- state -----------------------------------------------------------------
let CONFIG = null;
let map, drawnItems, heatLayer, pinLayer, candidatesLayer, roadsLayer, canvasRenderer, gotoMarker;
let nearbyCgLayer; // ephemeral markers: the campgrounds near the open scored site
let tripLayer; // markers for the currently plotted trip's members
let runExtentLayer; // dashed rectangle showing the loaded run's scored bbox
let savedLayer; // saved-place markers shown on the Saved tab (collection-colored)
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
// Satellite is the default: in the backcountry the imagery (+ Cairn's own roads
// overlay) reads better than a street map, and it's where the vector roads earn
// their keep. Switch the menu's default by changing this id.
const DEFAULT_BASEMAP = "satellite";
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
  national_park: "#7b4ea3",
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
// Roads are a zoomed-in detail; below this we don't refetch (a wide viewport
// would pull the whole region). Keeps general browsing cheap.
const ROADS_MIN_ZOOM = 11;

let overlayDefs = []; // from GET /overlays
let mvumOverlays = []; // from GET /mvum: [{key, name, bounds:[s,w,n,e], url}]
const mvumLayers = {}; // key -> L.imageOverlay
let mvumOpacity = 0.75;
const overlayLayers = {}; // id -> L.GeoJSON
const overlayActive = new Set();
const overlayTruncated = new Set(); // layers whose last fetch hit the cap

// Land layers whose polygons are tap-to-discover: tap one -> highlight the whole
// named shape (all disjoint parts) + zoom to fit. Ownership parcels are excluded
// (their "name" is a county, which would union the whole county).
const SHAPE_LAYERS = new Set(["national_forest", "state_forest", "state_park"]);
// Discovery fit: keep the WHOLE shape in view (so the part you tapped never
// scrolls off), but framed tight — small padding + a maxZoom so a small park
// doesn't slam to street level.
const SHAPE_FIT_PADDING = 12;
const SHAPE_FIT_MAX_ZOOM = 13;
let shapeSelectLayer = null; // L.GeoJSON highlight of the selected forest/park
let selectedShape = null; // {layer, name} currently highlighted, or null

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
  await initMvum();
  scheduleRoads(); // roads default on — load them for the opening view

  $("draw-bbox").onclick = startDraw;
  $("use-view").onclick = useCurrentView;
  $("goto-btn").onclick = gotoCoords;
  $("goto-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") gotoCoords();
  });
  $("drop-pin").onclick = toggleDropPin;
  $("score-btn").onclick = runScore;
  // (run selection is now a clickable list — see renderRunsList)
  ["layer-pins", "layer-dots", "layer-heat", "layer-roads", "layer-saved"].forEach((id) => {
    $(id).onchange = updateLayerVisibility;
  });
  $("saved-layer-coll").onchange = renderSavedMarkers; // re-filter the saved layer
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
  $("sheet-handle").onclick = () => $("sheet").classList.toggle("collapsed");
  // Sidebar: a left column on desktop (collapsible), an overlay drawer on
  // mobile (closed by default). One state class on #app; CSS interprets it
  // per viewport.
  setNav(!window.matchMedia("(max-width: 640px)").matches);
  // Reopening the drawer also bails out of an in-progress draw (mobile closes
  // the drawer to draw). Leaflet.Draw, while active, adds a document-level
  // touchstart preventDefault that cancels the click on iOS — so stop the fab's
  // touchstart from reaching that handler, keeping the fab tappable mid-draw.
  $("nav-open").onclick = () => {
    cancelDraw();
    setNav(true);
  };
  $("nav-open").addEventListener("touchstart", (e) => e.stopPropagation(), { passive: true });
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
  loadCollections(); // populate the Layers-panel saved-collection dropdown
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

// On mobile the panel is an overlay drawer over the map; on desktop it's a
// side column. Some flows (e.g. drawing a bbox) need the map reachable.
function isMobile() {
  return window.matchMedia("(max-width: 640px)").matches;
}

function setupMap() {
  // Zoom on the right so the floating menu button can live top-left.
  map = L.map("map", { zoomControl: false }).setView([46.3, -94.3], 7);
  L.control.zoom({ position: "topright" }).addTo(map);
  addBasemapControl(); // small on-map style picker (synced with the panel select)
  addMeasureControl(); // 📏 distance ruler (independent of the scoring tools)
  setBasemap(DEFAULT_BASEMAP);
  // Candidate-dot canvas: its OWN pane BELOW the land pane. The canvas spans the
  // whole viewport and captures pointer events across it, so if it sat above the
  // land overlays (Leaflet's default overlayPane, z400) it would swallow every
  // tap meant for a forest/park polygon once a run had been scored — and stay
  // there until reload. Below land (z230 < 250), forest polygons hit-test on top
  // again; the numbered result pins live in the marker pane (z600) so their
  // breakdowns are unaffected.
  map.createPane("candidates");
  map.getPane("candidates").style.zIndex = 230;
  // Land overlays sit above the basemap + candidate dots, below roads/pins.
  map.createPane("land");
  map.getPane("land").style.zIndex = 250;
  // MVUM raster overlay sits just above the land fills (so its symbols read)
  // but below the highlight/roads/pins; opacity is user-controlled.
  map.createPane("mvum");
  map.getPane("mvum").style.zIndex = 255;
  // The discovery highlight sits just above the land fills but takes no pointer
  // events, so tapping a highlighted shape still selects whatever land polygon
  // is underneath (lets you jump straight to an adjacent forest/park).
  map.createPane("shapeHighlight");
  map.getPane("shapeHighlight").style.zIndex = 260;
  map.getPane("shapeHighlight").style.pointerEvents = "none";
  map.on("moveend", refreshActiveOverlays);
  map.on("moveend", scheduleRoads); // viewport roads overlay follows the map
  map.on("moveend", updateShapeScoreBtn); // re-evaluate the boundary-score cap gate
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
  // Canvas renderer so thousands of candidate dots stay smooth. In the
  // "candidates" pane (below land) so the canvas can't swallow forest taps.
  canvasRenderer = L.canvas({ padding: 0.5, pane: "candidates" });
  // roadsLayer holds Cairn's own drivable ways (OSM ∪ MVUM) as vector lines —
  // on by default (the "Roads" checkbox is checked), since satellite imagery
  // shows no forest roads. updateLayerVisibility adds it once a run loads.
  roadsLayer = new L.LayerGroup();
  candidatesLayer = new L.LayerGroup().addTo(map);
  pinLayer = new L.LayerGroup().addTo(map);
  nearbyCgLayer = new L.LayerGroup().addTo(map);
  tripLayer = new L.LayerGroup().addTo(map);
  savedLayer = new L.LayerGroup().addTo(map);
  runExtentLayer = new L.LayerGroup().addTo(map);
  measureLayer = new L.LayerGroup().addTo(map); // the distance ruler (custom)

  map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    setBboxFromBounds(e.layer.getBounds());
    // Mobile: the panel was closed to let the user draw — bring it back so the
    // bbox readout + Score button are reachable again.
    if (isMobile()) setNav(true);
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
  sel.value = DEFAULT_BASEMAP;
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
  syncBasemapUI(def.id);
}

// Keep both basemap pickers — the panel <select> and the on-map control — in
// step with the active basemap, whichever one changed it.
function syncBasemapUI(id) {
  const sel = $("basemap");
  if (sel) sel.value = id;
  const ctrl = document.querySelector(".basemap-ctrl");
  if (ctrl) {
    const def = BASEMAPS.find((b) => b.id === id);
    const toggle = ctrl.querySelector(".basemap-toggle");
    if (toggle && def) toggle.title = "Basemap: " + def.name; // the only label (icon-only button)
    ctrl.querySelectorAll(".basemap-opt").forEach((o) =>
      o.classList.toggle("active", o.dataset.id === id)
    );
  }
}

// A compact, expandable basemap-style switcher docked on the map (top-right,
// under the zoom control). Reuses BASEMAPS + setBasemap so it shares one source
// of truth with the panel select.
function addBasemapControl() {
  const ctrl = L.control({ position: "topright" });
  ctrl.onAdd = () => {
    const div = L.DomUtil.create("div", "basemap-ctrl leaflet-control");
    const opts = BASEMAPS.map(
      (b) => `<button type="button" class="basemap-opt" data-id="${b.id}">${esc(b.name)}</button>`
    ).join("");
    // Collapsed = a single small icon button (same footprint as a zoom button,
    // so it stacks cleanly beneath it and doesn't eat mobile screen). The style
    // list is an absolutely-positioned dropdown that only overlays the map when
    // open — closed, it takes zero layout space.
    div.innerHTML =
      `<button type="button" class="basemap-toggle" title="Basemap style" aria-label="Basemap style">▦</button>` +
      `<div class="basemap-list" hidden>${opts}</div>`;
    // Keep clicks/scroll on the control from reaching the map underneath.
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    const list = div.querySelector(".basemap-list");
    const toggle = div.querySelector(".basemap-toggle");
    const setOpen = (open) => {
      list.hidden = !open;
      toggle.classList.toggle("open", open);
    };
    toggle.onclick = () => setOpen(list.hidden);
    div.querySelectorAll(".basemap-opt").forEach((o) => {
      o.onclick = () => {
        setBasemap(o.dataset.id);
        setOpen(false);
      };
    });
    map.on("click", () => setOpen(false)); // a tap on the map collapses it
    return div;
  };
  ctrl.addTo(map);
}

let drawHandler = null;
function cancelDraw() {
  if (drawHandler) {
    drawHandler.disable();
    drawHandler = null;
  }
}
function startDraw() {
  finishMeasure(); // finish any in-progress ruler (tools are mutually exclusive)
  if (drawHandler) drawHandler.disable();
  drawHandler = new L.Draw.Rectangle(map, { shapeOptions: { color: "#5b9c5a" } });
  drawHandler.enable();
  // Mobile: the panel overlays the whole map, so close it — otherwise there's
  // no map to draw on and the drawer looks frozen. Leaflet's own draw tooltip
  // guides the drag; the panel reopens automatically once the box is drawn.
  if (isMobile()) setNav(false);
}

// --- measure tool (distance ruler) -----------------------------------------
// A standalone ruler, independent of the scoring selection. Tap 📏, then tap the
// map to drop points; the chip shows the running total. Tap "Done" (or the 📏
// button, or Enter) to finish, ✕ to clear. A plain tap-to-add handler — NOT
// Leaflet.draw — so adding many segments works reliably on mobile (Leaflet.draw's
// only finish gesture is a double-tap, which on touch ends the line after one
// segment). Great-circle segment distances (map.distance), not road routing.
let measureLayer = null; // the ruler polyline + vertex dots
let measuring = false; // actively dropping points
let measurePts = []; // L.LatLng[] of dropped points

const $MEASURE_BTN = () => document.querySelector(".measure-toggle");

function addMeasureControl() {
  const ctrl = L.control({ position: "topright" });
  ctrl.onAdd = () => {
    const div = L.DomUtil.create("div", "measure-ctrl leaflet-control");
    div.innerHTML =
      `<button type="button" class="measure-toggle" title="Measure distance" ` +
      `aria-label="Measure distance" aria-pressed="false">📏</button>`;
    L.DomEvent.disableClickPropagation(div);
    div.querySelector(".measure-toggle").onclick = toggleMeasure;
    return div;
  };
  ctrl.addTo(map);
}

function setMeasureBtnActive(on) {
  const btn = $MEASURE_BTN();
  if (btn) {
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

function toggleMeasure() {
  if (measuring) finishMeasure(); // tapping the active tool finishes
  else startMeasure();
}

function startMeasure() {
  cancelDraw(); // tools are mutually exclusive
  cancelDropPin();
  clearMeasure(); // a new measurement replaces any prior one
  measuring = true;
  measurePts = [];
  setMeasureBtnActive(true);
  if (!measureLayer) measureLayer = new L.LayerGroup().addTo(map);
  map.getContainer().style.cursor = "crosshair";
  // Keep double-click from zooming mid-measure. We deliberately do NOT finish on
  // double-click: on touch, two quick taps read as a double-tap and would end the
  // line after one segment. Finishing is explicit (Done button / Enter / 📏).
  map.doubleClickZoom.disable();
  map.on("click", onMeasureClick);
  document.addEventListener("keydown", onMeasureKey);
  if (isMobile()) setNav(false);
  renderMeasureChip(0, { prompt: true });
}

function onMeasureClick(e) {
  measurePts.push(e.latlng);
  redrawMeasure();
  renderMeasureChip(totalMeters(measurePts), { live: true });
}

function redrawMeasure() {
  measureLayer.clearLayers();
  if (!measurePts.length) return;
  // interactive:false so the ruler's own line/vertices never swallow a tap meant
  // to drop the next point.
  measureLayer.addLayer(
    L.polyline(measurePts, { color: "#2bb3ff", weight: 3, interactive: false })
  );
  measurePts.forEach((ll) =>
    measureLayer.addLayer(
      L.circleMarker(ll, {
        radius: 4, color: "#2bb3ff", weight: 2, fillColor: "#fff", fillOpacity: 1,
        interactive: false,
      })
    )
  );
}

function finishMeasure() {
  if (!measuring) return;
  stopMeasureInput();
  if (measurePts.length < 2) {
    clearMeasure(); // a lone point isn't a measurement
    return;
  }
  renderMeasureChip(totalMeters(measurePts), { final: true });
}

// Detach the map/keyboard listeners + restore map behavior, keeping any drawn
// line in place. Shared by finish and clear.
function stopMeasureInput() {
  measuring = false;
  setMeasureBtnActive(false);
  map.getContainer().style.cursor = "";
  if (map.doubleClickZoom) map.doubleClickZoom.enable();
  map.off("click", onMeasureClick);
  document.removeEventListener("keydown", onMeasureKey);
}

function onMeasureKey(e) {
  if (e.key === "Enter") finishMeasure();
  else if (e.key === "Escape") clearMeasure();
}

function clearMeasure() {
  stopMeasureInput();
  measurePts = [];
  if (measureLayer) measureLayer.clearLayers();
  hideMeasureChip();
}

function totalMeters(latlngs) {
  let m = 0;
  for (let i = 1; i < latlngs.length; i++) m += map.distance(latlngs[i - 1], latlngs[i]);
  return m;
}

// Whole miles + feet (never a fractional mile); just feet under a mile.
function formatDistance(meters) {
  const totalFt = Math.round(meters * 3.28084);
  if (totalFt < 5280) return `${totalFt} ft`;
  const miles = Math.floor(totalFt / 5280);
  const ft = totalFt - miles * 5280;
  return ft ? `${miles} mi ${ft} ft` : `${miles} mi`;
}

// States: prompt (no points yet), live (dropping points — show Done), final
// (finished). The ✕ clears in every state.
function renderMeasureChip(meters, { prompt = false, live = false, final = false } = {}) {
  const chip = $("measure-chip");
  if (!chip) return;
  chip.hidden = false;
  const label = prompt ? "Tap the map to measure" : `📏 ${formatDistance(meters)}`;
  chip.innerHTML =
    `<span class="measure-chip-text">${label}</span>` +
    (live ? `<button type="button" class="measure-chip-done">Done</button>` : "") +
    `<button type="button" class="measure-chip-x" title="Clear measurement" ` +
    `aria-label="Clear measurement">✕</button>`;
  const done = chip.querySelector(".measure-chip-done");
  if (done) done.onclick = finishMeasure;
  chip.querySelector(".measure-chip-x").onclick = clearMeasure;
}

function hideMeasureChip() {
  const chip = $("measure-chip");
  if (chip) {
    chip.hidden = true;
    chip.innerHTML = "";
  }
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
  finishMeasure(); // finish any in-progress ruler (tools are mutually exclusive)
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
  await executeScore(bbox, {});
}

// Shared score driver for both the drawn-bbox "Score" button and the
// "Score within boundary" chip button. `opts.clip` ({layer, name}) restricts
// candidates to a forest/park polygon; `opts.button` is the triggering element
// to disable while the run is in flight (defaults to the draw Score button).
async function executeScore(bb, opts = {}) {
  if (areaKm2(bb) > CONFIG.max_bbox_area_km2) {
    // client-side mirror of TLC-4; the API is still the authority.
    setStatus("Area exceeds the cap — draw a smaller box.", "err");
    return;
  }
  const btn = opts.button || $("score-btn");
  if (btn) btn.disabled = true;
  setStatus(opts.clip ? "Scoring within boundary…" : "Scoring…");
  try {
    const body = {
      bbox: [bb.min_lon, bb.min_lat, bb.max_lon, bb.max_lat],
      weights: readWeights(),
    };
    if (opts.clip) body.clip = opts.clip;
    if ($("public-only")?.checked) body.public_only = true;
    const resp = await fetch("/score", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (resp.status === 422) {
      const err = await resp.json();
      setStatus(err.detail || "Invalid request", "err");
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
    if (btn) btn.disabled = false;
  }
}

let runsList = []; // GET /runs — recent run summaries, newest first

async function loadRuns() {
  try {
    runsList = (await (await fetch("/runs?limit=25")).json()).runs || [];
  } catch {
    runsList = [];
  }
  renderRunsList();
}

// "just now / 2h ago / 3d ago / Jun 3" — created_at is UTC ISO without a zone.
function relativeDate(iso) {
  if (!iso) return "";
  const t = Date.parse(/[Z+]/.test(iso) ? iso : iso + "Z");
  if (isNaN(t)) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// A run's display label: its place name when set, else its bbox-center coords.
function runLabel(r) {
  const b = r.bbox; // [min_lon, min_lat, max_lon, max_lat]
  if (r.place) return r.place;
  return `${((b[1] + b[3]) / 2).toFixed(3)}, ${((b[0] + b[2]) / 2).toFixed(3)}`;
}

function runAreaKm2(r) {
  const b = r.bbox;
  return areaKm2({ min_lon: b[0], min_lat: b[1], max_lon: b[2], max_lat: b[3] });
}

function renderRunsList() {
  const host = $("run-list");
  if (!host) return;
  $("runs-empty").hidden = runsList.length > 0;
  host.innerHTML = runsList
    .map((r) => {
      const sites = r.distinct_count != null ? r.distinct_count : 0;
      const area = Math.round(runAreaKm2(r)).toLocaleString();
      const meta =
        `${sites} site${sites === 1 ? "" : "s"} · ${area} km² · ${relativeDate(r.created_at)}`;
      const pill =
        r.status !== "done"
          ? ` <span class="run-pill run-${esc(r.status)}">${esc(r.status)}</span>`
          : "";
      return (
        `<div class="run-row${r.id === activeRunId ? " active" : ""}" data-id="${r.id}">` +
        `<div class="run-row-main"><div class="run-row-name">${esc(runLabel(r))}${pill}</div>` +
        `<div class="run-row-sub">${meta}</div></div>` +
        `<button class="run-rename" title="Rename run">✎</button>` +
        `<button class="run-del" title="Delete run">×</button></div>`
      );
    })
    .join("");
  host.querySelectorAll(".run-row").forEach((row) => {
    const id = parseInt(row.dataset.id, 10);
    row.querySelector(".run-row-main").onclick = () => {
      if (isMobile()) setNav(false); // mobile: close the drawer so the map shows
      loadRun(id);
    };
    row.querySelector(".run-rename").onclick = (e) => {
      e.stopPropagation();
      renameRun(id);
    };
    row.querySelector(".run-del").onclick = (e) => {
      e.stopPropagation();
      deleteRun(id);
    };
  });
}

async function renameRun(id) {
  const r = runsList.find((x) => x.id === id);
  const name = prompt("Rename run", r ? runLabel(r) : "");
  if (name == null) return; // cancelled
  const trimmed = name.trim();
  if (!trimmed) return;
  try {
    const resp = await fetch(`/runs/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      setStatus(err.detail || "Rename failed", "err");
      return;
    }
    await loadRuns(); // re-render the list with the new label
  } catch (e) {
    setStatus("Error: " + e.message, "err");
  }
}

async function loadRun(runId) {
  exitTripPlot(); // loading a run leaves the trip-plot view
  activeRunId = runId;
  const gj = await (await fetch(`/runs/${runId}/geojson`)).json();
  renderResults(gj); // fitBounds here fires moveend -> scheduleRoads() fetches the area
  renderMeta(gj.run);
  drawRunExtent(gj.run.bbox);
  renderRunsList(); // highlight the active row
  renderRunChip(); // on-map "viewing run" indicator + exit affordance
}

// Leave run-view: pull the scored results + extent off the map and reset state.
// The exit path the run-chip ✕ uses; also reused when the active run is deleted.
function clearRun() {
  activeRunId = null;
  lastGj = null;
  pinLayer.clearLayers();
  candidatesLayer.clearLayers();
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  runExtentLayer.clearLayers();
  nearbyCgLayer.clearLayers();
  closeSite();
  $("provenance").innerHTML = "";
  $("degraded").innerHTML = "";
  renderRunChip();
  renderRunsList();
}

// The on-map chip naming the run whose results are shown, with an ✕ to exit.
function renderRunChip() {
  const chip = $("run-chip");
  if (!chip) return;
  if (activeRunId == null) {
    chip.hidden = true;
    chip.innerHTML = "";
    return;
  }
  const r = runsList.find((x) => x.id === activeRunId);
  const name = r ? runLabel(r) : "Run #" + activeRunId;
  chip.hidden = false;
  chip.innerHTML =
    `<span class="run-chip-text">📊 ${esc(name)}</span>` +
    `<button type="button" class="run-chip-x" title="Exit run view" aria-label="Exit run view">✕</button>`;
  chip.querySelector(".run-chip-x").onclick = clearRun;
}

// A dashed rectangle showing the run's scored extent. b = [minlon,minlat,maxlon,maxlat].
function drawRunExtent(b) {
  if (!runExtentLayer) return;
  runExtentLayer.clearLayers();
  if (!b) return;
  L.rectangle([[b[1], b[0]], [b[3], b[2]]], {
    color: "#5b9c5a",
    weight: 1.5,
    dashArray: "6 4",
    fill: false,
    interactive: false,
  }).addTo(runExtentLayer);
}

async function deleteRun(id) {
  const r = runsList.find((x) => x.id === id);
  if (!confirm(`Delete run "${r ? runLabel(r) : "#" + id}"? This can't be undone.`)) return;
  await fetch(`/runs/${id}`, { method: "DELETE" });
  if (id === activeRunId) clearRun(); // the active run's results are on the map
  await loadRuns();
}

// Cairn's own road network (OSM ∪ MVUM forest spurs) as vector lines — far
// better than the satellite basemap's transportation overlay, which is blank in
// the forest and drops linework above z18. Viewport-driven (GET /roads?bbox=):
// the overlay follows wherever you browse, for any area a prior run ingested,
// not just the loaded run's box. Best-effort: a fetch failure leaves it empty.
let _roadsBounds = null; // padded LatLngBounds last fetched (skip refetch within)
let _roadsTimer = null;

function scheduleRoads() {
  clearTimeout(_roadsTimer);
  _roadsTimer = setTimeout(loadRoadsForView, 300);
}

async function loadRoadsForView() {
  if (!$("layer-roads").checked) return; // layer off — nothing to fetch
  if (map.getZoom() < ROADS_MIN_ZOOM) return; // too far out — keep what's drawn
  const view = map.getBounds();
  // Already have this area (we fetch a padded box, so small pans don't refetch).
  // Re-fetch if the layer was cleared out from under us (e.g. trip-plot view).
  if (_roadsBounds && _roadsBounds.contains(view) && roadsLayer.getLayers().length) return;
  const padded = view.pad(0.4);
  const bbox = [padded.getWest(), padded.getSouth(), padded.getEast(), padded.getNorth()].join(",");
  let gj;
  try {
    gj = await (await fetch(`/roads?bbox=${bbox}`)).json();
  } catch {
    return;
  }
  if (!$("layer-roads").checked) return; // toggled off mid-fetch
  renderRoads(gj);
  _roadsBounds = padded;
}

// Parse an MVUM access string ("Roads open to all Vehicles, Seasonal") into a
// readable class + season + a "special designation" flag (worth a closer look).
function mvumInfo(access) {
  if (!access) return null;
  const season = /seasonal/i.test(access)
    ? "Seasonal"
    : /yearlong/i.test(access)
      ? "Yearlong"
      : null;
  let klass = access;
  let special = false;
  if (/all vehicles/i.test(access)) klass = "Open to all vehicles";
  else if (/highway legal/i.test(access)) klass = "Highway-legal vehicles only";
  else if (/special/i.test(access)) {
    klass = "Special designation";
    special = true;
  }
  return { klass, season, special };
}

function renderRoads(gj) {
  roadsLayer.clearLayers();
  (gj.features || []).forEach((f) => {
    const p = f.properties || {};
    const forest = p.forest_road;
    const mv = mvumInfo(p.mvum_access);
    // Color: special-designation MVUM roads stand out (purple); other forest/
    // MVUM roads orange; non-MVUM drivable yellow. Rough → dashed (unchanged).
    const color = mv && mv.special ? "#9b59b6" : forest ? "#ff7a00" : "#ffd23d";
    const line = L.geoJSON(f, {
      style: {
        color,
        weight: forest ? 3 : 2,
        opacity: 0.9,
        dashArray: p.rough ? "5 4" : null,
        lineCap: "round",
      },
    });
    const label = p.ref ? `FR ${p.ref}` : p.name || "Road";
    line.bindTooltip(label + (mv && mv.season ? ` · ${mv.season}` : ""), { sticky: true });
    line.bindPopup(roadPopup(p, mv, label), { maxWidth: 260 });
    roadsLayer.addLayer(line);
  });
}

function roadPopup(p, mv, label) {
  const rows = [];
  if (p.name && p.ref && p.name !== label) rows.push(esc(p.name));
  if (mv) {
    rows.push(esc(mv.klass) + (mv.season ? ` · <b>${esc(mv.season)}</b>` : ""));
  }
  if (p.surface) rows.push("surface: " + esc(p.surface));
  if (p.rough) rows.push("⚠ rough / high-clearance");
  // The MVUM tells you a road is open to DRIVING; dispersed-camping designation
  // (corridor + dates) lives only on the MVUM/Avenza — nudge the user there.
  const note = mv
    ? `<div class="road-pop-note">Open to <i>driving</i> as shown. Confirm ` +
      `<b>dispersed camping</b> (corridor + dates) on the MVUM / Avenza.</div>`
    : "";
  return (
    `<div class="road-pop"><b>${esc(label)}</b>` +
    (rows.length ? "<br>" + rows.join("<br>") : "") +
    note +
    "</div>"
  );
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
    dot.on("click", () => {
      if (measuring) return; // a tap mid-measure drops a ruler point, not a site
      showSite(p, lat, lon);
    });
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
  if ($("layer-roads").checked) scheduleRoads(); // fetch the view when turned on
  renderSavedMarkers(); // saved layer (clears itself when its checkbox is off)
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

// --- MVUM raster overlays (display-only district map sheets) ---------------
// Region-gated: the toggle only shows when the map view is over a district that
// has an MVUM raster (and zoomed in enough to read it) — out of sight in MN etc.
const MVUM_MIN_ZOOM = 9;
let _mvumShownKeys = null; // signature of the currently-listed overlays

async function initMvum() {
  let data;
  try {
    data = await (await fetch("/mvum")).json();
  } catch {
    return;
  }
  mvumOverlays = data.overlays || [];
  if (!mvumOverlays.length) return;
  map.on("moveend", updateMvumPanel);
  updateMvumPanel();
}

function updateMvumPanel() {
  const host = $("mvum-layers");
  if (!host || !mvumOverlays.length) return;
  const view = map.getBounds();
  const inView =
    map.getZoom() >= MVUM_MIN_ZOOM
      ? mvumOverlays.filter((o) =>
          L.latLngBounds([o.bounds[0], o.bounds[1]], [o.bounds[2], o.bounds[3]]).intersects(view)
        )
      : [];
  const sig = inView.map((o) => o.key).join(",");
  if (sig === _mvumShownKeys) return; // visible set unchanged — leave the DOM be
  _mvumShownKeys = sig;
  if (!inView.length) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  host.innerHTML =
    inView
      .map(
        (o) =>
          `<label><input type="checkbox" data-mvum="${esc(o.key)}"` +
          `${mvumLayers[o.key] ? " checked" : ""}> 🗺 ${esc(o.name)}</label>`
      )
      .join("") +
    `<label class="mvum-opacity">opacity ` +
    `<input type="range" id="mvum-opacity" min="0.2" max="1" step="0.05" value="${mvumOpacity}"></label>`;
  host.querySelectorAll("input[data-mvum]").forEach((cb) => {
    cb.onchange = (e) => toggleMvum(e.target.dataset.mvum, e.target.checked);
  });
  $("mvum-opacity").oninput = (e) => {
    mvumOpacity = parseFloat(e.target.value);
    Object.values(mvumLayers).forEach((l) => l.setOpacity(mvumOpacity));
  };
}

function toggleMvum(key, on) {
  if (!on) {
    if (mvumLayers[key]) {
      map.removeLayer(mvumLayers[key]);
      delete mvumLayers[key];
    }
    return;
  }
  const o = mvumOverlays.find((x) => x.key === key);
  if (!o || mvumLayers[key]) return;
  const [s, w, n, e] = o.bounds;
  mvumLayers[key] = L.imageOverlay(o.url, [[s, w], [n, e]], {
    opacity: mvumOpacity,
    pane: "mvum",
    interactive: false,
  }).addTo(map);
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
        // Tap a forest/park polygon -> highlight the whole named shape + fit.
        // Skip while measuring so the tap drops a ruler point instead (Leaflet
        // still bubbles the click to the map handler that adds the point).
        if (SHAPE_LAYERS.has(layerId) && p.name) {
          lyr.on("click", () => {
            if (measuring) return;
            selectShape(layerId, p.name);
          });
        }
      }
    },
  });
  overlayLayers[layerId] = gjLayer.addTo(map);
  if (layerId === "campground") {
    campgroundGj = gj;
    updateCgCount(gj);
  }
}

// --- forest/park shape discovery -------------------------------------------
// Tap a forest/park -> fetch its WHOLE outline (the overlay tiles only carry the
// viewport slice) -> highlight every disjoint part and fit-zoom to all of them.
async function selectShape(layer, name) {
  let shape;
  try {
    const r = await fetch(`/overlays/${layer}/shape?name=${encodeURIComponent(name)}`);
    if (!r.ok) return; // 404 (gone) / inert overlays -> silently no-op
    shape = await r.json();
  } catch {
    return;
  }
  if (shapeSelectLayer) map.removeLayer(shapeSelectLayer);
  shapeSelectLayer = L.geoJSON(shape, {
    pane: "shapeHighlight",
    interactive: false,
    style: {
      color: "#ffd23f",
      weight: 3,
      opacity: 0.95,
      fillColor: "#ffd23f",
      fillOpacity: 0.12,
      className: "shape-pulse", // one-shot CSS pulse on (re)select
    },
  }).addTo(map);
  selectedShape = { layer, name };
  renderShapeChip();
  const bb = shape.bbox; // [w, s, e, n] from the full geometry
  if (bb && bb.every((v) => typeof v === "number")) {
    map.fitBounds(
      [
        [bb[1], bb[0]],
        [bb[3], bb[2]],
      ],
      { padding: [SHAPE_FIT_PADDING, SHAPE_FIT_PADDING], maxZoom: SHAPE_FIT_MAX_ZOOM }
    );
  }
}

function clearShape() {
  if (shapeSelectLayer) {
    map.removeLayer(shapeSelectLayer);
    shapeSelectLayer = null;
  }
  selectedShape = null;
  renderShapeChip();
}

// The on-map chip: "<name>  [Score within boundary]  ✕" — shows the current
// selection, scores the current view clipped to it, or clears it.
let _shapeScoring = false; // a within-boundary run is in flight
function renderShapeChip() {
  const chip = $("shape-chip");
  if (!chip) return;
  if (!selectedShape) {
    chip.hidden = true;
    chip.innerHTML = "";
    return;
  }
  chip.hidden = false;
  chip.innerHTML =
    `<span class="shape-chip-name">${esc(selectedShape.name)}</span>` +
    `<button type="button" class="shape-chip-score btn">Score within boundary</button>` +
    `<button type="button" class="shape-chip-x" title="Clear selection" aria-label="Clear selection">✕</button>`;
  chip.querySelector(".shape-chip-x").onclick = clearShape;
  chip.querySelector(".shape-chip-score").onclick = scoreWithinBoundary;
  updateShapeScoreBtn();
}

// Cap-gate the within-boundary button exactly like the draw flow: the SEARCH
// area is the current view (bbox-capped as usual); only the polygon clip is new.
// A forest bigger than the cap stays scoreable once you zoom to a section.
function updateShapeScoreBtn() {
  const btn = document.querySelector(".shape-chip-score");
  if (!btn || !CONFIG) return;
  const b = map.getBounds();
  const a = areaKm2({
    min_lon: b.getWest(),
    min_lat: b.getSouth(),
    max_lon: b.getEast(),
    max_lat: b.getNorth(),
  });
  const ok = a <= CONFIG.max_bbox_area_km2;
  btn.textContent = _shapeScoring ? "Scoring…" : "Score within boundary";
  btn.disabled = _shapeScoring || !ok;
  btn.classList.toggle("over-cap", !ok);
  btn.title = ok
    ? "Score the current view, clipped to this boundary"
    : `View is ${a.toFixed(0)} km² — over the ${CONFIG.max_bbox_area_km2} km² cap. Zoom to a section.`;
}

async function scoreWithinBoundary() {
  if (!selectedShape || _shapeScoring) return;
  const b = map.getBounds();
  const view = {
    min_lon: b.getWest(),
    min_lat: b.getSouth(),
    max_lon: b.getEast(),
    max_lat: b.getNorth(),
  };
  if (areaKm2(view) > CONFIG.max_bbox_area_km2) return; // cap-gated (button disabled)
  _shapeScoring = true;
  updateShapeScoreBtn();
  try {
    await executeScore(view, {
      clip: { layer: selectedShape.layer, name: selectedShape.name },
      button: document.querySelector(".shape-chip-score"),
    });
  } finally {
    _shapeScoring = false;
    updateShapeScoreBtn();
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
  // South Dakota state-park sites (OSM-sourced) reserve via SD GFP.
  if ((p.meta || {}).state === "SD") {
    return { href: "https://gfp.sd.gov/reservations/", label: "Reserve · SD GFP" };
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
  $("sheet").classList.remove("hidden", "collapsed"); // a new selection opens expanded
  $("sheet-peek").textContent =
    (p && p.name) || presetName || `${(+lat).toFixed(4)}, ${(+lon).toFixed(4)}`;
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
  $("sheet").classList.remove("collapsed"); // reset for next open
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

// Collections (multi-membership tags over saved places — TLC-3, display-only).
let collections = []; // GET /collections — [{id,name,color,position,count}]
let collectionsLoaded = false;
const collById = new Map();
const COLLECTION_PALETTE = [
  "#5b9c5a", "#3a6ea5", "#b5651d", "#8e6bc9",
  "#c8a24a", "#d96c6c", "#4aa3a3", "#9c5a8e",
];
const SAVED_MARKER_DEFAULT = "#f3c64b"; // gold (★ theme) when no collection filter
const UNFILED = "__unfiled__"; // filter token for places with no collection
// Active collection filters (ids + UNFILED). Empty = show all. The filter bar
// doubles as the map legend: a matching place lights up in its collection color.
const activeCollFilters = new Set();

// Multi-select bulk-add: a "Select" mode over the saved list -> "Add N to …".
let selectMode = false;
const selectedIds = new Set();

function placePassesFilter(place) {
  if (!activeCollFilters.size) return true;
  const ids = place.collection_ids || [];
  if (activeCollFilters.has(UNFILED) && ids.length === 0) return true;
  return ids.some((id) => activeCollFilters.has(id));
}

// --- Saved-places map layer (toggle + collection dropdown in the Layers panel),
// independent of the Saved tab. The chip bar filters the LIST; this filters the
// MAP layer.
function savedLayerColl() {
  const sel = $("saved-layer-coll");
  return sel ? sel.value || "all" : "all";
}

function savedPassesLayerFilter(place) {
  const v = savedLayerColl();
  if (v === "all") return true;
  const ids = place.collection_ids || [];
  if (v === "unfiled") return ids.length === 0;
  return ids.includes(parseInt(v, 10));
}

// Marker color: a selected collection -> its color; "All"/"Unfiled" -> the
// place's first collection color, gold if uncategorized.
function savedLayerMarkerColor(place) {
  const v = savedLayerColl();
  if (v !== "all" && v !== "unfiled") {
    const c = collById.get(parseInt(v, 10));
    return (c && c.color) || SAVED_MARKER_DEFAULT;
  }
  const ids = place.collection_ids || [];
  for (const c of collections) if (ids.includes(c.id)) return c.color || SAVED_MARKER_DEFAULT;
  return SAVED_MARKER_DEFAULT;
}

// Keep the Layers-panel collection dropdown in sync with the collection set.
function populateSavedLayerColl() {
  const sel = $("saved-layer-coll");
  if (!sel) return;
  const cur = sel.value || "all";
  sel.innerHTML =
    `<option value="all">All collections</option>` +
    collections.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("") +
    `<option value="unfiled">Unfiled</option>`;
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

// The collection chip bar — a filter AND the on-map color legend.
function renderCollFilterBar() {
  const host = $("coll-filter-bar");
  if (!host) return;
  const unfiledCount = savedPlaces.filter((s) => !(s.collection_ids || []).length).length;
  const chips = collections
    .map((c) => {
      const on = activeCollFilters.has(c.id);
      const col = c.color || "#8a97a3";
      return (
        `<button class="cf-chip${on ? " on" : ""}" data-id="${c.id}" style="--cf:${esc(col)}">` +
        `<span class="cf-dot"></span>${esc(c.name)} <span class="cf-n">${c.count}</span></button>`
      );
    })
    .join("");
  const unfiled =
    `<button class="cf-chip cf-unfiled${activeCollFilters.has(UNFILED) ? " on" : ""}" ` +
    `data-id="${UNFILED}" style="--cf:#8a97a3"><span class="cf-dot"></span>` +
    `Unfiled <span class="cf-n">${unfiledCount}</span></button>`;
  host.innerHTML = collections.length || unfiledCount ? chips + unfiled : "";
  host.querySelectorAll(".cf-chip").forEach((b) => {
    b.onclick = () =>
      toggleCollFilter(b.dataset.id === UNFILED ? UNFILED : parseInt(b.dataset.id, 10));
  });
}

function toggleCollFilter(key) {
  if (activeCollFilters.has(key)) activeCollFilters.delete(key);
  else activeCollFilters.add(key);
  renderCollFilterBar();
  renderSavedList(); // also repaints markers
}

// Saved-place markers on the map, filtered + collection-colored. Only shown
// while the Saved tab is active (cleared otherwise).
function renderSavedMarkers() {
  if (!savedLayer) return;
  savedLayer.clearLayers();
  if (!$("layer-saved") || !$("layer-saved").checked) return; // layer off
  savedPlaces.filter(savedPassesLayerFilter).forEach((s) => {
    // A ★ favorite marker (matching the selected-site star), tinted by the
    // place's collection so the grouping reads at a glance.
    const mk = L.marker([s.lat, s.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div class="saved-star" style="--c:${savedLayerMarkerColor(s)}">★</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }),
    });
    if (s.name) mk.bindTooltip(s.name, { direction: "top" });
    mk.on("click", () => reopenSaved(s));
    savedLayer.addLayer(mk);
  });
}

async function loadCollections() {
  try {
    collections = (await (await fetch("/collections")).json()).collections || [];
  } catch {
    collections = [];
  }
  collectionsLoaded = true;
  collById.clear();
  collections.forEach((c) => collById.set(c.id, c));
  populateSavedLayerColl(); // keep the Layers-panel dropdown in sync
}

async function createCollection(name, color) {
  color = color || COLLECTION_PALETTE[collections.length % COLLECTION_PALETTE.length];
  const r = await fetch("/collections", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, color }),
  });
  if (!r.ok) return null;
  const col = await r.json();
  await loadCollections();
  return col;
}
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

// Colored membership chips for a saved place (looked up against collById).
function collChipsHtml(place) {
  const ids = place.collection_ids || [];
  const chips = ids
    .map((id) => {
      const c = collById.get(id);
      if (!c) return "";
      const col = c.color || "#8a97a3";
      return `<span class="coll-chip" style="border-color:${esc(col)};background:${esc(col)}22">${esc(c.name)}</span>`;
    })
    .join("");
  return chips ? `<div class="coll-chips">${chips}</div>` : "";
}

function renderSavedList() {
  const host = $("saved-list");
  if (!host) return;
  const empty = $("saved-empty");
  if (empty) empty.hidden = savedPlaces.length > 0;
  const shown = savedPlaces.filter(placePassesFilter);
  host.innerHTML = shown
    .map((s) => {
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
      const sel = selectMode && selectedIds.has(s.id);
      return (
        `<div class="saved-row${sel ? " selected" : ""}" data-id="${s.id}">` +
        `<input type="checkbox" class="saved-check"${sel ? " checked" : ""} tabindex="-1">` +
        `<div class="saved-main">` +
        `<div class="saved-name">${icon} ${esc(label)}</div>` +
        `<div class="saved-coords">${coords}${score}${stars}</div>` +
        noteLine +
        collChipsHtml(s) +
        `</div>` +
        `<button class="saved-tag" title="Collections">🏷</button>` +
        `<button class="saved-add" title="Add to a trip">＋</button>` +
        `<button class="saved-del" title="Remove">×</button></div>`
      );
    })
    .join("");
  host.classList.toggle("select-mode", selectMode);
  host.querySelectorAll(".saved-row").forEach((row) => {
    const s = savedPlaces.find((x) => x.id === parseInt(row.dataset.id, 10));
    if (!s) return;
    if (selectMode) {
      row.onclick = () => toggleSelected(s.id); // whole row toggles selection
      return;
    }
    row.querySelector(".saved-main").onclick = () => reopenSaved(s);
    row.querySelector(".saved-tag").onclick = (e) => {
      e.stopPropagation();
      openCollectionPicker(s, e.currentTarget);
    };
    row.querySelector(".saved-add").onclick = (e) => {
      e.stopPropagation();
      openTripPickerAt(s, e.currentTarget);
    };
    row.querySelector(".saved-del").onclick = (e) => {
      e.stopPropagation();
      toggleFav(s);
    };
  });
  renderSavedMarkers();
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
// Phase 2 availability (display-only — TLC-3). Fetched lazily per trip so the
// detail renders instantly; a fetch failure just drops the badges (degradable).
let tripAvail = {}; // saved_id(str) -> badge {system,status,open_sites,total_sites,summary}
let tripAvailLoading = false;

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      if (tab.classList.contains("active")) return;
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const which = tab.dataset.tab;
      $("score-section").hidden = which !== "score";
      $("trips-section").hidden = which !== "trips";
      $("saved-section-tab").hidden = which !== "saved";
      if (which === "trips") loadTrips();
      else exitTripPlot(); // back to Score/Saved: restore the normal map
      if (which === "saved") openSavedTab();
    };
  });
  buildCollSwatches();
  $("coll-new").onclick = () => toggleCollForm(true);
  $("coll-new-cancel").onclick = () => toggleCollForm(false);
  $("coll-new-form").onsubmit = (e) => {
    e.preventDefault();
    createCollectionFromForm();
  };
  $("saved-select").onclick = () => toggleSelectMode(!selectMode);
  $("ssb-add").onclick = bulkAddSelected;
  $("ssb-cancel").onclick = () => toggleSelectMode(false);
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
  tripAvail = {};
  $("trips-list-view").hidden = true;
  $("trip-detail").hidden = false;
  renderTripDetail();
  loadTripAvailability(id); // lazy: paints badges when it returns
}

// Fetch live availability for the open trip's campground members and repaint.
// Best-effort: any failure (offline, source down) leaves the trip working with
// no badges (RIDB/MVUM posture). Guards against the user navigating away mid-flight.
async function loadTripAvailability(tripId) {
  const hasCg = (activeTrip?.members || []).some((m) => m.kind === "campground");
  if (!hasCg) {
    tripAvail = {};
    tripAvailLoading = false;
    return;
  }
  tripAvail = {};
  tripAvailLoading = true;
  renderTripMembers(); // show "checking…" on campground rows
  try {
    const r = await fetch(`/trips/${tripId}/availability`);
    if (!r.ok) throw new Error(r.status);
    const body = await r.json();
    if (!activeTrip || activeTrip.id !== tripId) return; // navigated away
    tripAvail = body.members || {};
  } catch (e) {
    tripAvail = {}; // degrade to no badges
  } finally {
    if (activeTrip && activeTrip.id === tripId) {
      tripAvailLoading = false;
      renderTripMembers();
    }
  }
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
    `<a href="${esc(gmaps)}" target="_blank" rel="noopener">Maps ↗</a></div>` +
    availBadgeHtml(m) +
    `</div>` +
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

// The availability annotation under a campground member row. Federal -> open/
// full count; non-federal -> a link-out to the reservation system; no dates ->
// a nudge; source error / still-loading -> muted. Non-campgrounds get nothing.
function availBadgeHtml(m) {
  if (m.kind !== "campground") return "";
  if (tripAvailLoading) return `<div class="trip-avail checking">checking availability…</div>`;
  const a = tripAvail[String(m.saved_id)];
  if (!a) return "";
  if (a.status === "ok") {
    const cls = a.open_sites > 0 ? "open" : "full";
    const mark = a.open_sites > 0 ? "🟢 " : "🚫 ";
    return `<div class="trip-avail ${cls}">${mark}${esc(a.summary)}</div>`;
  }
  if (a.status === "first_come") {
    return `<div class="trip-avail firstcome">⛺ First-come, first-served</div>`;
  }
  if (a.status === "no_dates") {
    return `<div class="trip-avail muted">Set trip dates to check availability</div>`;
  }
  if (a.status === "unsupported") {
    const { href, label } = campgroundLink(m.payload || {});
    return (
      `<div class="trip-avail link">` +
      `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(label)} ↗</a></div>`
    );
  }
  return `<div class="trip-avail muted">Availability unavailable</div>`; // error
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
  loadTripAvailability(activeTrip.id); // membership changed -> re-check (cache makes this cheap)
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
  loadTripAvailability(activeTrip.id); // dates may have changed -> re-check
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
  clearShape(); // drop any forest/park discovery highlight
  clearMeasure(); // drop any distance ruler
  if ($("run-chip")) $("run-chip").hidden = true; // hide (activeRunId kept for restore)
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
  renderRunChip(); // restore the "viewing run" chip if a run is still active
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

// --- Saved tab + collections ------------------------------------------------
async function openSavedTab() {
  if (selectMode) toggleSelectMode(false); // start fresh each visit
  await loadCollections();
  renderCollFilterBar();
  await loadSaved(); // renderSavedList reads collById for the chips + paints markers
}

let _collFormColor = COLLECTION_PALETTE[0];
function buildCollSwatches() {
  const host = $("coll-swatches");
  if (!host) return;
  host.innerHTML = COLLECTION_PALETTE.map(
    (c, i) =>
      `<button type="button" class="coll-swatch${i === 0 ? " sel" : ""}" ` +
      `data-c="${c}" style="background:${c}" aria-label="color"></button>`
  ).join("");
  host.querySelectorAll(".coll-swatch").forEach((b) => {
    b.onclick = () => {
      _collFormColor = b.dataset.c;
      host.querySelectorAll(".coll-swatch").forEach((x) => x.classList.toggle("sel", x === b));
    };
  });
}

function toggleCollForm(show) {
  $("coll-new-form").hidden = !show;
  if (!show) return;
  // Default to the next palette color so new collections get distinct colors
  // (and the map layer's collection-coloring is useful out of the box).
  const def = COLLECTION_PALETTE[collections.length % COLLECTION_PALETTE.length];
  _collFormColor = def;
  const host = $("coll-swatches");
  if (host) host.querySelectorAll(".coll-swatch").forEach((x) => x.classList.toggle("sel", x.dataset.c === def));
  $("coll-name").focus();
}

async function createCollectionFromForm() {
  const name = $("coll-name").value.trim();
  const err = $("coll-new-err");
  if (!name) {
    err.textContent = "Name the collection.";
    err.className = "status err";
    return;
  }
  const col = await createCollection(name, _collFormColor);
  if (!col) {
    err.textContent = "Couldn't create the collection.";
    err.className = "status err";
    return;
  }
  $("coll-name").value = "";
  err.textContent = "";
  err.className = "status";
  toggleCollForm(false);
  renderCollFilterBar(); // surface the new collection as a filter chip
  renderSavedList();
}

// The per-place tag picker — a floating popover (document-level so a Leaflet
// popup never clips it), modeled on the trip picker. Toggle collections on/off,
// create a new one inline, or delete one. Stays open while you toggle several.
let _collPicker = null;
let _collEditing = null; // collection id being renamed/recolored inline, or null

// Wire the inline name/color editor (one row of the picker, when _collEditing set).
function wireCollEditor(pick, place) {
  const editor = pick.querySelector(".coll-pick-editor");
  if (!editor) return;
  editor.querySelectorAll(".cpe-sw").forEach((b) => {
    b.onclick = () =>
      editor.querySelectorAll(".cpe-sw").forEach((x) => x.classList.toggle("sel", x === b));
  });
  editor.querySelector(".cpe-cancel").onclick = () => {
    _collEditing = null;
    renderCollPickerBody(pick, place);
  };
  const save = editor.querySelector(".cpe-save");
  save.onclick = async () => {
    const name = editor.querySelector(".cpe-name").value.trim();
    const color = editor.querySelector(".cpe-sw.sel") ? editor.querySelector(".cpe-sw.sel").dataset.c : undefined;
    if (!name) {
      editor.querySelector(".cpe-name").focus();
      return;
    }
    const r = await fetch(`/collections/${editor.dataset.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, color }),
    });
    if (!r.ok) {
      pick.querySelector(".coll-pick-err").textContent = "Couldn't save.";
      return;
    }
    await loadCollections();
    _collEditing = null;
    renderCollFilterBar();
    renderSavedList(); // refresh chips + recolor the map layer
    renderCollPickerBody(pick, place);
  };
  editor.querySelector(".cpe-name").onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      save.click();
    }
  };
}
function closeCollPicker() {
  if (_collPicker) {
    _collPicker.remove();
    _collPicker = null;
    document.removeEventListener("click", _onCollPickerOutside, true);
    if (map) map.off("movestart", closeCollPicker);
  }
}
function _onCollPickerOutside(e) {
  if (_collPicker && !_collPicker.contains(e.target)) closeCollPicker();
}

async function openCollectionPicker(place, anchorEl) {
  const same = _collPicker && _collPicker._for === place.id;
  closeCollPicker();
  if (same) return; // a second click toggles it closed
  if (!collectionsLoaded) await loadCollections();
  const pick = document.createElement("div");
  pick.className = "coll-picker floating";
  pick._for = place.id;
  _collEditing = null; // start unedited
  document.body.appendChild(pick);
  _collPicker = pick;
  renderCollPickerBody(pick, place);
  positionPicker(pick, anchorEl);
  setTimeout(() => document.addEventListener("click", _onCollPickerOutside, true), 0);
  if (map) map.on("movestart", closeCollPicker);
}

function collEditorHtml(c) {
  const sw = COLLECTION_PALETTE.map(
    (p) =>
      `<button type="button" class="cpe-sw${p === c.color ? " sel" : ""}" ` +
      `data-c="${p}" style="background:${p}"></button>`
  ).join("");
  return (
    `<div class="coll-pick-editor" data-id="${c.id}">` +
    `<input class="cpe-name" type="text" value="${esc(c.name)}" autocomplete="off" />` +
    `<div class="cpe-swatches">${sw}</div>` +
    `<div class="cpe-actions"><button class="cpe-save btn">Save</button>` +
    `<button class="cpe-cancel btn ghost">Cancel</button></div></div>`
  );
}

function renderCollPickerBody(pick, place) {
  const ids = new Set(place.collection_ids || []);
  const rows = collections
    .map((c) => {
      if (c.id === _collEditing) return collEditorHtml(c);
      const on = ids.has(c.id);
      const col = c.color || "#8a97a3";
      return (
        `<div class="coll-pick-row${on ? " on" : ""}" data-id="${c.id}">` +
        `<span class="coll-dot" style="background:${esc(col)}"></span>` +
        `<span class="coll-pick-name">${esc(c.name)}</span>` +
        `<span class="coll-pick-check">${on ? "✓" : ""}</span>` +
        `<button class="coll-pick-edit" title="Edit name / color">✎</button>` +
        `<button class="coll-pick-del" title="Delete collection">×</button></div>`
      );
    })
    .join("");
  const title = place.name || (place.kind === "campground" ? "Campground" : "Place");
  pick.innerHTML =
    `<div class="coll-pick-head"><span class="coll-pick-title">${esc(title)}</span>` +
    `<button class="coll-pick-close" title="Done">×</button></div>` +
    `<div class="coll-pick-list">${
      rows || '<div class="coll-pick-empty">No collections yet.</div>'
    }</div>` +
    `<div class="coll-pick-new">` +
    `<input class="coll-pick-input" type="text" placeholder="＋ New collection" autocomplete="off" />` +
    `<button class="coll-pick-add btn">Add</button></div>` +
    `<div class="coll-pick-err"></div>`;
  pick.querySelector(".coll-pick-close").onclick = closeCollPicker;
  pick.querySelectorAll(".coll-pick-row").forEach((row) => {
    const id = parseInt(row.dataset.id, 10);
    row.onclick = (e) => {
      if (e.target.closest(".coll-pick-del") || e.target.closest(".coll-pick-edit")) return;
      toggleMembership(place, id);
    };
    row.querySelector(".coll-pick-del").onclick = (e) => {
      e.stopPropagation();
      deleteCollection(id, place);
    };
    row.querySelector(".coll-pick-edit").onclick = (e) => {
      e.stopPropagation();
      _collEditing = id;
      renderCollPickerBody(pick, place);
    };
  });
  wireCollEditor(pick, place);
  const input = pick.querySelector(".coll-pick-input");
  const add = pick.querySelector(".coll-pick-add");
  add.onclick = async () => {
    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    const col = await createCollection(name);
    if (!col) {
      pick.querySelector(".coll-pick-err").textContent = "Couldn't create.";
      return;
    }
    input.value = "";
    await toggleMembership(place, col.id); // tag the place with the new collection
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add.click();
    }
  };
}

async function toggleMembership(place, collId) {
  const ids = new Set(place.collection_ids || []);
  if (ids.has(collId)) ids.delete(collId);
  else ids.add(collId);
  const r = await fetch(`/saved/${place.id}/collections`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ collection_ids: [...ids] }),
  });
  if (!r.ok) return;
  const updated = await r.json();
  place.collection_ids = updated.collection_ids;
  const i = savedPlaces.findIndex((s) => s.id === place.id);
  if (i >= 0) savedPlaces[i].collection_ids = updated.collection_ids;
  await loadCollections(); // member counts changed
  renderCollFilterBar();
  renderSavedList();
  if (_collPicker && _collPicker._for === place.id) renderCollPickerBody(_collPicker, place);
}

async function deleteCollection(id, place) {
  const c = collById.get(id);
  if (!confirm(`Delete collection "${c ? c.name : ""}"? Your saved places aren't deleted.`)) return;
  await fetch(`/collections/${id}`, { method: "DELETE" });
  await loadCollections();
  activeCollFilters.delete(id); // drop it from any active filter
  // Drop the deleted id from any cached place membership.
  savedPlaces.forEach((s) => {
    if (s.collection_ids) s.collection_ids = s.collection_ids.filter((x) => x !== id);
  });
  renderCollFilterBar();
  renderSavedList();
  if (_collPicker && place) renderCollPickerBody(_collPicker, place);
}

// --- multi-select bulk-add --------------------------------------------------
function toggleSelectMode(on) {
  selectMode = on;
  selectedIds.clear();
  const btn = $("saved-select");
  if (btn) btn.textContent = on ? "Cancel" : "Select";
  $("saved-select-bar").hidden = !on;
  if (on) populateSelectColl();
  updateSelectCount();
  renderSavedList();
}

function toggleSelected(id) {
  if (selectedIds.has(id)) selectedIds.delete(id);
  else selectedIds.add(id);
  const row = document.querySelector(`#saved-list .saved-row[data-id="${id}"]`);
  if (row) {
    const on = selectedIds.has(id);
    row.classList.toggle("selected", on);
    const cb = row.querySelector(".saved-check");
    if (cb) cb.checked = on;
  }
  updateSelectCount();
}

function updateSelectCount() {
  const n = selectedIds.size;
  const c = $("ssb-count");
  if (c) c.textContent = `${n} selected`;
  const add = $("ssb-add");
  if (add) add.disabled = n === 0;
}

function populateSelectColl() {
  const sel = $("ssb-coll");
  if (!sel) return;
  sel.innerHTML =
    collections.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("") +
    `<option value="__new__">＋ New collection…</option>`;
}

async function bulkAddSelected() {
  if (!selectedIds.size) return;
  const sel = $("ssb-coll");
  let cid;
  if (sel.value === "__new__") {
    const name = (prompt("New collection name") || "").trim();
    if (!name) return;
    const col = await createCollection(name);
    if (!col) return;
    cid = col.id;
  } else {
    cid = parseInt(sel.value, 10);
  }
  const count = selectedIds.size;
  const r = await fetch(`/collections/${cid}/places`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ saved_ids: [...selectedIds] }),
  });
  if (!r.ok) return;
  await loadCollections();
  await loadSaved(); // re-hydrate collection_ids on the tagged places
  renderCollFilterBar();
  const c = collById.get(cid);
  tripToast(`Added ${count} to ${c ? c.name : "collection"}`);
  toggleSelectMode(false);
}

init();
