import { useEffect, useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, Legend,
} from "recharts";
import { ComposableMap, Geographies, Geography, Marker } from "react-simple-maps";
import { geoCentroid } from "d3-geo";
import gccTopo from "./data/gcc-topo.json";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const COUNTRIES = ["UAE", "Saudi Arabia", "Kuwait", "Bahrain", "Oman", "Qatar"];
const STAGES = [
  "Pre-Seed", "Seed", "Series A", "Series B", "Series C+", "Growth", "Undisclosed",
];
const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020];
const PAGE_SIZE_OPTIONS = [20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

// Subtle per-stage badge palette: light tinted text + ~50% opacity border +
// barely-there tinted background. Each class name is a full string literal so
// Tailwind's JIT picks them up at build time.
const STAGE_STYLES = {
  "Pre-Seed":    "text-purple-300 border-purple-700/50 bg-purple-950/40",
  "Seed":        "text-blue-300 border-blue-700/50 bg-blue-950/40",
  "Series A":    "text-cyan-300 border-cyan-700/50 bg-cyan-950/40",
  "Series B":    "text-emerald-300 border-emerald-700/50 bg-emerald-950/40",
  "Series C+":   "text-amber-300 border-amber-700/50 bg-amber-950/40",
  "Growth":      "text-orange-300 border-orange-700/50 bg-orange-950/40",
  "Undisclosed": "text-gdf-muted border-gdf-border bg-gdf-surface",
};

function stageBadgeClass(stage) {
  return STAGE_STYLES[stage] || "text-gdf-muted border-gdf-border bg-gdf-surface";
}

function formatAmount(usd) {
  if (usd == null) return "—";
  if (usd >= 1_000_000_000) return `$${(usd / 1_000_000_000).toFixed(2)}B`;
  if (usd >= 1_000_000)     return `$${(usd / 1_000_000).toFixed(1)}M`;
  if (usd >= 1_000)         return `$${(usd / 1_000).toFixed(0)}K`;
  return `$${usd}`;
}

function formatCapitalB(usd) {
  if (usd == null) return "—";
  return `$${(usd / 1_000_000_000).toFixed(2)}B`;
}

/* ---------- Sector normalisation (display layer only) ---------- */

// The DB stores granular sub-sector strings like
// "Fintech / digital wallets / payments platform". For charts, filters, and
// tooltips we collapse these to a small fixed set of top-level categories.
// Raw data in Supabase is never modified — this is purely UI-side.
const SECTOR_CATEGORIES = [
  "Fintech", "E-commerce", "Logistics", "Healthtech", "Edtech",
  "Deep Tech / AI", "B2B SaaS", "Foodtech", "Real Estate Tech",
  "Gaming", "Media", "Other",
];

// Lookup of (lowercased lead-segment) → canonical category. The lead segment
// is everything before the first "/" in the raw sector string. Anything not
// matched here falls through to "Other".
const SECTOR_ALIASES = {
  // Fintech & money
  "fintech": "Fintech",
  "payments": "Fintech",
  "payment": "Fintech",
  "wallet": "Fintech",
  "wallets": "Fintech",
  "banking": "Fintech",
  "digital banking": "Fintech",
  "insurtech": "Fintech",
  "lending": "Fintech",
  "bnpl": "Fintech",
  "crypto": "Fintech",
  "wealth": "Fintech",
  "wealthtech": "Fintech",
  "wealth management": "Fintech",
  "savings": "Fintech",
  "spend management": "Fintech",
  "tap-to-pay": "Fintech",
  "pos": "Fintech",
  // E-commerce
  "e-commerce": "E-commerce",
  "ecommerce": "E-commerce",
  "e-commerce & retail": "E-commerce",
  "retail": "E-commerce",
  "re-commerce marketplace": "E-commerce",
  "marketplace": "E-commerce",
  "quick commerce": "E-commerce",
  "eyewear": "E-commerce",
  // Logistics & mobility
  "logistics": "Logistics",
  "logistics & supply chain": "Logistics",
  "supply chain": "Logistics",
  "mobility": "Logistics",
  "delivery": "Logistics",
  // Health
  "healthtech": "Healthtech",
  "health": "Healthtech",
  "healthcare": "Healthtech",
  // Edu / talent
  "edtech": "Edtech",
  "education": "Edtech",
  "talent platform": "Edtech",
  "recruitment marketplace": "Edtech",
  // Deep tech / AI
  "deep tech": "Deep Tech / AI",
  "deep tech & ai": "Deep Tech / AI",
  "deeptech": "Deep Tech / AI",
  "ai": "Deep Tech / AI",
  "additive manufacturing": "Deep Tech / AI",
  "web3": "Deep Tech / AI",
  "blockchain": "Deep Tech / AI",
  "hardware": "Deep Tech / AI",
  "cloud": "Deep Tech / AI",
  // B2B SaaS
  "b2b saas": "B2B SaaS",
  "saas": "B2B SaaS",
  "saas & enterprise software": "B2B SaaS",
  "enterprise software": "B2B SaaS",
  "hrtech": "B2B SaaS",
  "sportstech": "B2B SaaS",
  "contech": "B2B SaaS",
  "device management": "B2B SaaS",
  // Food / agri
  "foodtech": "Foodtech",
  "food": "Foodtech",
  "food & agritech": "Foodtech",
  "agritech": "Foodtech",
  "cloud kitchen": "Foodtech",
  "restaurant tech": "Foodtech",
  "fooh": "Foodtech",  // typo seen in source CSV
  // Real estate
  "real estate tech": "Real Estate Tech",
  "proptech": "Real Estate Tech",
  "property": "Real Estate Tech",
  // Gaming / media
  "gaming": "Gaming",
  "media": "Media",
  "media & entertainment": "Media",
  "entertainment": "Media",
};

function normalizeSector(raw) {
  if (!raw) return "Other";
  const lead = String(raw).split("/")[0].trim().toLowerCase();
  return SECTOR_ALIASES[lead] || "Other";
}

/* ---------- Stage normalisation (display layer only) ---------- */

// Raw `stage` values in the DB include "Venture investment / seed-like round",
// "Funding round (stage not specified in snippet)", etc. The chart only needs
// canonical funding stages in chronological order; everything else is folded
// into "Other" rather than cluttering the axis.
const STAGE_ORDER_CANONICAL = [
  "Pre-Seed", "Seed", "Pre-Series A", "Series A", "Series B",
  "Series C+", "Growth", "Other",
];

const STAGE_ALIASES = {
  // Pre-Seed
  "pre-seed":                                  "Pre-Seed",
  "preseed":                                   "Pre-Seed",
  "pre seed":                                  "Pre-Seed",
  "first investment round":                    "Pre-Seed",
  // Seed (incl. extensions / debt-flavoured rounds / verbose paraphrases)
  "seed":                                      "Seed",
  "seed extension":                            "Seed",
  "seed equity + debt":                        "Seed",
  "venture investment / seed-like round":      "Seed",
  // Pre-Series A (Bridges typically fund the gap pre-A)
  "pre-series a":                              "Pre-Series A",
  "pre series a":                              "Pre-Series A",
  "bridge":                                    "Pre-Series A",
  // Series A
  "series a":                                  "Series A",
  // Series B family (incl. first close, A→B extensions, sloppy "Pre-Series B")
  "series b":                                  "Series B",
  "series b (first close)":                    "Series B",
  "series b / a extension":                    "Series B",
  "pre-series b":                              "Series B",
  // Series C+
  "series c":                                  "Series C+",
  "series c+":                                 "Series C+",
  "series d":                                  "Series C+",
  "series e":                                  "Series C+",
  // Growth / late-stage
  "growth":                                    "Growth",
  "growth / late-stage":                       "Growth",
  "growth / late-stage round":                 "Growth",
  "pre-ipo / growth":                          "Growth",
  "growth / strategic investment (non-labeled vc round)": "Growth",
  "strategic funding / growth":                "Growth",
  // Everything else — ambiguous "Funding round" / "Equity round" / etc.
  "unknown":                                   "Other",
  "undisclosed":                               "Other",
  "venture round":                             "Other",
  "equity round":                              "Other",
  "funding round":                             "Other",
  "funding round (stage not specified in snippet)":          "Other",
  "funding round (stage not explicitly clear in snippet)":   "Other",
  "extension/venture round":                   "Other",
};

function normalizeStage(raw) {
  if (!raw) return "Other";
  return STAGE_ALIASES[String(raw).trim().toLowerCase()] || "Other";
}

// Collapse the raw by_stage breakdown into canonical buckets in chronological
// order. Empty buckets are kept so the axis spacing stays consistent across
// data reloads — readers expect to see Pre-Seed → Growth even when one bucket
// happens to be zero.
function aggregateStagesForChart(by_stage) {
  const counts = Object.fromEntries(STAGE_ORDER_CANONICAL.map((s) => [s, 0]));
  for (const item of by_stage || []) {
    const key = normalizeStage(item.key);
    counts[key] = (counts[key] || 0) + (item.deal_count || 0);
  }
  return STAGE_ORDER_CANONICAL.map((key) => ({ key, deal_count: counts[key] }));
}

// Roll a raw `by_sector` breakdown into canonical buckets, then enforce
// display caps: anything below `minPct` of total (default 2%) or beyond the
// `max`-th slice is folded into the "Other" bucket so the donut chart never
// fragments into a kaleidoscope. Returns at most `max` slices.
function aggregateSectorsForChart(by_sector, { max = 12, minPct = 0.02 } = {}) {
  const buckets = new Map();
  let total = 0;
  for (const item of by_sector || []) {
    const key = normalizeSector(item.key);
    const prev = buckets.get(key) || { key, deal_count: 0, total_capital_usd: 0 };
    prev.deal_count += item.deal_count || 0;
    prev.total_capital_usd += item.total_capital_usd || 0;
    buckets.set(key, prev);
    total += item.deal_count || 0;
  }
  const sorted = [...buckets.values()].sort((a, b) => b.deal_count - a.deal_count);

  const cutoff = total * minPct;
  const keep = [];
  let otherCount = 0;
  let otherCapital = 0;
  for (const item of sorted) {
    const shouldFold =
      item.key === "Other" ||
      item.deal_count < cutoff ||
      keep.length >= max - 1;
    if (shouldFold) {
      otherCount += item.deal_count;
      otherCapital += item.total_capital_usd;
    } else {
      keep.push(item);
    }
  }
  if (otherCount > 0) {
    keep.push({ key: "Other", deal_count: otherCount, total_capital_usd: otherCapital });
  }
  return keep;
}

/* ---------- CSV export ---------- */

// Full schema, in the order columns will appear in the exported file.
// Includes fields not visible in the table (description, co_investors,
// founded_year, website, source, notes) per the export spec.
const CSV_COLUMNS = [
  "deal_id", "company_name", "country", "city", "date", "stage",
  "amount_usd", "disclosed", "sector", "description", "founded_year",
  "website", "lead_investor", "co_investors", "investor_types",
  "source", "notes",
];

function escapeCsvField(v) {
  if (v == null) return "";
  const s = String(v);
  // RFC 4180: wrap in quotes if the field contains comma, quote, CR, or LF.
  // Inside a quoted field, double-quotes are escaped by doubling.
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function dealsToCsv(deals) {
  const header = CSV_COLUMNS.join(",");
  const rows = deals.map((d) =>
    CSV_COLUMNS.map((c) => escapeCsvField(d[c])).join(",")
  );
  return [header, ...rows].join("\r\n");
}

function downloadDealsCsv(deals) {
  // Prepend the UTF-8 BOM so Excel renders em-dashes and non-ASCII chars
  // correctly on Windows.
  const csv = "﻿" + dealsToCsv(deals);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const today = new Date().toISOString().slice(0, 10);
  const a = document.createElement("a");
  a.href = url;
  a.download = `gulfdealflow-deals-${today}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function ExportButton({ deals }) {
  const count = deals.length;
  const disabled = count === 0;
  return (
    <button
      type="button"
      onClick={() => !disabled && downloadDealsCsv(deals)}
      disabled={disabled}
      title={disabled
        ? "No deals to export"
        : `Download ${count} deal${count === 1 ? "" : "s"} as CSV`}
      className="inline-flex items-center gap-1.5 text-xs font-medium
                 text-gdf-teal border border-gdf-teal/40 rounded-md px-3 py-1.5
                 hover:border-gdf-teal hover:bg-gdf-teal/[0.08]
                 disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent
                 transition-colors"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="w-3.5 h-3.5"
      >
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </svg>
      Export CSV
    </button>
  );
}

function buildQuery(filters) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== "" && v != null) params.set(k, v);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

function Header() {
  return (
    <header className="border-b border-gdf-border bg-gdf-bg/95 backdrop-blur sticky top-0 z-10">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-4 flex items-center gap-4">
        <img src="/logo.svg" alt="GulfDealFlow — GCC Venture Intelligence"
             className="h-10 sm:h-12 w-auto" />
        <span className="ml-auto font-mono text-[10px] text-gdf-muted uppercase tracking-widest">
          v0.1
        </span>
      </div>
    </header>
  );
}

function StatCard({ label, value }) {
  return (
    <div className="bg-gdf-surface border border-gdf-border rounded-lg px-4 py-4">
      <p className="text-[11px] font-medium text-gdf-muted uppercase tracking-wider">
        {label}
      </p>
      <p className="mt-2 text-xl font-bold text-gdf-teal font-mono tabular-nums truncate">
        {value}
      </p>
    </div>
  );
}

function StatsBar({ stats }) {
  // Top sector uses the same normalisation as the donut chart — picking the
  // raw by_sector[0] would surface a granular sub-sector string here.
  const topSector = useMemo(() => {
    const agg = aggregateSectorsForChart(stats?.by_sector);
    const first = agg.find((s) => s.key !== "Other") || agg[0];
    return first?.key ?? "—";
  }, [stats?.by_sector]);

  const display = {
    deals:     stats?.total_deals ?? "—",
    capital:   formatCapitalB(stats?.total_capital_usd),
    countries: stats?.by_country?.length ?? "—",
    topSector,
  };
  return (
    <section className="border-b border-gdf-border bg-gdf-bg">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-5 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard label="Deals Tracked"    value={display.deals} />
        <StatCard label="Capital Deployed" value={display.capital} />
        <StatCard label="Countries"        value={display.countries} />
        <StatCard label="Top Sector"       value={display.topSector} />
      </div>
    </section>
  );
}

/* ---------- Nav (tab switcher) ---------- */

function Nav({ tab, setTab }) {
  return (
    <nav className="border-b border-gdf-border bg-gdf-bg overflow-x-auto">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 flex min-w-max">
        <NavTab active={tab === "explorer"}  onClick={() => setTab("explorer")}>Deal Explorer</NavTab>
        <NavTab active={tab === "dashboard"} onClick={() => setTab("dashboard")}>Dashboard</NavTab>
        <NavTab active={tab === "insights"}  onClick={() => setTab("insights")}>Insights</NavTab>
        <NavTab active={tab === "investors"} onClick={() => setTab("investors")}>Investors</NavTab>
        <NavTab active={tab === "about"}     onClick={() => setTab("about")}>About</NavTab>
      </div>
    </nav>
  );
}

function NavTab({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 sm:px-5 py-3 text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap
        ${active
          ? "text-gdf-teal border-gdf-teal"
          : "text-gdf-muted border-transparent hover:text-gdf-text"}`}
    >
      {children}
    </button>
  );
}

