# Map label glyphs (offline)

`Noto Sans Medium/0-255.pbf` — the Latin-1 glyph range MapLibre needs to draw
road labels, as signed-distance-field PBF. Vendored (not fetched) because the
field map must draw labels with no signal; the service worker precaches it.

- Source: https://github.com/protomaps/basemaps-assets, `fonts/Noto Sans Medium/0-255.pbf`
  at commit `028c18f713baecad011301ff7a69acc39bcc2ae7`
- sha256 `ba2f0118dd024e3041b158e5f9eb49bc0a658019f53f458e9f5c0b8efcd79b91`
- Font: Noto Sans (Google), SIL Open Font License 1.1.

Other ranges (accented Latin Extended etc.) are deliberately omitted: US road
names are Latin-1. A label needing a missing range just doesn't draw.
