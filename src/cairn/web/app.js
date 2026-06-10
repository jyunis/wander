"use strict";

// --- state -----------------------------------------------------------------
let CONFIG = null;
let map, drawnItems, heatLayer, pinLayer, candidatesLayer, canvasRenderer, gotoMarker;
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

  $("draw-bbox").onclick = startDraw;
  $("use-view").onclick = useCurrentView;
  $("goto-btn").onclick = gotoCoords;
  $("goto-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") gotoCoords();
  });
  $("score-btn").onclick = runScore;
  $("run-history").onchange = (e) => {
    if (e.target.value) loadRun(parseInt(e.target.value, 10));
  };
  $("bd-close").onclick = () => $("breakdown").classList.add("hidden");
  ["layer-pins", "layer-dots", "layer-heat"].forEach((id) => {
    $(id).onchange = updateLayerVisibility;
  });
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

function setupMap() {
  map = L.map("map").setView([47.9, -91.86], 11);
  setBasemap(BASEMAPS[0].id);

  drawnItems = new L.FeatureGroup().addTo(map);
  // Canvas renderer so thousands of candidate dots stay smooth.
  canvasRenderer = L.canvas({ padding: 0.5 });
  candidatesLayer = new L.LayerGroup().addTo(map);
  pinLayer = new L.LayerGroup().addTo(map);

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

function gotoCoords() {
  const input = $("goto-input");
  const ll = parseLatLon(input.value);
  if (!ll) {
    input.classList.add("bad");
    return;
  }
  input.classList.remove("bad");
  const [lat, lon] = ll;
  map.setView([lat, lon], 15);
  if (gotoMarker) map.removeLayer(gotoMarker);
  gotoMarker = L.circleMarker([lat, lon], {
    radius: 10,
    color: "#22d3ee",
    weight: 3,
    fill: false,
  })
    .addTo(map)
    .bindPopup(`${lat.toFixed(6)}, ${lon.toFixed(6)}`)
    .openPopup();
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
    dot.on("click", () => showBreakdown(p, lat, lon));
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
    marker.on("click", () => showBreakdown(p, lat, lon));
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
}

function toggleLayer(layer, on) {
  if (!layer) return;
  if (on && !map.hasLayer(layer)) map.addLayer(layer);
  else if (!on && map.hasLayer(layer)) map.removeLayer(layer);
}

function showBreakdown(p, lat, lon) {
  const body = $("bd-body");
  const coords = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
  const coordsHtml =
    `<div class="bd-coords">📍 <span class="bd-ll">${coords}</span>` +
    `<a class="bd-gmaps" href="${gmaps}" target="_blank" rel="noopener">` +
    `Open in Google&nbsp;Maps ↗</a></div>`;
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
  body.innerHTML =
    coordsHtml +
    rows +
    `<div class="bd-total">Total: ${p.total.toFixed(3)} (rank #${p.rank})${rough}</div>` +
    `<div class="bd-raw">${raw}</div>` +
    `<div class="bd-raw">${CONFIG.disclaimer}</div>`;
  $("breakdown").classList.remove("hidden");
}

function fmt(v, unit) {
  return v === null || v === undefined ? "—" : Math.round(v) + unit;
}

init();