/* ---------- Dashboard (charts) ---------- */

// Monochromatic teal palette for sector donut slices. Most-frequent sector
// uses the primary teal; subsequent slices vary in lightness/darkness.
const SECTOR_COLORS = [
  "#06b6d4", "#22d3ee", "#0891b2", "#67e8f9", "#0e7490",
  "#a5f3fc", "#155e75", "#cffafe", "#164e63", "#0c4a6e", "#075985",
];

const CHART_TEAL   = "#06b6d4";
const CHART_GRID   = "#2a2f3d";
const CHART_AXIS   = "#3a4150";
const CHART_TICK   = "#8a92a6";

const tooltipContentStyle = {
  background:   "#171a23",
  border:       "1px solid #2a2f3d",
  borderRadius: 4,
  fontSize:     12,
};
const tooltipLabelStyle = { color: "#e6e8ee", fontWeight: 600 };
const tooltipItemStyle  = { color: "#06b6d4" };
const cursorStyle       = { fill: "#2a2f3d", opacity: 0.35 };

function ChartPanel({ title, subtitle, children }) {
  return (
    <section className="bg-gdf-surface border border-gdf-border rounded-lg p-5">
      <header className="mb-4">
        <h3 className="text-[11px] font-semibold text-gdf-muted uppercase tracking-wider">
          {title}
        </h3>
        {subtitle && (
          <p className="text-[11px] text-gdf-muted/70 mt-0.5">{subtitle}</p>
        )}
      </header>
      {children}
    </section>
  );
}

