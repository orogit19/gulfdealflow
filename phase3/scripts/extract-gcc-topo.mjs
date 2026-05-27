// One-shot extractor: read world-atlas/countries-50m.json, keep only the 6
// GCC countries by ISO numeric id, prune the unused topology arcs, and write
// to src/data/gcc-topo.json. Run once after `npm install world-atlas`; the
// output is committed and bundled.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const GCC_IDS = new Set(["048", "414", "512", "634", "682", "784"]);

const src = JSON.parse(
  readFileSync(`${__dirname}/../node_modules/world-atlas/countries-50m.json`, "utf-8")
);

const obj = src.objects.countries;
const kept = obj.geometries.filter((g) => GCC_IDS.has(String(g.id)));

// Collect all arc indices referenced by the kept geometries. In TopoJSON,
// arc indices are signed (~i means reverse direction); the actual index is
// (idx >= 0 ? idx : ~idx). Geometry "arcs" structure depends on type:
//   Polygon: [[a, b, ...], [hole arcs...], ...]
//   MultiPolygon: [Polygon, Polygon, ...]
const used = new Set();
function visit(node) {
  if (typeof node === "number") used.add(node >= 0 ? node : ~node);
  else if (Array.isArray(node)) node.forEach(visit);
}
kept.forEach((g) => visit(g.arcs));

// Build remap: old index → new index, preserving order.
const sorted = [...used].sort((a, b) => a - b);
const remap = new Map(sorted.map((oldIdx, newIdx) => [oldIdx, newIdx]));
const newArcs = sorted.map((i) => src.arcs[i]);

function remapArcs(node) {
  if (typeof node === "number") {
    const orig = node >= 0 ? node : ~node;
    const next = remap.get(orig);
    return node >= 0 ? next : ~next;
  }
  return node.map(remapArcs);
}

const remappedGeometries = kept.map((g) => ({
  ...g,
  arcs: remapArcs(g.arcs),
}));

const out = {
  type: src.type,
  bbox: src.bbox,
  transform: src.transform,
  arcs: newArcs,
  objects: { countries: { ...obj, geometries: remappedGeometries } },
};

const destDir = `${__dirname}/../src/data`;
mkdirSync(destDir, { recursive: true });
const destPath = `${destDir}/gcc-topo.json`;
writeFileSync(destPath, JSON.stringify(out));

const beforeKB = (JSON.stringify(src).length / 1024).toFixed(0);
const afterKB = (JSON.stringify(out).length / 1024).toFixed(0);
console.log(`Extracted ${kept.length} countries, ${newArcs.length}/${src.arcs.length} arcs → ${destPath}`);
console.log(`Size: ${beforeKB} KB → ${afterKB} KB`);
