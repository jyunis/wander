"use strict";

// --- state -----------------------------------------------------------------
let CONFIG = null;
let map, drawnItems, heatLayer, pinLayer;
let bbox = null; // {min_lon,min_lat,max_lon,max_lat}
let activeRunId = null;

const FEATURE_LABELS = {
  flatness: "Flatness",
  water_proximity: "Water",
  tree_cover: "Trees",
  solitude: "Solitude",
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
  setupMap();
  await loadRuns();

  $("draw-bbox").onclick = startDraw;
  $("use-view").onclick = useCurrentView;
  $("score-btn").onclick = runScore;
  $("run-history").onchange = (e) => {
    if (e.target.value) loadRun(parseInt(e.target.value, 10));
  };
  $("bd-close").onclick = () => $("breakdown").classList.add("hidden");
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
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17,
    attribution:
      'Map © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA), ' +
      'data © OpenStreetMap contributors',
  }).addTo(map);

  drawnItems = new L.FeatureGroup().addTo(map);
  pinLayer = new L.LayerGroup().addTo(map);

  map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    setBboxFromBounds(e.layer.getBounds());
  });
}

let drawHandler = null;
function startDraw() {
  if (drawHandler) drawHandler.disable();
  drawHandler = new L.Draw.Rectangle(map, { shapeOptions: { color: "#5b9c5a" } });
  drawHandler.enable();
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
      setStatus(`Done — ${run.candidate_count} candidates.`, "ok");
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
  $("provenance").innerHTML = run.provenance
    ? `OSM ${osm.from_cache ? "cached" : "fetched"} ${(osm.fetched_at || "").slice(0, 10)} · ` +
      `elevation z${(elev.covering || {}).zoom ?? "?"}${run.capped ? " · capped" : ""}`
    : "";
  const deg = run.degraded_features || [];
  $("degraded").textContent = deg.length
    ? "⚠ degraded (scored on the rest): " + deg.map((d) => FEATURE_LABELS[d] || d).join(", ")
    : "";
}

function renderResults(gj) {
  if (heatLayer) map.removeLayer(heatLayer);
  pinLayer.clearLayers();
  const feats = gj.features || [];
  if (!feats.length) return;

  const heat = feats.map((f) => [
    f.geometry.coordinates[1],
    f.geometry.coordinates[0],
    Math.max(0.05, f.properties.total),
  ]);
  heatLayer = L.heatLayer(heat, { radius: 22, blur: 16, max: 1.0 }).addTo(map);

  feats.slice(0, 25).forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({ className: "", html: `<div class="rank-pin">${p.rank}</div>`, iconSize: [22, 22] }),
    });
    marker.on("click", () => showBreakdown(p));
    pinLayer.addLayer(marker);
  });

  const b = gj.run.bbox;
  map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [20, 20] });
}

function showBreakdown(p) {
  const body = $("bd-body");
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
  const raw =
    `slope ${fmt(p.slope_deg, "°")} · water ${fmt(p.dist_water_m, " m")} · ` +
    `forest ${fmt(p.dist_forest_m, " m")} · solitude ${fmt(p.dist_solitude_m, " m")}` +
    (p.near_campsite ? " · near a known campsite" : "");
  body.innerHTML =
    rows +
    `<div class="bd-total">Total: ${p.total.toFixed(3)} (rank #${p.rank})</div>` +
    `<div class="bd-raw">${raw}</div>` +
    `<div class="bd-raw">${CONFIG.disclaimer}</div>`;
  $("breakdown").classList.remove("hidden");
}

function fmt(v, unit) {
  return v === null || v === undefined ? "—" : Math.round(v) + unit;
}

init();