function YearChart({ data }) {
  // Drop "Unknown" — chart shows attributed deals only. Footnote handled in panel subtitle.
  const chartData = (data || []).filter((d) => d.key !== "Unknown");
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
        <XAxis dataKey="key" stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} />
        <YAxis stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} allowDecimals={false} />
        <Tooltip
          contentStyle={tooltipContentStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          cursor={cursorStyle}
          formatter={(value) => [value, "Deals"]}
        />
        <Bar dataKey="deal_count" fill={CHART_TEAL} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function CountryChart({ data }) {
  const chartData = (data || []).slice().sort((a, b) => b.deal_count - a.deal_count);
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={chartData} layout="vertical" margin={{ top: 8, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} horizontal={false} />
        <XAxis type="number" stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} allowDecimals={false} />
        <YAxis type="category" dataKey="key" stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} width={100} />
        <Tooltip
          contentStyle={tooltipContentStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          cursor={cursorStyle}
          formatter={(value) => [value, "Deals"]}
        />
        <Bar dataKey="deal_count" fill={CHART_TEAL} radius={[0, 3, 3, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function SectorChart({ data }) {
  const chartData = (data || []).slice().sort((a, b) => b.deal_count - a.deal_count);
  const total = chartData.reduce((sum, d) => sum + (d.deal_count || 0), 0);

  // Custom tooltip so we can render a single line with the sector name, the
  // deal count, and the % of total. The default Recharts formatter only
  // exposes name + value, but doesn't know about the whole-pie total.
  function SectorTooltip({ active, payload }) {
    if (!active || !payload || !payload.length) return null;
    const slice = payload[0];
    const count = slice.value || 0;
    const name = slice.name;
    const pct = total > 0 ? ((count / total) * 100).toFixed(1) : "0.0";
    return (
      <div style={{ ...tooltipContentStyle, padding: "6px 10px", whiteSpace: "nowrap" }}>
        <span style={{ color: "#e6e8ee", fontWeight: 600 }}>{name}</span>
        <span style={{ color: "#8a92a6" }}>{" — "}</span>
        <span style={{ color: "#06b6d4" }}>
          {count} {count === 1 ? "deal" : "deals"}
        </span>
        <span style={{ color: "#8a92a6" }}>{` (${pct}%)`}</span>
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={320}>
      <PieChart>
        <Pie
          data={chartData}
          dataKey="deal_count"
          nameKey="key"
          cx="50%"
          cy="45%"
          innerRadius={55}
          outerRadius={95}
          paddingAngle={2}
          stroke="#0f1117"
          strokeWidth={2}
        >
          {chartData.map((_, i) => (
            <Cell key={i} fill={SECTOR_COLORS[i % SECTOR_COLORS.length]} />
          ))}
        </Pie>
        <Tooltip content={<SectorTooltip />} cursor={false} />
        <Legend
          verticalAlign="bottom"
          align="center"
          height={70}
          iconSize={8}
          formatter={(label) => (
            <span style={{ color: CHART_TICK, fontSize: 11 }}>{label}</span>
          )}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}

function StageChart({ data }) {
  // Data arrives already normalised + ordered by the Dashboard (see
  // aggregateStagesForChart). No per-chart reshaping needed here.
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data || []} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} vertical={false} />
        <XAxis dataKey="key" stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} interval={0} />
        <YAxis stroke={CHART_AXIS} tick={{ fill: CHART_TICK, fontSize: 11 }} allowDecimals={false} />
        <Tooltip
          contentStyle={tooltipContentStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          cursor={cursorStyle}
          formatter={(value) => [value, "Deals"]}
        />
        <Bar dataKey="deal_count" fill={CHART_TEAL} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/* ---------- GCC geography map ---------- */

// Map world-atlas ISO numeric ids → the country names used elsewhere in the
// app (matches the values in the COUNTRIES filter and stats.by_country.key).
// "United Arab Emirates" is shortened to "UAE" for both label and filter.
const ISO_TO_NAME = {
  "048": "Bahrain",
  "414": "Kuwait",
  "512": "Oman",
  "634": "Qatar",
  "682": "Saudi Arabia",
  "784": "UAE",
};

// Per-country label tweaks. Small countries get their label nudged off the
// centroid (or hidden in favour of the tooltip) so they remain readable.
const LABEL_OVERRIDES = {
  // Bahrain is a tiny archipelago — its centroid label would overlap Qatar.
  // Nudge north-east into open water and skip the deal-count line.
  Bahrain:  { dx: 28,  dy: -8, fontSize: 10, showCount: false },
  Qatar:    { dx: 0,   dy: 0,  fontSize: 10, showCount: true },
  Kuwait:   { dx: 0,   dy: 0,  fontSize: 11, showCount: true },
};

// Linear-interpolate between two hex colours. t is clamped to [0,1].
function lerpHex(a, b, t) {
  const clamp = Math.max(0, Math.min(1, t));
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const ar = (pa >> 16) & 0xff, ag = (pa >> 8) & 0xff, ab = pa & 0xff;
  const br = (pb >> 16) & 0xff, bg = (pb >> 8) & 0xff, bb = pb & 0xff;
  const r = Math.round(ar + (br - ar) * clamp);
  const g = Math.round(ag + (bg - ag) * clamp);
  const bl = Math.round(ab + (bb - ab) * clamp);
  return `#${[r, g, bl].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

const MAP_GRADIENT_LO = "#cffafe";
const MAP_GRADIENT_HI = "#0e7490";
const MAP_BORDER       = "#334155";

// Tuned for the bundled GCC topojson. Mercator centred between Saudi's
// western Red Sea coast and Oman's eastern tip, with scale chosen so the
// full peninsula fits the viewBox with margin on every side.
const MAP_PROJECTION_CONFIG = { scale: 1650, center: [47.5, 24.5] };
const MAP_WIDTH  = 1000;
const MAP_HEIGHT = 560;

function GeographyMap({ data, onCountryClick, topSectorByCountry }) {
  const [hover, setHover] = useState(null); // { name, x, y }

  const byName = useMemo(() => {
    const m = new Map();
    (data || []).forEach((d) => m.set(d.key, d));
    return m;
  }, [data]);

  const maxCount = useMemo(() => {
    const counts = Object.values(ISO_TO_NAME).map(
      (n) => byName.get(n)?.deal_count || 0
    );
    return Math.max(1, ...counts);
  }, [byName]);

  const colorFor = (count) => {
    // sqrt eases the gradient so mid-volume countries don't blend with the floor.
    const t = Math.sqrt(count / maxCount);
    return lerpHex(MAP_GRADIENT_LO, MAP_GRADIENT_HI, t);
  };

  // Track pointer in container-relative coords so the tooltip can follow.
  const handleMove = (e, name) => {
    const container = e.currentTarget.closest("[data-map-container]");
    if (!container) return;
    const rect = container.getBoundingClientRect();
    setHover({
      name,
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    });
  };

  const hovered = hover ? byName.get(hover.name) : null;
  const hoveredCount = hovered?.deal_count || 0;
  const hoveredCapital = hovered?.total_capital_usd || 0;

  return (
    <div
      data-map-container
      className="relative w-full"
      style={{ maxHeight: 460 }}
    >
      <ComposableMap
        projection="geoMercator"
        projectionConfig={MAP_PROJECTION_CONFIG}
        width={MAP_WIDTH}
        height={MAP_HEIGHT}
        style={{ width: "100%", height: "auto", maxHeight: 460, display: "block" }}
        aria-label="GCC deal map"
      >
        <Geographies geography={gccTopo}>
          {({ geographies }) => (
            <>
              {geographies.map((geo) => {
                const name = ISO_TO_NAME[String(geo.id).padStart(3, "0")];
                if (!name) return null;
                const entry = byName.get(name);
                const count = entry?.deal_count || 0;
                const isHover = hover?.name === name;
                const fill = colorFor(count);
                return (
                  <Geography
                    key={geo.rsmKey}
                    geography={geo}
                    onMouseEnter={(e) => handleMove(e, name)}
                    onMouseMove={(e) => handleMove(e, name)}
                    onMouseLeave={() => setHover(null)}
                    onClick={() => onCountryClick && onCountryClick(name)}
                    style={{
                      default: {
                        fill,
                        stroke: MAP_BORDER,
                        strokeWidth: 0.75,
                        strokeLinejoin: "round",
                        outline: "none",
                        cursor: "pointer",
                        transition: "filter 150ms ease",
                      },
                      hover: {
                        fill,
                        stroke: MAP_BORDER,
                        strokeWidth: 0.75,
                        outline: "none",
                        cursor: "pointer",
                        filter: "brightness(1.18)",
                      },
                      pressed: {
                        fill,
                        stroke: MAP_BORDER,
                        strokeWidth: 0.75,
                        outline: "none",
                      },
                    }}
                  />
                );
              })}
              {/* Labels rendered on top of all geographies so they don't get
                  clipped by neighbouring shapes. */}
              {geographies.map((geo) => {
                const name = ISO_TO_NAME[String(geo.id).padStart(3, "0")];
                if (!name) return null;
                const entry = byName.get(name);
                const count = entry?.deal_count || 0;
                const override = LABEL_OVERRIDES[name] || {};
                const fontSize = override.fontSize ?? 12;
                const showCount = override.showCount ?? true;
                const [cx, cy] = geoCentroid(geo);
                return (
                  <Marker
                    key={`label-${geo.rsmKey}`}
                    coordinates={[cx + (override.dx || 0) / 60, cy + (override.dy || 0) / 60]}
                    style={{ default: { pointerEvents: "none" } }}
                  >
                    <text
                      textAnchor="middle"
                      y={-2}
                      fill="#ffffff"
                      fontSize={fontSize}
                      fontWeight={600}
                      style={{
                        paintOrder: "stroke",
                        stroke: "rgba(15,17,23,0.75)",
                        strokeWidth: 3,
                        strokeLinejoin: "round",
                      }}
                    >
                      {name}
                    </text>
                    {showCount && (
                      <text
                        textAnchor="middle"
                        y={fontSize + 1}
                        fill="#ffffff"
                        fontSize={fontSize - 2}
                        fontWeight={500}
                        opacity={0.92}
                        style={{
                          paintOrder: "stroke",
                          stroke: "rgba(15,17,23,0.75)",
                          strokeWidth: 3,
                          strokeLinejoin: "round",
                        }}
                      >
                        {count} {count === 1 ? "deal" : "deals"}
                      </text>
                    )}
                  </Marker>
                );
              })}
            </>
          )}
        </Geographies>
      </ComposableMap>

      {hover && (
        <div
          className="absolute pointer-events-none rounded border border-gdf-border bg-[#171a23] px-3 py-2 text-xs shadow-lg"
          style={{
            left: hover.x + 12,
            top: hover.y + 12,
            whiteSpace: "nowrap",
            zIndex: 10,
          }}
        >
          <div className="font-semibold text-gdf-text">{hover.name}</div>
          <div className="text-gdf-teal font-mono tabular-nums">
            {hoveredCount} {hoveredCount === 1 ? "deal" : "deals"}
          </div>
          <div className="text-gdf-muted font-mono tabular-nums">
            {formatCapitalB(hoveredCapital)} total capital
          </div>
          {topSectorByCountry?.[hover.name] && (
            <div className="text-gdf-muted mt-1">
              Top sector:{" "}
              <span className="text-gdf-text">
                {topSectorByCountry[hover.name]}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Dashboard({ stats, onCountryClick }) {
  // Per-country top sector for the map tooltip. Deals are fetched once when
  // the Dashboard mounts; the breakdown is computed client-side from the
  // normalised sector so it stays in sync with the donut chart's buckets.
  const [allDeals, setAllDeals] = useState(null);
  useEffect(() => {
    const ctrl = new AbortController();
    fetch(`${API_URL}/deals?limit=500`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => setAllDeals(data.deals || []))
      .catch((err) => { if (err.name !== "AbortError") setAllDeals([]); });
    return () => ctrl.abort();
  }, []);

  const topSectorByCountry = useMemo(() => {
    if (!allDeals) return {};
    const per = {};
    for (const d of allDeals) {
      if (!d.country) continue;
      const norm = normalizeSector(d.sector);
      per[d.country] = per[d.country] || {};
      per[d.country][norm] = (per[d.country][norm] || 0) + 1;
    }
    const out = {};
    for (const [c, sectors] of Object.entries(per)) {
      const top = Object.entries(sectors).sort((a, b) => b[1] - a[1])[0];
      if (top) out[c] = top[0];
    }
    return out;
  }, [allDeals]);

  // Normalised + capped sector data for the donut. The raw `by_sector` has
  // dozens of granular sub-sector strings; this collapses them into the
  // canonical category list with <2% groups folded into "Other".
  const normalisedSectors = useMemo(
    () => aggregateSectorsForChart(stats?.by_sector),
    [stats?.by_sector]
  );

  // Same idea for stages: raw `by_stage` has paraphrases like "Venture
  // investment / seed-like round" — collapse into canonical funding stages.
  const normalisedStages = useMemo(
    () => aggregateStagesForChart(stats?.by_stage),
    [stats?.by_stage]
  );

  // One-time log so the user can see the grouping worked.
  useEffect(() => {
    if (!stats?.by_sector) return;
    const rawCount = stats.by_sector.filter(
      (s) => s.key && s.key !== "Unknown"
    ).length;
    // eslint-disable-next-line no-console
    console.log(
      `[Sectors] Normalised ${rawCount} raw sub-sectors → ${normalisedSectors.length} chart slices`
    );
  }, [stats?.by_sector, normalisedSectors.length]);

  useEffect(() => {
    if (!stats?.by_stage) return;
    const rawCount = stats.by_stage.length;
    const populated = normalisedStages.filter((s) => s.deal_count > 0).length;
    // eslint-disable-next-line no-console
    console.log(
      `[Stages] Normalised ${rawCount} raw stage labels → ${normalisedStages.length} canonical buckets (${populated} non-empty)`
    );
  }, [stats?.by_stage, normalisedStages]);

  if (!stats) {
    return (
      <div className="py-20 text-center text-gdf-muted">
        <div className="inline-block w-2 h-2 bg-gdf-teal rounded-full animate-pulse mr-2" />
        Loading dashboard data…
      </div>
    );
  }
  const unknownYear = (stats.by_year || []).find((y) => y.key === "Unknown");
  const yearSubtitle = unknownYear
    ? `${unknownYear.deal_count} deal${unknownYear.deal_count === 1 ? "" : "s"} excluded (no date)`
    : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <ChartPanel title="Deal Volume by Year" subtitle={yearSubtitle}>
        <YearChart data={stats.by_year} />
      </ChartPanel>
      <ChartPanel title="Deals by Country">
        <CountryChart data={stats.by_country} />
      </ChartPanel>
      <ChartPanel title="Deals by Sector">
        <SectorChart data={normalisedSectors} />
      </ChartPanel>
      <ChartPanel title="Deals by Stage">
        <StageChart data={normalisedStages} />
      </ChartPanel>
      <div className="lg:col-span-2">
        <ChartPanel title="Deals by Geography" subtitle="Click a country to filter the Deal Explorer">
          <GeographyMap
            data={stats.by_country}
            onCountryClick={onCountryClick}
            topSectorByCountry={topSectorByCountry}
          />
        </ChartPanel>
      </div>
    </div>
  );
}

/* ---------- Insights (articles) ---------- */

const ARTICLES = [
  {
    id: "gcc-2024-review",
    title: "State of GCC Venture: 2024 in Review",
    category: "Market Analysis",
    date: "January 2025",
    excerpt:
      "2024 marked a pivotal year for venture capital across the Gulf, with deal volume " +
      "reaching new highs and fintech cementing its position as the region's dominant sector…",
    featured: true,
    status: "coming-soon",
  },
  {
    id: "uae-vs-saudi",
    title: "UAE vs Saudi Arabia: Where is the Capital Flowing?",
    category: "Country Focus",
    date: "March 2025",
    excerpt:
      "The UAE and Saudi Arabia together account for over 70% of GCC venture deals. " +
      "But the dynamics between the two ecosystems are shifting…",
    status: "coming-soon",
  },
];

function CategoryTag({ children }) {
  return (
    <span className="inline-block font-mono text-[10px] uppercase tracking-wider
                     text-gdf-teal border border-gdf-teal/30 bg-gdf-teal/10
                     rounded px-2 py-0.5">
      {children}
    </span>
  );
}

function ComingSoonBadge() {
  return (
    <span className="inline-block font-mono text-[10px] uppercase tracking-wider
                     text-gdf-muted border border-gdf-border bg-gdf-bg/60
                     rounded px-2 py-0.5">
      Coming Soon
    </span>
  );
}

function ReadMore({ disabled }) {
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-medium
                  ${disabled ? "text-gdf-muted" : "text-gdf-teal"}`}
    >
      Read more
      <span aria-hidden="true">→</span>
    </span>
  );
}

function FeaturedCard({ article }) {
  const disabled = article.status === "coming-soon";
  return (
    <article
      className={`group bg-gdf-surface border border-gdf-border rounded-lg p-6 sm:p-8
                  transition-colors duration-200
                  ${disabled ? "" : "cursor-pointer"}
                  hover:border-gdf-teal/40`}
    >
      <div className="flex flex-wrap items-center gap-2 mb-5">
        <span className="font-mono text-[10px] uppercase tracking-widest text-gdf-teal">
          Featured
        </span>
        <span className="text-gdf-border">·</span>
        <CategoryTag>{article.category}</CategoryTag>
        {disabled && <ComingSoonBadge />}
      </div>

      <h2 className="text-2xl sm:text-3xl font-bold text-gdf-text leading-tight mb-3
                     group-hover:text-white transition-colors">
        {article.title}
      </h2>

      <p className="text-gdf-muted text-sm sm:text-base leading-relaxed mb-6 max-w-3xl">
        {article.excerpt}
      </p>

      <div className="flex items-center justify-between gap-4">
        <span className="text-[11px] font-mono text-gdf-muted uppercase tracking-wider">
          {article.date}
        </span>
        <ReadMore disabled={disabled} />
      </div>
    </article>
  );
}

function ArticleCard({ article }) {
  const disabled = article.status === "coming-soon";
  return (
    <article
      className={`group bg-gdf-surface border border-gdf-border rounded-lg p-5
                  transition-colors duration-200 flex flex-col
                  ${disabled ? "" : "cursor-pointer"}
                  hover:border-gdf-teal/40`}
    >
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <CategoryTag>{article.category}</CategoryTag>
        {disabled && <ComingSoonBadge />}
      </div>

      <h3 className="text-lg font-bold text-gdf-text leading-snug mb-2
                     group-hover:text-white transition-colors">
        {article.title}
      </h3>

      <p className="text-gdf-muted text-sm leading-relaxed mb-5 line-clamp-3">
        {article.excerpt}
      </p>

      <div className="mt-auto flex items-center justify-between gap-4">
        <span className="text-[11px] font-mono text-gdf-muted uppercase tracking-wider">
          {article.date}
        </span>
        <ReadMore disabled={disabled} />
      </div>
    </article>
  );
}

function PlaceholderCard() {
  return (
    <div className="rounded-lg p-5 border border-dashed border-gdf-border
                    flex items-center justify-center min-h-[180px]">
      <p className="text-gdf-muted text-xs font-mono uppercase tracking-wider text-center">
        More insights coming soon
      </p>
    </div>
  );
}

function Insights() {
  const featured = ARTICLES.find((a) => a.featured);
  const others = ARTICLES.filter((a) => !a.featured);
  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl sm:text-3xl font-bold text-gdf-text">Insights</h1>
        <p className="text-gdf-muted text-sm">
          Analysis and commentary on GCC venture capital
        </p>
      </header>

      {featured && <FeaturedCard article={featured} />}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {others.map((a) => (
          <ArticleCard key={a.id} article={a} />
        ))}
        <PlaceholderCard />
      </div>
    </div>
  );
}

/* ---------- Investor Directory ---------- */

function formatTimelineAmount(amount) {
  if (!amount) return "Undisclosed";
  if (amount >= 1_000_000_000) return `$${(amount / 1_000_000_000).toFixed(2)}B`;
  if (amount >= 1_000_000)     return `$${(amount / 1_000_000).toFixed(1)}M`;
  if (amount >= 1_000)         return `$${(amount / 1_000).toFixed(0)}K`;
  return `$${amount}`;
}

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatTimelineDate(date) {
  if (!date) return "Date unknown";
  const m = /^(\d{4})-(\d{2})/.exec(date);
  if (!m) return date;
  const year = m[1];
  const month = MONTH_ABBR[parseInt(m[2], 10) - 1] || "";
  return month ? `${month} ${year}` : year;
}

function dealMatchesInvestor(deal, investorName) {
  const needle = investorName.toLowerCase();
  const lead = (deal.lead_investor || "").toLowerCase();
  const co   = (deal.co_investors  || "").toLowerCase();
  return lead.includes(needle) || co.includes(needle);
}

// One pin = thin vertical line + a rectangular deal card hanging off the
// axis (above or below depending on chronological index). Hover shows full
// tooltip; click closes the modal and jumps to the Deal Explorer.
function TimelinePin({ deal, x, axisY, above, onClick }) {
  const PIN_LENGTH  = 36;
  const CARD_WIDTH  = 164;
  const CARD_HEIGHT = 96;

  const lineTop = above ? axisY - PIN_LENGTH : axisY;
  const cardTop = above
    ? axisY - PIN_LENGTH - CARD_HEIGHT
    : axisY + PIN_LENGTH;

  return (
    <div
      className="absolute top-0 group"
      style={{ left: x, transform: "translateX(-50%)" }}
    >
      {/* Pin line */}
      <div
        className="absolute w-px bg-[#334155]"
        style={{ left: "50%", marginLeft: -0.5, top: lineTop, height: PIN_LENGTH }}
      />

      {/* Card */}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onClick(deal); }}
        className="absolute bg-[#1c2333] border border-gdf-border rounded-md
                   hover:border-gdf-teal/50 transition-colors text-left overflow-hidden
                   focus:outline-none focus-visible:ring-2 focus-visible:ring-gdf-teal"
        style={{
          width: CARD_WIDTH,
          height: CARD_HEIGHT,
          left: "50%",
          top: cardTop,
          marginLeft: -CARD_WIDTH / 2,
        }}
      >
        <div className="p-3 flex flex-col gap-1.5 h-full">
          <div className="text-sm font-bold text-white truncate">
            {deal.company_name}
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className={`inline-block font-mono text-[10px] uppercase tracking-wider
                              px-1.5 py-0.5 rounded border ${stageBadgeClass(deal.stage)}`}>
              {deal.stage || "—"}
            </span>
            <span className="text-gdf-teal font-mono tabular-nums">
              {formatTimelineAmount(deal.amount_usd)}
            </span>
          </div>
          <div className="text-[10px] text-gdf-muted font-mono mt-auto">
            {deal.date ? deal.date.slice(0, 4) : ""}
          </div>
        </div>
      </button>

      {/* Hover tooltip — appears on the opposite side of the card from the axis */}
      <div
        role="tooltip"
        className="absolute opacity-0 group-hover:opacity-100 transition-opacity
                   pointer-events-none z-30"
        style={{
          left: "50%",
          top: above ? cardTop - 8 : cardTop + CARD_HEIGHT + 8,
          transform: above
            ? "translate(-50%, -100%)"
            : "translate(-50%, 0)",
        }}
      >
        <div className="bg-gdf-bg border border-gdf-border rounded-md px-3 py-2
                        text-[11px] leading-snug shadow-xl min-w-[220px]">
          <div className="font-semibold text-gdf-text mb-1">{deal.company_name}</div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
            <dt className="text-gdf-muted">Stage</dt>
            <dd className="text-gdf-text">{deal.stage || "—"}</dd>
            <dt className="text-gdf-muted">Amount</dt>
            <dd className="text-gdf-teal">{formatTimelineAmount(deal.amount_usd)}</dd>
            <dt className="text-gdf-muted">Date</dt>
            <dd className="text-gdf-text">{formatTimelineDate(deal.date)}</dd>
            <dt className="text-gdf-muted">Sector</dt>
            <dd className="text-gdf-text">{deal.sector || "—"}</dd>
            <dt className="text-gdf-muted">Lead</dt>
            <dd className="text-gdf-text">{deal.lead_investor || "—"}</dd>
          </dl>
        </div>
      </div>
    </div>
  );
}

function TimelinePlot({ deals, onCardClick }) {
  if (deals.length === 0) {
    return (
      <div className="h-full flex items-center justify-center px-8">
        <p className="text-sm text-gdf-muted">No deals tracked yet.</p>
      </div>
    );
  }

  // Plot constants
  const SIDE_PAD  = 80;
  const CARD_GAP  = 200;             // horizontal px between adjacent card centers
  const MIN_WIDTH = 800;
  const AXIS_Y    = 200;             // y-position of the axis inside the plot
  const PLOT_H    = 400;             // total vertical area for the plot

  const n = deals.length;
  const naturalWidth = SIDE_PAD * 2 + Math.max(0, n - 1) * CARD_GAP;
  const plotWidth    = Math.max(MIN_WIDTH, naturalWidth);
  const usableSpan   = plotWidth - SIDE_PAD * 2;

  // Card X positions — evenly spaced across the usable span by chronological
  // index. Single-deal edge case → centered.
  const cardX = (i) => {
    if (n === 1) return plotWidth / 2;
    return SIDE_PAD + (i / (n - 1)) * usableSpan;
  };

  // Year labels — evenly spaced across the same span by year value.
  const years = deals.map((d) => parseInt(d.date.slice(0, 4), 10));
  let minYear = Math.min(...years);
  let maxYear = Math.max(...years);
  if (maxYear - minYear < 3) maxYear = minYear + 3;  // breathing room
  const yearSpan = Math.max(1, maxYear - minYear);
  const yearLabels = [];
  for (let y = minYear; y <= maxYear; y++) {
    const x = SIDE_PAD + ((y - minYear) / yearSpan) * usableSpan;
    yearLabels.push({ year: y, x });
  }

  return (
    <div className="h-full flex items-center px-4 py-6">
      <div className="relative" style={{ width: plotWidth, height: PLOT_H }}>
        {/* Axis */}
        <div
          className="absolute left-0 right-0 h-px bg-[#334155]"
          style={{ top: AXIS_Y }}
        />
        {/* Year markers + tick marks */}
        {yearLabels.map(({ year, x }) => (
          <div key={year}>
            <div
              className="absolute bg-[#334155]"
              style={{
                left: x, top: AXIS_Y - 3, width: 1, height: 7, marginLeft: -0.5,
              }}
            />
            <div
              className="absolute text-[10px] text-gdf-muted font-mono"
              style={{ left: x, top: AXIS_Y + 8, transform: "translateX(-50%)" }}
            >
              {year}
            </div>
          </div>
        ))}
        {/* Pins — first deal above, second below, alternating */}
        {deals.map((deal, i) => (
          <TimelinePin
            key={deal.deal_id}
            deal={deal}
            x={cardX(i)}
            axisY={AXIS_Y}
            above={i % 2 === 0}
            onClick={onCardClick}
          />
        ))}
      </div>
    </div>
  );
}

function TimelineModal({ investor, allDeals, onClose, onViewDeals }) {
  // ESC closes the modal.
  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  // Lock body scroll while the modal is open.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, []);

  const filtered = (allDeals || []).filter((d) => dealMatchesInvestor(d, investor.name));
  const dated    = filtered.filter((d) => d.date && /^\d{4}/.test(d.date));
  const sorted   = [...dated].sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  const undated  = filtered.filter((d) => !d.date || !/^\d{4}/.test(d.date));

  const disclosed     = filtered.filter((d) => d.amount_usd).map((d) => d.amount_usd);
  const totalDeployed = disclosed.reduce((s, n) => s + n, 0);

  return (
    <div
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Deal timeline for ${investor.name}`}
      className="fixed inset-0 z-50 flex items-center justify-center
                 bg-black/85 backdrop-blur-sm"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-[90vw] h-[80vh] flex flex-col
                   bg-[#161b27] rounded-xl border border-gdf-teal/30
                   shadow-2xl overflow-hidden"
      >
        {/* Header */}
        <header className="flex items-start justify-between gap-6 px-6 py-4
                           border-b border-gdf-border">
          <div className="min-w-0">
            <h2 className="text-xl sm:text-2xl font-bold text-white truncate">
              {investor.name}
            </h2>
            <p className="text-xs text-gdf-muted mt-0.5">{investor.country}</p>
          </div>
          <div className="flex items-center gap-4 shrink-0">
            <p className="text-xs text-gdf-muted hidden sm:block">
              <span className="text-gdf-text font-semibold">{filtered.length}</span>
              {filtered.length === 1 ? " deal" : " deals"}
              <span className="mx-1.5">·</span>
              <span className="text-gdf-text font-semibold">
                {totalDeployed > 0 ? formatTimelineAmount(totalDeployed) : "—"}
              </span>
              {" total"}
            </p>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close timeline"
              className="w-8 h-8 flex items-center justify-center rounded
                         text-gdf-muted hover:text-gdf-text hover:bg-gdf-border/40
                         transition-colors text-xl leading-none"
            >
              ×
            </button>
          </div>
        </header>

        {/* Timeline body */}
        <div className="flex-1 overflow-x-auto overflow-y-hidden">
          {allDeals == null ? (
            <div className="h-full flex items-center justify-center text-sm text-gdf-muted">
              Loading deals…
            </div>
          ) : (
            <TimelinePlot
              deals={sorted}
              onCardClick={(deal) => { onClose(); onViewDeals(deal.company_name); }}
            />
          )}
        </div>

        {/* Footer note for undated deals */}
        {undated.length > 0 && (
          <p className="px-6 py-2 border-t border-gdf-border text-[11px] text-gdf-muted shrink-0">
            + {undated.length} deal{undated.length === 1 ? "" : "s"} with no date — not shown on timeline
          </p>
        )}
      </div>
    </div>
  );
}

// One card per investor, generated from a /investors/leaderboard row. The
// rank sits subtly in the top-right; the four key stats (deals, capital, top
// sector, top stage) form a 2x2 grid; actions live in a footer row.
function InvestorRankCard({ row, onViewDeals, onOpenTimeline }) {
  return (
    <article
      className="group bg-gdf-surface border border-gdf-border rounded-lg p-5
                 flex flex-col gap-4 transition-colors duration-200
                 hover:border-gdf-teal/40"
    >
      <header className="flex items-start justify-between gap-3">
        <h3 className="text-base font-bold text-gdf-text group-hover:text-white
                       transition-colors truncate min-w-0 flex-1">
          {row.investor}
        </h3>
        <span className="shrink-0 font-mono text-[11px] text-gdf-muted tabular-nums">
          #{row.rank}
        </span>
      </header>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-3">
        <div className="min-w-0">
          <dt className="text-[10px] uppercase tracking-wider text-gdf-muted font-mono mb-0.5">
            Deals
          </dt>
          <dd className="text-sm font-mono tabular-nums text-gdf-text">
            {row.deal_count}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] uppercase tracking-wider text-gdf-muted font-mono mb-0.5">
            Capital
          </dt>
          <dd className="text-sm font-mono tabular-nums text-gdf-teal">
            {row.capital_deployed_usd > 0
              ? formatAmount(row.capital_deployed_usd)
              : "—"}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] uppercase tracking-wider text-gdf-muted font-mono mb-0.5">
            Top Sector
          </dt>
          <dd className="text-xs text-gdf-text truncate">
            {row.top_sector || "—"}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] uppercase tracking-wider text-gdf-muted font-mono mb-0.5">
            Top Stage
          </dt>
          <dd>
            {row.top_stage ? (
              <span className={`inline-block font-mono text-[10px] uppercase tracking-wider
                                px-1.5 py-0.5 rounded border ${stageBadgeClass(row.top_stage)}`}>
                {row.top_stage}
              </span>
            ) : (
              <span className="text-xs text-gdf-muted">—</span>
            )}
          </dd>
        </div>
      </dl>

      <div className="mt-auto pt-3 border-t border-gdf-border/60
                      flex justify-end items-center gap-3 flex-wrap">
        <button
          type="button"
          onClick={() => onViewDeals(row.investor)}
          className="text-xs font-medium text-gdf-muted hover:text-gdf-text transition-colors"
        >
          View deals
        </button>
        <span aria-hidden="true" className="text-gdf-border">·</span>
        <button
          type="button"
          onClick={() => onOpenTimeline(row)}
          className="text-xs font-medium text-gdf-teal hover:underline
                     inline-flex items-center gap-1"
        >
          Investment Timeline
          <span aria-hidden="true">→</span>
        </button>
      </div>
    </article>
  );
}

function InvestorFilterBar({ filters, setFilters }) {
  const update = (key) => (val) => setFilters((f) => ({ ...f, [key]: val }));
  return (
    <section className="border-b border-gdf-border bg-gdf-surface/40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-5 flex flex-col sm:flex-row sm:flex-wrap sm:items-end gap-3 sm:gap-4">
        <Select label="Country" value={filters.country} options={COUNTRIES}
                onChange={update("country")} allLabel="All countries" />
        <Select label="Stage" value={filters.stage} options={STAGES}
                onChange={update("stage")} allLabel="All stages" />
      </div>
    </section>
  );
}

function InvestorDirectory({ onViewDeals }) {
  const [filters, setFilters] = useState({ country: "", stage: "" });
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [allDeals, setAllDeals] = useState(null);
  // Currently-open timeline modal; null = closed.
  const [modalInvestor, setModalInvestor] = useState(null);

  // Leaderboard fetch — refires on filter change. Country/Stage are passed
  // straight through to the API, which filters deals first then aggregates
  // per investor (so deal_count, capital_deployed_usd, top_sector and
  // top_stage all reflect the filtered slice).
  useEffect(() => {
    const ctrl = new AbortController();
    setRows(null);
    setError(null);
    fetch(`${API_URL}/investors/leaderboard${buildQuery(filters)}`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => setRows(data.investors || []))
      .catch((err) => { if (err.name !== "AbortError") setError(String(err)); });
    return () => ctrl.abort();
  }, [filters]);

  // One-shot fetch of the full deal set so the timeline modal can render
  // immediately on click without waiting for a per-investor request.
  useEffect(() => {
    const ctrl = new AbortController();
    fetch(`${API_URL}/deals?limit=500`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => setAllDeals(data.deals || []))
      .catch((err) => { if (err.name !== "AbortError") setAllDeals([]); });
    return () => ctrl.abort();
  }, []);

  // The /investors/leaderboard endpoint doesn't return a country per
  // investor, so we derive the most-active country from allDeals to populate
  // the modal subtitle (which still reads `investor.country`).
  const handleOpenTimeline = (row) => {
    let country = "";
    if (allDeals) {
      const counts = new Map();
      for (const d of allDeals) {
        if (!d.country || !dealMatchesInvestor(d, row.investor)) continue;
        counts.set(d.country, (counts.get(d.country) || 0) + 1);
      }
      const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
      if (top) country = top[0];
    }
    setModalInvestor({ name: row.investor, country });
  };

  return (
    <>
      <InvestorFilterBar filters={filters} setFilters={setFilters} />
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
        <header className="space-y-1.5 mb-6">
          <h1 className="text-2xl sm:text-3xl font-bold text-gdf-text">
            Investor Directory
          </h1>
          <p className="text-gdf-muted text-sm">
            Active venture capital funds across the GCC — ranked by deal count
          </p>
        </header>

        {rows == null && !error && (
          <div className="py-20 text-center text-gdf-muted text-sm">
            <span className="inline-block w-2 h-2 bg-gdf-teal rounded-full animate-pulse mr-2" />
            Loading investors…
          </div>
        )}

        {error && (
          <div className="py-20 text-center text-red-400 text-sm">
            Failed to load investors ({error})
          </div>
        )}

        {rows && rows.length === 0 && !error && (
          <div className="py-20 text-center text-gdf-muted text-sm">
            No investors match the selected filters.
          </div>
        )}

        {rows && rows.length > 0 && (
          <>
            <p className="text-sm text-gdf-muted mb-4">
              Showing{" "}
              <span className="text-gdf-text font-semibold">
                {rows.length} {rows.length === 1 ? "investor" : "investors"}
              </span>
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {rows.map((row) => (
                <InvestorRankCard
                  key={row.investor}
                  row={row}
                  onViewDeals={onViewDeals}
                  onOpenTimeline={handleOpenTimeline}
                />
              ))}
            </div>
          </>
        )}
      </main>

      {modalInvestor && (
        <TimelineModal
          investor={modalInvestor}
          allDeals={allDeals}
          onClose={() => setModalInvestor(null)}
          onViewDeals={onViewDeals}
        />
      )}
    </>
  );
}

/* ---------- About ---------- */

function SectionHeading({ children }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-gdf-teal">
      {children}
    </h2>
  );
}

function CoverageStat({ value, label }) {
  return (
    <div className="min-w-0">
      <p className="text-2xl sm:text-3xl md:text-4xl font-mono font-bold text-gdf-teal tabular-nums break-words">
        {value}
      </p>
      <p className="text-[11px] text-gdf-muted uppercase tracking-wider mt-2">
        {label}
      </p>
    </div>
  );
}

function About({ stats }) {
  return (
    <article className="max-w-3xl space-y-12">
      <header>
        <h1 className="text-3xl sm:text-4xl font-bold text-gdf-text">
          About GulfDealFlow
        </h1>
      </header>

      <p className="text-base sm:text-lg text-gdf-text leading-relaxed">
        GulfDealFlow is an independent venture intelligence platform tracking
        startup funding across the Gulf Cooperation Council. Built to make GCC
        deal flow data accessible, searchable, and useful for founders, investors,
        and analysts operating in the region.
      </p>

      <section className="space-y-4">
        <SectionHeading>What We Track</SectionHeading>
        <ul className="list-disc list-outside ml-5 marker:text-gdf-teal space-y-2.5">
          <li className="text-gdf-text leading-relaxed">
            All 6 GCC countries — UAE, Saudi Arabia, Kuwait, Bahrain, Oman, Qatar
          </li>
          <li className="text-gdf-text leading-relaxed">
            Funding stages Pre-Seed through Growth
          </li>
          <li className="text-gdf-text leading-relaxed">
            Data sourced from public announcements and fund portfolios
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeading>Methodology</SectionHeading>
        <div className="space-y-3 text-gdf-text leading-relaxed">
          <p>
            Data is sourced manually from public announcements, investor
            portfolio pages, and regional press.
          </p>
          <p>All deals verified against at least one primary source.</p>
          <p>
            Amounts converted to USD where originally reported in local
            currency.
          </p>
        </div>
      </section>

      <section className="space-y-5">
        <SectionHeading>Coverage</SectionHeading>
        <div className="grid grid-cols-3 gap-6 sm:gap-12">
          <CoverageStat
            value={stats?.total_deals ?? "—"}
            label="Total Deals"
          />
          <CoverageStat
            value={formatCapitalB(stats?.total_capital_usd)}
            label="Total Capital"
          />
          <CoverageStat
            value={stats?.by_country?.length ?? "—"}
            label="Countries"
          />
        </div>
      </section>

      <section className="space-y-4">
        <SectionHeading>Contact</SectionHeading>
        <p className="text-gdf-text leading-relaxed">
          Questions, feedback, or data corrections? Reach out at{" "}
          <a
            href="mailto:stavros.gaiganis@gmail.com"
            className="text-gdf-teal hover:underline break-all"
          >
            stavros.gaiganis@gmail.com
          </a>
          {" "}or connect on{" "}
          <a
            href="https://www.linkedin.com/in/stavros-gaiganis-1a3baa282/"
            target="_blank"
            rel="noreferrer"
            className="text-gdf-teal hover:underline"
          >
            LinkedIn
          </a>
          .
        </p>
      </section>

      <div className="pt-8 border-t border-gdf-border">
        <p className="text-[11px] text-gdf-muted font-mono uppercase tracking-wider">
          Built by Stavros Gaiganis — Dubai, UAE
        </p>
      </div>
    </article>
  );
}

/* ---------- Deal Explorer (existing) ---------- */

function SearchBar({ value, onChange }) {
  return (
    <div className="relative w-full md:max-w-md">
      {/* Magnifying-glass icon, left-aligned inside the input. */}
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gdf-muted pointer-events-none"
      >
        <circle cx="11" cy="11" r="7" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search company, investor, sector…"
        aria-label="Search deals"
        className="w-full bg-gdf-surface border border-gdf-border rounded-md
                   pl-9 pr-9 py-2 text-sm text-gdf-text placeholder-gdf-muted
                   focus:outline-none focus:border-gdf-teal focus:ring-1 focus:ring-gdf-teal
                   transition-colors"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label="Clear search"
          className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center
                     text-gdf-muted hover:text-gdf-text rounded transition-colors"
        >
          ×
        </button>
      )}
    </div>
  );
}

function Select({ label, value, options, onChange, allLabel }) {
  return (
    <label className="flex flex-col gap-1.5 w-full sm:w-auto sm:min-w-[140px]">
      <span className="text-[11px] font-medium text-gdf-muted uppercase tracking-wider">
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-gdf-surface border border-gdf-border rounded-md px-3 py-2 text-sm text-gdf-text
                   focus:outline-none focus:border-gdf-accent focus:ring-1 focus:ring-gdf-accent
                   transition-colors cursor-pointer"
      >
        <option value="">{allLabel}</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>{opt}</option>
        ))}
      </select>
    </label>
  );
}

function FilterBar({ filters, setFilters, sectors, onReset, search, setSearch, hasActiveFilters }) {
  const update = (key) => (val) => setFilters((f) => ({ ...f, [key]: val }));

  return (
    <section className="border-b border-gdf-border bg-gdf-surface/40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-5 space-y-4">
        <SearchBar value={search} onChange={setSearch} />
        <div className="flex flex-col sm:flex-row sm:flex-wrap sm:items-end gap-3 sm:gap-4">
          <Select label="Country" value={filters.country} options={COUNTRIES}
                  onChange={update("country")} allLabel="All countries" />
          <Select label="Sector" value={filters.sector} options={sectors}
                  onChange={update("sector")} allLabel="All sectors" />
          <Select label="Stage" value={filters.stage} options={STAGES}
                  onChange={update("stage")} allLabel="All stages" />
          <Select label="Year" value={filters.year} options={YEARS}
                  onChange={update("year")} allLabel="All years" />
          {hasActiveFilters && (
            <div className="flex items-center gap-3 text-xs sm:ml-auto sm:pb-2">
              <span className="flex items-center gap-1.5 text-gdf-muted">
                <span aria-hidden="true"
                      className="inline-block w-1.5 h-1.5 bg-gdf-teal rounded-full" />
                Filters active
              </span>
              <span aria-hidden="true" className="text-gdf-border">·</span>
              <button
                type="button"
                onClick={onReset}
                className="font-medium text-gdf-teal hover:underline"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function CompanyModal({ companyName, onClose }) {
  const [rounds, setRounds] = useState(null);
  const [fetchError, setFetchError] = useState(null);

  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    setRounds(null);
    setFetchError(null);
    const qs = new URLSearchParams({ search: companyName }).toString();
    fetch(`${API_URL}/deals?${qs}`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => {
        const target = companyName.trim().toLowerCase();
        const matched = (data.deals || []).filter(
          (d) => (d.company_name || "").trim().toLowerCase() === target
        );
        matched.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
        setRounds(matched);
      })
      .catch((err) => { if (err.name !== "AbortError") setFetchError(String(err)); });
    return () => ctrl.abort();
  }, [companyName]);

  const latest = rounds && rounds.length > 0 ? rounds[0] : null;
  const sector = latest?.sector || null;
  const country = rounds?.find((d) => d.country)?.country || null;
  const stage = latest?.stage || null;
  const websiteRaw = rounds?.find((d) => d.website)?.website || null;
  const websiteHref = websiteRaw
    ? (websiteRaw.startsWith("http") ? websiteRaw : `https://${websiteRaw}`)
    : null;

  const investors = useMemo(() => {
    if (!rounds) return [];
    const seen = new Map();
    const add = (name) => {
      const trimmed = (name || "").trim();
      if (!trimmed) return;
      const key = trimmed.toLowerCase();
      if (!seen.has(key)) seen.set(key, trimmed);
    };
    for (const d of rounds) {
      add(d.lead_investor);
      for (const co of (d.co_investors || "").split(",")) add(co);
    }
    return [...seen.values()].sort((a, b) => a.localeCompare(b));
  }, [rounds]);

  return (
    <div
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Company profile for ${companyName}`}
      className="fixed inset-0 z-50 flex items-center justify-center p-4
                 bg-black/85 backdrop-blur-sm"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-2xl max-h-[85vh] flex flex-col
                   bg-[#161b27] rounded-xl border border-gdf-teal/30
                   shadow-2xl overflow-hidden"
      >
        <header className="flex items-start justify-between gap-6 px-6 py-4
                           border-b border-gdf-border shrink-0">
          <div className="min-w-0">
            <h2 className="text-xl sm:text-2xl font-bold text-white truncate">
              {companyName}
            </h2>
            {sector && (
              <p className="text-xs text-gdf-muted mt-0.5">{sector}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close profile"
            className="w-8 h-8 flex items-center justify-center rounded shrink-0
                       text-gdf-muted hover:text-gdf-text hover:bg-gdf-border/40
                       transition-colors text-xl leading-none"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto">
          {rounds == null && !fetchError && (
            <div className="px-6 py-16 text-center text-sm text-gdf-muted">
              <span className="inline-block w-2 h-2 bg-gdf-teal rounded-full animate-pulse mr-2" />
              Loading company profile…
            </div>
          )}

          {fetchError && (
            <div className="px-6 py-16 text-center">
              <p className="text-red-400 text-sm font-medium">Failed to load company</p>
              <p className="text-gdf-muted text-xs mt-1">{fetchError}</p>
            </div>
          )}

          {rounds && rounds.length === 0 && !fetchError && (
            <div className="px-6 py-16 text-center text-sm text-gdf-muted">
              No rounds found for this company.
            </div>
          )}

          {rounds && rounds.length > 0 && (
            <>
              <div className="px-6 py-4 border-b border-gdf-border
                              flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
                <div className="flex items-baseline gap-1.5">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-gdf-muted">
                    Country
                  </span>
                  <span className="text-gdf-text">{country || "—"}</span>
                </div>
                <div className="flex items-baseline gap-1.5">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-gdf-muted">
                    Stage
                  </span>
                  <span className={`font-mono text-xs px-2 py-0.5 rounded border ${stageBadgeClass(stage)}`}>
                    {stage || "—"}
                  </span>
                </div>
                <div className="flex items-baseline gap-1.5 min-w-0">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-gdf-muted">
                    Website
                  </span>
                  {websiteHref ? (
                    <a
                      href={websiteHref}
                      target="_blank"
                      rel="noreferrer"
                      className="text-gdf-teal hover:underline truncate"
                    >
                      {websiteRaw}
                    </a>
                  ) : (
                    <span className="text-gdf-muted">—</span>
                  )}
                </div>
              </div>

              <section className="px-6 py-5 border-b border-gdf-border">
                <h3 className="text-[11px] font-semibold text-gdf-muted uppercase tracking-wider mb-3">
                  Funding History
                  <span className="ml-2 font-mono text-gdf-muted/70 normal-case tracking-normal">
                    {rounds.length} {rounds.length === 1 ? "round" : "rounds"}
                  </span>
                </h3>
                <ul className="divide-y divide-gdf-border/60 border border-gdf-border rounded-md overflow-hidden">
                  {rounds.map((d) => (
                    <li key={d.deal_id} className="px-4 py-3 flex items-center gap-3 text-sm">
                      <span className={`font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 w-20 text-center
                                       ${stageBadgeClass(d.stage)}`}>
                        {d.stage || "—"}
                      </span>
                      <span className="font-mono text-xs tabular-nums text-gdf-teal shrink-0 w-16 text-right">
                        {d.amount_usd != null ? formatAmount(d.amount_usd) : "—"}
                      </span>
                      <span className="font-mono text-[11px] text-gdf-muted shrink-0 w-20">
                        {d.date || "—"}
                      </span>
                      <span className="text-gdf-text truncate flex-1 min-w-0">
                        {d.lead_investor || <span className="text-gdf-muted">—</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="px-6 py-5">
                <h3 className="text-[11px] font-semibold text-gdf-muted uppercase tracking-wider mb-3">
                  Investors
                  <span className="ml-2 font-mono text-gdf-muted/70 normal-case tracking-normal">
                    {investors.length} unique
                  </span>
                </h3>
                {investors.length === 0 ? (
                  <p className="text-xs text-gdf-muted">No investors disclosed.</p>
                ) : (
                  <ul className="flex flex-wrap gap-1.5">
                    {investors.map((name) => (
                      <li
                        key={name}
                        className="font-mono text-[11px] text-gdf-text bg-gdf-surface
                                   border border-gdf-border rounded-md px-2 py-1"
                      >
                        {name}
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function isRecentDeal(dateStr) {
  if (!dateStr || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return false;
  const dealTime = Date.parse(dateStr);
  if (Number.isNaN(dealTime)) return false;
  const ageMs = Date.now() - dealTime;
  const SIXTY_DAYS = 60 * 24 * 60 * 60 * 1000;
  return ageMs >= 0 && ageMs <= SIXTY_DAYS;
}

function NewBadge() {
  return (
    <span
      className="font-mono text-[9px] uppercase tracking-wider
                 text-gdf-teal border border-gdf-teal/40 bg-gdf-teal/10
                 rounded-full px-1.5 py-0.5 leading-none shrink-0"
    >
      New
    </span>
  );
}

function DealRow({ deal, expanded, onToggle, onCompanyClick }) {
  const handleCompanyClick = (e) => {
    e.stopPropagation();
    if (deal.company_name) onCompanyClick?.(deal.company_name);
  };
  return (
    <>
      <tr
        onClick={onToggle}
        className={`border-b border-gdf-border cursor-pointer transition-colors
                    ${expanded ? "bg-gdf-panel" : "hover:bg-gdf-surface"}`}
      >
        <td className="px-4 py-3.5">
          <div className="flex items-center gap-2">
            <span className={`text-gdf-muted text-xs transition-transform inline-block w-3
                             ${expanded ? "rotate-90" : ""}`}>
              ▸
            </span>
            {deal.company_name ? (
              <button
                type="button"
                onClick={handleCompanyClick}
                className="font-medium text-left hover:text-gdf-teal hover:underline
                           decoration-gdf-teal/40 underline-offset-2 transition-colors
                           focus:outline-none focus:text-gdf-teal"
              >
                {deal.company_name}
              </button>
            ) : (
              <span className="font-medium">—</span>
            )}
            {isRecentDeal(deal.date) && <NewBadge />}
          </div>
        </td>
        <td className="px-4 py-3.5 text-gdf-muted">{deal.country || "—"}</td>
        <td className="px-4 py-3.5">
          <span className={`font-mono text-xs px-2 py-0.5 rounded border ${stageBadgeClass(deal.stage)}`}>
            {deal.stage || "—"}
          </span>
        </td>
        <td className="px-4 py-3.5 font-mono text-right tabular-nums">
          {deal.amount_usd != null ? (
            <span className="text-gdf-teal">{formatAmount(deal.amount_usd)}</span>
          ) : (
            <span className="text-gdf-muted">—</span>
          )}
        </td>
        <td className="px-4 py-3.5 text-gdf-muted">{deal.sector || "—"}</td>
        <td className="px-4 py-3.5 font-mono text-xs text-gdf-muted">{deal.date || "—"}</td>
        <td className="px-4 py-3.5 text-gdf-muted truncate max-w-[200px]">
          {deal.lead_investor || "—"}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-gdf-border bg-gdf-panel">
          <td colSpan={7} className="px-6 py-5">
            <DealDetails deal={deal} />
          </td>
        </tr>
      )}
    </>
  );
}

function DetailField({ label, children }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-semibold text-gdf-teal uppercase tracking-wider">
        {label}
      </span>
      <span className="text-sm text-gdf-text">
        {children || <span className="text-gdf-muted">—</span>}
      </span>
    </div>
  );
}

function DealDetails({ deal }) {
  const websiteHref = deal.website
    ? (deal.website.startsWith("http") ? deal.website : `https://${deal.website}`)
    : null;

  return (
    <div className="space-y-5">
      <DetailField label="Description">{deal.description}</DetailField>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
        <DetailField label="Lead Investor">{deal.lead_investor}</DetailField>
        <DetailField label="Investor Types">{deal.investor_types}</DetailField>
      </div>

      <DetailField label="Co-Investors">{deal.co_investors}</DetailField>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
        <DetailField label="Founded Year">{deal.founded_year}</DetailField>
        <DetailField label="Website">
          {websiteHref ? (
            <a href={websiteHref} target="_blank" rel="noreferrer"
               className="text-gdf-teal hover:underline break-all">
              {deal.website}
            </a>
          ) : null}
        </DetailField>
      </div>

      <DetailField label="Source">
        {deal.source && (
          <span className="text-gdf-muted text-xs leading-relaxed">{deal.source}</span>
        )}
      </DetailField>
    </div>
  );
}

function EmptyState({ hasActiveFilters, onReset }) {
  return (
    <div className="py-20 px-6 text-center">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="mx-auto w-10 h-10 text-gdf-border mb-4"
      >
        <circle cx="11" cy="11" r="7" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
        <line x1="8" y1="11" x2="14" y2="11" />
      </svg>
      <p className="text-gdf-text font-medium text-sm">No deals match your filters</p>
      <p className="text-gdf-muted text-xs mt-1.5">
        Try adjusting your search or clearing one of the filters.
      </p>
      {hasActiveFilters && (
        <button
          onClick={onReset}
          className="mt-5 text-xs text-gdf-text bg-gdf-surface hover:bg-gdf-panel
                     border border-gdf-border hover:border-gdf-teal rounded-md
                     px-4 py-2 transition-colors"
        >
          Reset filters
        </button>
      )}
    </div>
  );
}

function Pagination({ page, totalPages, onChange, pageSize, onPageSizeChange }) {
  // Hide when one page or fewer — nothing to paginate. (The per-page selector
  // is part of the pagination block too, so it disappears with single-page
  // results; users only need to change rows-per-page when there *are* enough
  // rows to span pages.)
  if (totalPages <= 1) return null;

  const buttonBase =
    "inline-flex items-center gap-1.5 text-xs font-medium " +
    "text-gdf-muted hover:text-gdf-teal " +
    "border border-gdf-border hover:border-gdf-teal/40 " +
    "rounded-md px-3 py-1.5 transition-colors " +
    "disabled:opacity-40 disabled:cursor-not-allowed " +
    "disabled:hover:text-gdf-muted disabled:hover:border-gdf-border";

  return (
    <nav
      aria-label="Pagination"
      className="mt-6 flex items-center justify-center gap-4 sm:gap-6 flex-wrap"
    >
      <button
        type="button"
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
        className={buttonBase}
        aria-label="Previous page"
      >
        <span aria-hidden="true">←</span>
        Previous
      </button>

      <span className="text-xs text-gdf-muted font-mono tabular-nums">
        Page <span className="text-gdf-text font-semibold">{page}</span> of {totalPages}
      </span>

      <button
        type="button"
        onClick={() => onChange(page + 1)}
        disabled={page >= totalPages}
        className={buttonBase}
        aria-label="Next page"
      >
        Next
        <span aria-hidden="true">→</span>
      </button>

      <label className="flex items-center gap-1.5 text-xs text-gdf-muted">
        <span className="sr-only">Rows per page</span>
        <select
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          aria-label="Rows per page"
          className="bg-gdf-surface border border-gdf-border rounded-md
                     px-2 py-1.5 text-xs text-gdf-text cursor-pointer
                     focus:outline-none focus:border-gdf-teal focus:ring-1 focus:ring-gdf-teal
                     transition-colors"
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>{n} per page</option>
          ))}
        </select>
      </label>
    </nav>
  );
}

function DealTable({ deals, loading, error, expandedId, setExpandedId, hasActiveFilters, onReset, sortState, onSort, onCompanyClick }) {
  // Resolve the sort indicator state for a given column.
  const stateFor = (col) =>
    sortState && sortState.column === col ? sortState.dir : "neutral";
  if (loading) {
    return (
      <div className="px-6 py-16 text-center text-gdf-muted">
        <div className="inline-block w-2 h-2 bg-gdf-accent rounded-full animate-pulse mr-2" />
        Loading deals…
      </div>
    );
  }
  if (error) {
    return (
      <div className="px-6 py-16 text-center">
        <p className="text-red-400 font-medium">Failed to load deals</p>
        <p className="text-gdf-muted text-sm mt-1">{error}</p>
        <p className="text-gdf-muted text-xs mt-3">
          Is the FastAPI backend running at <span className="font-mono">{API_URL}</span>?
        </p>
      </div>
    );
  }
  if (deals.length === 0) {
    return <EmptyState hasActiveFilters={hasActiveFilters} onReset={onReset} />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gdf-border bg-gdf-surface/60 text-left">
            <Th>Company</Th>
            <Th>Country</Th>
            <Th>Stage</Th>
            <Th
              align="right"
              sortable
              sortState={stateFor("amount_usd")}
              onSort={() => onSort && onSort("amount_usd")}
            >
              Amount
            </Th>
            <Th>Sector</Th>
            <Th
              sortable
              sortState={stateFor("date")}
              onSort={() => onSort && onSort("date")}
            >
              Date
            </Th>
            <Th>Lead Investor</Th>
          </tr>
        </thead>
        <tbody>
          {deals.map((deal) => (
            <DealRow
              key={deal.deal_id}
              deal={deal}
              expanded={expandedId === deal.deal_id}
              onToggle={() =>
                setExpandedId((cur) => (cur === deal.deal_id ? null : deal.deal_id))
              }
              onCompanyClick={onCompanyClick}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Three-state sort indicator. Teal up/down arrow for the active column,
// neutral up+down chevrons (muted) for unsorted sortable columns.
function SortIcon({ state }) {
  const common = {
    width: 10,
    height: 12,
    viewBox: "0 0 12 14",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true",
  };
  if (state === "asc") {
    return (
      <svg {...common} className="text-gdf-teal">
        <path d="M3 8l3-3 3 3" />
      </svg>
    );
  }
  if (state === "desc") {
    return (
      <svg {...common} className="text-gdf-teal">
        <path d="M3 6l3 3 3-3" />
      </svg>
    );
  }
  return (
    <svg {...common} className="text-gdf-border">
      <path d="M3 5l3-3 3 3" />
      <path d="M3 9l3 3 3-3" />
    </svg>
  );
}

function Th({ children, align = "left", sortable, sortState, onSort }) {
  const base =
    "px-4 py-3 text-[11px] font-semibold text-gdf-muted uppercase tracking-wider " +
    (align === "right" ? "text-right" : "");
  if (!sortable) {
    return <th className={base}>{children}</th>;
  }
  const ariaSort =
    sortState === "asc" ? "ascending" :
    sortState === "desc" ? "descending" :
    "none";
  return (
    <th className={base} aria-sort={ariaSort}>
      <button
        type="button"
        onClick={onSort}
        className={`inline-flex items-center gap-1.5 hover:text-gdf-text
                    focus:outline-none focus-visible:text-gdf-text
                    transition-colors ${align === "right" ? "" : ""}`}
      >
        {children}
        <SortIcon state={sortState} />
      </button>
    </th>
  );
}

export default function App() {
  const [tab, setTab] = useState("explorer");
  const [filters, setFilters] = useState({
    country: "", sector: "", stage: "", year: "",
  });
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // Explicit sort override. `column: null` means "no explicit sort" — the
  // table renders in the backend's default order (date desc, nulls last).
  const [sortState, setSortState] = useState({ column: null, dir: null });
  // Debounced version of `search` — only this value drives the API request,
  // so we don't fire a query on every keystroke. `search` itself feeds the
  // input so typing stays instantaneous.
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [sectors, setSectors] = useState([]);
  const [stats, setStats] = useState(null);
  const [deals, setDeals] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [companyModalName, setCompanyModalName] = useState(null);

  // 300 ms debounce: settle on the search term before hitting the API.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Pagination jumps back to page 1 whenever the filter set, search term,
  // or rows-per-page changes — otherwise users get stuck on a page that no
  // longer exists.
  useEffect(() => {
    setPage(1);
  }, [filters, debouncedSearch, pageSize]);

  // Fetch /stats once: feeds both the StatsBar and the sector dropdown.
  // Dropdown values are the normalised top-level categories — when the user
  // picks one, we filter deals client-side via normalizeSector(deal.sector).
  useEffect(() => {
    fetch(`${API_URL}/stats`)
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => {
        setStats(data);
        const rawKeys = (data.by_sector || [])
          .map((s) => s.key)
          .filter((k) => k && k !== "Unknown");
        const normalised = [...new Set(rawKeys.map(normalizeSector))].sort();
        setSectors(normalised);
        // eslint-disable-next-line no-console
        console.log(
          `[Sectors] Filter dropdown: ${rawKeys.length} raw sub-sectors → ${normalised.length} canonical categories`
        );
      })
      .catch(() => {
        setStats(null);
        setSectors([]);
      });
  }, []);

  // Refetch deals on any filter or debounced-search change. Skip while a
  // non-explorer tab is visible — those rows aren't rendered. The sector
  // filter is stripped from the backend call and applied client-side after
  // normalisation, because the backend stores raw sub-sector strings.
  useEffect(() => {
    if (tab !== "explorer") return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const { sector: _ignoredSector, ...backendFilters } = filters;
    const params = { ...backendFilters, search: debouncedSearch };
    fetch(`${API_URL}/deals${buildQuery(params)}`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data) => {
        setDeals(data.deals || []);
        setExpandedId(null);
      })
      .catch((err) => {
        if (err.name !== "AbortError") setError(String(err));
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [filters, debouncedSearch, tab]);

  const resetFilters = () => {
    setFilters({ country: "", sector: "", stage: "", year: "" });
    setSearch("");
  };

  // Triggered from an InvestorCard "View deals →" — drop the user into the
  // Deal Explorer with the search prefilled to the investor's name. The
  // client-side search already matches against `lead_investor`, so the table
  // narrows to that fund's deals on arrival.
  const viewDealsForInvestor = (investorName) => {
    setFilters({ country: "", sector: "", stage: "", year: "" });
    setSearch(investorName);
    setTab("explorer");
  };

  // Triggered from the Dashboard geography map — drop the user into Deal
  // Explorer with the Country filter pre-set.
  const viewDealsForCountry = (countryName) => {
    setFilters({ country: countryName, sector: "", stage: "", year: "" });
    setSearch("");
    setTab("explorer");
  };

  const hasActiveFilters =
    Object.values(filters).some((v) => v !== "") || search.trim() !== "";

  // Three-state cycle: unsorted → asc → desc → unsorted. Clicking a
  // different column from the active one starts at asc on that column.
  const handleSort = (column) => {
    setSortState((prev) => {
      if (prev.column !== column) return { column, dir: "asc" };
      if (prev.dir === "asc")     return { column, dir: "desc" };
      return { column: null, dir: null };  // back to default
    });
  };

  // Sector filter is applied client-side: the dropdown lists canonical
  // categories ("Fintech", "B2B SaaS", …) but the DB stores raw sub-sectors,
  // so we project each deal's sector through normalizeSector before matching.
  const sectorFilteredDeals = useMemo(() => {
    if (!filters.sector) return deals;
    return deals.filter((d) => normalizeSector(d.sector) === filters.sector);
  }, [deals, filters.sector]);

  // Apply the user's explicit sort on top of the server's default order.
  // Nulls / empties always sink to the bottom, regardless of direction.
  const sortedDeals = useMemo(() => {
    if (!sortState.column) return sectorFilteredDeals;
    const col = sortState.column;
    const mult = sortState.dir === "asc" ? 1 : -1;
    const sorted = [...sectorFilteredDeals];
    sorted.sort((a, b) => {
      const av = a[col];
      const bv = b[col];
      const aNull = av == null || av === "";
      const bNull = bv == null || bv === "";
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      if (av < bv) return -1 * mult;
      if (av > bv) return  1 * mult;
      return 0;
    });
    return sorted;
  }, [sectorFilteredDeals, sortState]);

  // Pagination derived values. `safePage` defends against the page being out
  // of range during the brief window between a filter change and the page
  // reset effect firing.
  const totalPages = Math.max(1, Math.ceil(sortedDeals.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const startIdx = (safePage - 1) * pageSize;
  const endIdx = Math.min(startIdx + pageSize, sortedDeals.length);
  const paginatedDeals = sortedDeals.slice(startIdx, endIdx);

  const countLabel = useMemo(() => {
    if (loading) return "Loading…";
    if (error) return "—";
    const total = sectorFilteredDeals.length;
    if (total === 0) return "0 deals";
    if (total === 1) return "1 of 1 deal";
    return `${startIdx + 1}–${endIdx} of ${total} deals`;
  }, [loading, error, sectorFilteredDeals.length, startIdx, endIdx]);

  return (
    <div className="min-h-full flex flex-col overflow-x-clip">
      <Header />
      <Nav tab={tab} setTab={setTab} />
      <StatsBar stats={stats} />

      {tab === "explorer" && (
        <>
          <FilterBar
            filters={filters}
            setFilters={setFilters}
            sectors={sectors}
            onReset={resetFilters}
            search={search}
            setSearch={setSearch}
            hasActiveFilters={hasActiveFilters}
          />
          <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
            <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
              <h2 className="text-sm text-gdf-muted">
                Showing <span className="text-gdf-text font-semibold">{countLabel}</span>
              </h2>
              <div className="flex items-center gap-4">
                <p className="hidden lg:block text-[11px] text-gdf-muted font-mono uppercase tracking-wider">
                  Click a row to expand
                </p>
                <ExportButton deals={sortedDeals} />
              </div>
            </div>

            <div className="border border-gdf-border rounded-lg bg-gdf-bg overflow-hidden">
              <DealTable
                deals={paginatedDeals}
                loading={loading}
                error={error}
                expandedId={expandedId}
                setExpandedId={setExpandedId}
                hasActiveFilters={hasActiveFilters}
                onReset={resetFilters}
                sortState={sortState}
                onSort={handleSort}
                onCompanyClick={setCompanyModalName}
              />
            </div>

            <Pagination
              page={safePage}
              totalPages={totalPages}
              onChange={setPage}
              pageSize={pageSize}
              onPageSizeChange={setPageSize}
            />
          </main>
        </>
      )}

      {tab === "dashboard" && (
        <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
          <Dashboard stats={stats} onCountryClick={viewDealsForCountry} />
        </main>
      )}

      {tab === "insights" && (
        <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
          <Insights />
        </main>
      )}

      {tab === "investors" && (
        <InvestorDirectory onViewDeals={viewDealsForInvestor} />
      )}

      {tab === "about" && (
        <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-8 sm:py-12">
          <About stats={stats} />
        </main>
      )}

      {companyModalName && (
        <CompanyModal
          companyName={companyModalName}
          onClose={() => setCompanyModalName(null)}
        />
      )}

      <footer className="border-t border-gdf-border mt-auto">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-4 text-[11px] text-gdf-muted font-mono
                        flex flex-col sm:flex-row sm:justify-between gap-1 sm:gap-4">
          <span>GulfDealFlow — GCC Venture Intelligence</span>
          <span>Data last updated: 15 May 2026</span>
          <span className="break-all">API · {API_URL}</span>
        </div>
      </footer>
    </div>
  );
}
