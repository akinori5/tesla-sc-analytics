#!/usr/bin/env python3
"""
Tesla Supercharger Analytics — CSV to Dashboard

Generates an interactive HTML dashboard from Tesla's official charging CSV exports.
No API keys, no account setup — just your data, visualized locally.

Usage:
    python supercharger_analytics.py charging-history*.csv
    python supercharger_analytics.py charging-history*.csv -o my_dashboard
    python supercharger_analytics.py charging-history*.csv --tz America/New_York
"""

import argparse
import csv
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path


# ─── CSV Import ────────────────────────────────────────────────────

def import_csv(csv_paths: list[Path], display_tz: str = "Europe/London") -> list[dict]:
    """Read charging records from Tesla official CSV exports."""
    from zoneinfo import ZoneInfo

    all_records = []
    tz = ZoneInfo(display_tz)

    for csv_path in csv_paths:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                record = _parse_csv_row(row, tz)
                if record:
                    all_records.append(record)

    print(f"  CSV: {len(all_records)} records from {len(csv_paths)} file(s)")
    return all_records


def _parse_csv_row(row: dict, tz) -> dict | None:
    """Convert a single CSV row to a charging record."""
    dt_str = row.get("ChargeStartDateTime", "")
    if not dt_str:
        return None

    try:
        dt = datetime.fromisoformat(dt_str).astimezone(tz)
    except ValueError:
        return None

    location = row.get("SiteLocationName", "").strip()
    if not location:
        return None

    # kWh
    kwh = None
    qty = row.get("QuantityBase", "")
    if qty and qty != "N/A":
        try:
            kwh = float(qty.replace(" kwh", "").strip())
        except ValueError:
            pass

    # Unit cost
    unit_cost = None
    uc = row.get("UnitCostBase", "")
    if uc and uc != "N/A":
        try:
            unit_cost = float(uc.replace("/kwh", "").strip())
        except ValueError:
            pass

    # Total cost
    total = None
    total_str = row.get("Total Inc. VAT", "")
    if total_str:
        try:
            total = float(total_str)
        except ValueError:
            pass

    # Currency from country code
    country_code = row.get("Country", "")
    currency_map = {"GB": "\u00a3", "CH": "CHF ", "US": "$", "NO": "NOK ", "DK": "DKK ", "SE": "SEK "}
    currency = currency_map.get(country_code, "\u20ac")
    cost_str = f"{currency}{total:.2f}" if total is not None else f"{currency}0.00"

    # Charge type
    desc = row.get("Description", "")
    if "PARKING" in desc:
        charge_type = "Parking"
    elif "IDLE" in desc:
        charge_type = "Supercharging & Idle Fee"
    else:
        charge_type = "Supercharging"

    return {
        "location": location,
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M"),
        "cost": cost_str,
        "type": charge_type,
        "kwh": kwh,
        "unit_cost": unit_cost,
        "country_code": country_code,
        "source": "csv",
    }


# ─── Normalize & Deduplicate ──────────────────────────────────────

def normalize_location(location: str) -> str:
    """Normalize location name for consistency."""
    n = location.replace("\xa0", " ")
    n = re.sub(r"\s{2,}", " ", n)
    n = n.replace(" - ", " \u2013 ")
    return n


def deduplicate(records: list[dict]) -> list[dict]:
    """Remove duplicate records, keeping the richest data."""
    by_key = {}
    for r in records:
        key = (r["location"], r["date"], r["time"])
        if key not in by_key:
            by_key[key] = r
        else:
            existing = by_key[key]
            for field in ("kwh", "unit_cost", "country_code"):
                if r.get(field) is not None and existing.get(field) is None:
                    existing[field] = r[field]

    result = list(by_key.values())
    result.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
    return result


# ─── Coordinates (supercharge.info) ───────────────────────────────

SUPERCHARGE_INFO_URL = "https://supercharge.info/service/supercharge/allSites"


def _normalize_for_match(name: str) -> str:
    """Normalize location name for fuzzy matching."""
    s = name.replace("\u2013", "-").replace("\u2014", "-").lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"\(.*?\)", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def fetch_supercharger_db(cache_path: Path) -> list[dict]:
    """Fetch all Supercharger sites from supercharge.info (with caching)."""
    import urllib.request
    import time

    if cache_path.exists():
        age_days = (time.time() - cache_path.stat().st_mtime) / 86400
        if age_days < 7:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)

    print("  Fetching Supercharger database from supercharge.info...")
    req = urllib.request.Request(
        SUPERCHARGE_INFO_URL,
        headers={"User-Agent": "TeslaSuperchargerAnalytics/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"    -> {len(data)} sites cached")
        return data
    except Exception as e:
        print(f"    -> Failed: {e}", file=sys.stderr)
        if cache_path.exists():
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        return []


def resolve_coords(locations: list[str], sites: list[dict], coords_path: Path) -> dict:
    """Match location names to coordinates via supercharge.info data."""
    from difflib import SequenceMatcher

    # Build lookup
    sc_lookup = {}
    for site in sites:
        name = site.get("name", "")
        gps = site.get("gps", {})
        lat, lng = gps.get("latitude"), gps.get("longitude")
        if name and lat is not None and lng is not None:
            sc_lookup[_normalize_for_match(name)] = [lat, lng]

    sc_keys = list(sc_lookup.keys())

    # Load existing cache
    cached = {}
    if coords_path.exists():
        with open(coords_path, encoding="utf-8") as f:
            cached = json.load(f)

    resolved = {}
    for loc in locations:
        norm = _normalize_for_match(loc)

        # 1. Exact match
        if norm in sc_lookup:
            resolved[loc] = sc_lookup[norm]
            continue

        # 2. Cached
        if loc in cached:
            resolved[loc] = cached[loc]
            continue

        # 3. City prefix match
        city_country = _normalize_for_match(loc.split("\u2013")[0].split("-")[0].strip())
        city_matches = [k for k in sc_keys if k.startswith(city_country)]
        if city_matches:
            resolved[loc] = sc_lookup[city_matches[0]]
            continue

        # 4. Fuzzy match (>= 0.65)
        best = max(((SequenceMatcher(None, norm, k).ratio(), k) for k in sc_keys),
                    key=lambda x: x[0], default=(0, ""))
        if best[0] >= 0.65:
            resolved[loc] = sc_lookup[best[1]]
            continue

        # 5. Word overlap match
        norm_words = set(norm.replace(",", " ").replace("-", " ").split())
        for k in sc_keys:
            k_words = set(k.replace(",", " ").replace("-", " ").split())
            overlap = len(norm_words & k_words) / max(min(len(norm_words), len(k_words)), 1)
            if overlap >= 0.6:
                resolved[loc] = sc_lookup[k]
                break

    # Save cache
    with open(coords_path, "w", encoding="utf-8") as f:
        json.dump(resolved, f, ensure_ascii=False, indent=2)

    matched = len(resolved)
    unmatched = len(locations) - matched
    print(f"    -> {matched} locations mapped" + (f", {unmatched} unresolved" if unmatched else ""))
    return resolved


# ─── Closed charger detection ─────────────────────────────────────

def build_closed_set(records: list[dict], sites: list[dict]) -> set:
    """Identify chargers that have been used but are now closed."""
    sc_status = {}
    for site in sites:
        sc_status[_normalize_for_match(site.get("name", ""))] = site.get("status", "")
    closed = set()
    for loc in set(r["location"] for r in records):
        status = sc_status.get(_normalize_for_match(loc), "")
        if status in ("CLOSED_PERM", "CLOSED_TEMP"):
            closed.add(loc)
    return closed


# ─── HTML Report ──────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Charging History & Insights</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #08080a; --surface: rgba(255,255,255,0.03); --surface2: rgba(255,255,255,0.06);
    --border: rgba(255,255,255,0.08); --border-hover: rgba(255,255,255,0.14);
    --text: #e8e8ec; --text2: #7a7a88;
    --red: #e31937; --red2: #ff3355; --blue: #4d94ff; --green: #34d399;
    --purple: #a78bfa; --amber: #fbbf24; --cyan: #22d3ee;
    --glow-red: rgba(227,25,55,0.15); --glow-blue: rgba(77,148,255,0.12);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    background-image:
      radial-gradient(ellipse 80% 60% at 50% -20%, rgba(227,25,55,0.06), transparent),
      radial-gradient(ellipse 60% 40% at 80% 100%, rgba(77,148,255,0.04), transparent);
    min-height: 100vh;
  }

  .header {
    background: linear-gradient(180deg, rgba(227,25,55,0.08) 0%, transparent 100%);
    padding: 40px 48px 32px;
    border-bottom: 1px solid var(--border);
    position: relative;
  }
  .header::after {
    content: '';
    position: absolute; bottom: -1px; left: 48px; right: 48px; height: 1px;
    background: linear-gradient(90deg, var(--red), transparent 40%);
  }
  .header h1 {
    font-family: 'Outfit', sans-serif;
    font-size: 26px; font-weight: 700; letter-spacing: -0.5px;
    display: flex; align-items: center; gap: 14px;
  }
  .header h1 .tesla-t {
    color: var(--red); font-size: 34px; font-weight: 800;
    text-shadow: 0 0 30px var(--glow-red);
  }
  .header p { color: var(--text2); margin-top: 8px; font-size: 13px; letter-spacing: 0.3px; }

  .container { max-width: 1440px; margin: 0 auto; padding: 28px; }

  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px; padding: 20px 22px;
    backdrop-filter: blur(12px);
    transition: border-color 0.3s, box-shadow 0.3s;
    animation: fadeUp 0.5s ease both;
  }
  .kpi:hover { border-color: var(--border-hover); box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
  .kpi .label {
    font-family: 'Outfit', sans-serif;
    font-size: 11px; color: var(--text2); text-transform: uppercase;
    letter-spacing: 1.2px; font-weight: 500;
  }
  .kpi .value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 30px; font-weight: 700; margin-top: 6px;
    letter-spacing: -1px;
  }
  .kpi .sub { font-size: 11.5px; color: var(--text2); margin-top: 4px; letter-spacing: 0.2px; }
  .kpi.red .value { color: var(--red2); text-shadow: 0 0 40px var(--glow-red); }
  .kpi.blue .value { color: var(--blue); text-shadow: 0 0 40px var(--glow-blue); }
  .kpi.green .value { color: var(--green); }
  .kpi.purple .value { color: var(--purple); }
  .kpi.amber .value { color: var(--amber); }
  .kpi.cyan .value { color: var(--cyan); }
  .kpi:nth-child(2) { animation-delay: 0.05s; }
  .kpi:nth-child(3) { animation-delay: 0.1s; }
  .kpi:nth-child(4) { animation-delay: 0.15s; }
  .kpi:nth-child(5) { animation-delay: 0.2s; }
  .kpi:nth-child(6) { animation-delay: 0.25s; }
  .kpi:nth-child(7) { animation-delay: 0.3s; }
  .kpi:nth-child(8) { animation-delay: 0.35s; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media (max-width: 1024px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }
  .full { margin-bottom: 16px; }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px; padding: 24px;
    backdrop-filter: blur(12px);
    transition: border-color 0.3s;
    animation: fadeUp 0.6s ease both;
  }
  .card:hover { border-color: var(--border-hover); }
  .card h2 {
    font-family: 'Outfit', sans-serif;
    font-size: 15px; font-weight: 600; margin-bottom: 18px;
    display: flex; align-items: center; gap: 10px;
    letter-spacing: -0.2px; color: var(--text);
  }
  .card h2 .dot {
    width: 7px; height: 7px; border-radius: 50%;
    box-shadow: 0 0 8px currentColor;
  }

  #map { height: 100%; min-height: 500px; border-radius: 12px; border: 1px solid var(--border); }

  .bar-list { list-style: none; }
  .bar-list li { margin-bottom: 10px; }
  .bar-list .bar-label {
    display: flex; justify-content: space-between;
    font-size: 12.5px; margin-bottom: 4px;
    font-family: 'DM Sans', sans-serif;
  }
  .bar-list .bar-label span:last-child {
    color: var(--text2);
    font-family: 'JetBrains Mono', monospace; font-size: 11.5px;
  }
  .bar-list .bar-track { height: 5px; background: var(--surface2); border-radius: 3px; overflow: hidden; }
  .bar-list .bar-fill { height: 100%; border-radius: 3px; transition: width 0.8s cubic-bezier(0.16,1,0.3,1); }

  .heatmap-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  .heatmap-table th {
    padding: 6px 4px; color: var(--text2); font-weight: 500;
    font-family: 'JetBrains Mono', monospace; font-size: 10px;
  }
  .heatmap-table td { padding: 4px; text-align: center; border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 10px; }

  .trip-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 1024px) { .trip-layout { grid-template-columns: 1fr; } }
  .trip-list { max-height: 500px; overflow-y: auto; }
  .trip-list::-webkit-scrollbar { width: 3px; }
  .trip-list::-webkit-scrollbar-track { background: transparent; }
  .trip-list::-webkit-scrollbar-thumb { background: var(--border-hover); border-radius: 2px; }
  .trip {
    padding: 12px 14px; border-bottom: 1px solid var(--border);
    font-size: 13px; cursor: pointer; border-radius: 8px;
    transition: all 0.2s ease;
  }
  .trip:hover { background: var(--surface2); transform: translateX(2px); }
  .trip.active { background: var(--surface2); border-left: 3px solid var(--cyan); }
  .trip .trip-title { font-family: 'Outfit', sans-serif; font-weight: 600; color: var(--cyan); }
  .trip .trip-meta { color: var(--text2); margin-top: 3px; font-size: 12px; }
  #tripMap { height: 500px; border-radius: 12px; border: 1px solid var(--border); }

  .chart-container { position: relative; height: 280px; }
  .chart-container-sm { position: relative; height: 240px; }

  .country-group { margin-bottom: 18px; }
  .country-group h3 {
    font-family: 'Outfit', sans-serif;
    font-size: 13px; font-weight: 600; margin-bottom: 10px;
    display: flex; align-items: center; gap: 8px;
  }
  .country-group h3 .cdot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; box-shadow: 0 0 6px currentColor; }
  .ot-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .ot-tag {
    font-size: 12px; font-family: 'DM Sans', sans-serif;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 5px 12px;
    color: var(--text2); cursor: pointer;
    transition: all 0.2s ease;
  }
  .ot-tag:hover { border-color: var(--text2); color: var(--text); transform: translateY(-1px); }
  .ot-tag.active { background: var(--blue); border-color: var(--blue); color: #fff; box-shadow: 0 0 12px var(--glow-blue); }
  #otMap { height: 400px; border-radius: 12px; border: 1px solid var(--border); margin-top: 18px; }

  .grid-map-side { display: grid; grid-template-columns: 1fr 380px; gap: 16px; margin-bottom: 16px; }
  .grid-map-side > .card { display: flex; flex-direction: column; }
  .grid-map-side > .card #map { flex: 1; }
  .side-charts { display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 1024px) { .grid-map-side { grid-template-columns: 1fr; } #map { min-height: 70vh; } }

  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .leaflet-container { background: #0d0d12 !important; }
  .leaflet-control-zoom a {
    background: rgba(20,20,25,0.9) !important; color: var(--text) !important;
    border-color: var(--border) !important; backdrop-filter: blur(8px);
  }
  .leaflet-control-zoom a:hover { background: rgba(40,40,50,0.9) !important; }
  .leaflet-control-attribution { opacity: 0.4; font-size: 10px !important; }
</style>
</head>
<body>
<div class="header">
  <h1><span class="tesla-t">T</span> My Charging History & Insights</h1>
  <p id="subtitle"></p>
</div>
<div class="container">
  <div class="kpi-grid" id="kpis"></div>
  <div class="grid-map-side">
    <div class="card">
      <h2><span class="dot" style="background:var(--red)"></span>Charger Map &mdash; All Locations</h2>
      <div id="map"></div>
    </div>
    <div class="side-charts">
      <div class="card"><h2><span class="dot" style="background:var(--green)"></span>Top 15 Locations</h2><ul class="bar-list" id="topLocations"></ul></div>
      <div class="card"><h2><span class="dot" style="background:var(--purple)"></span>By Country</h2><div class="chart-container-sm"><canvas id="countryChart"></canvas></div></div>
      <div class="card"><h2><span class="dot" style="background:var(--red)"></span>Yearly Breakdown</h2><div class="chart-container-sm"><canvas id="yearlyChart"></canvas></div></div>
    </div>
  </div>
  <div class="full"><div class="card"><h2><span class="dot" style="background:var(--blue)"></span>Monthly Charging Frequency</h2><div class="chart-container"><canvas id="monthlyChart"></canvas></div></div></div>
  <div class="grid-2" id="kwhSection" style="display:none">
    <div class="card"><h2><span class="dot" style="background:var(--green)"></span>Monthly kWh Charged</h2><div class="chart-container"><canvas id="monthlyKwhChart"></canvas></div></div>
    <div class="card"><h2><span class="dot" style="background:var(--amber)"></span>Average kWh per Session &amp; Unit Cost</h2><div class="chart-container"><canvas id="avgKwhChart"></canvas></div></div>
  </div>
  <div class="grid-3">
    <div class="card"><h2><span class="dot" style="background:var(--amber)"></span>By Day of Week</h2><div class="chart-container-sm"><canvas id="dowChart"></canvas></div></div>
    <div class="card"><h2><span class="dot" style="background:var(--cyan)"></span>By Hour of Day</h2><div class="chart-container-sm"><canvas id="hourChart"></canvas></div></div>
    <div class="card"><h2><span class="dot" style="background:var(--amber)"></span>Day &times; Hour Heatmap</h2><div id="heatmap"></div></div>
  </div>
  <div style="display:none">
    <div class="card"><canvas id="currencyChart"></canvas></div>
  </div>
  <div class="full">
    <div class="card">
      <h2><span class="dot" style="background:var(--cyan)"></span>Detected Road Trips</h2>
      <div class="trip-layout">
        <div class="trip-list" id="trips"></div>
        <div id="tripMap"></div>
      </div>
    </div>
  </div>
  <div class="full">
    <div class="card">
      <h2><span class="dot" style="background:var(--text2)"></span>One-Time Chargers</h2>
      <div id="oneTimers"></div>
      <div id="otMap"></div>
    </div>
  </div>
</div>
<script>
const RAW = %%DATA%%;
const COORDS = %%COORDS%%;
const CLOSED = new Set(%%CLOSED%%);

function getCountry(loc) {
  if (loc.includes("UK")) return "UK";
  if (loc.includes("Spain")) return "Spain";
  if (loc.includes("France")) return "France";
  if (loc.includes("Italy")) return "Italy";
  if (loc.includes("Switzerland")) return "Switzerland";
  if (loc.includes("Belgium")) return "Belgium";
  return "Other";
}
const CC = { UK:"#3b82f6", Spain:"#e31937", France:"#8b5cf6", Italy:"#22c55e", Switzerland:"#f59e0b", Belgium:"#06b6d4" };
const CF = { UK:"\u{1F1EC}\u{1F1E7}", Spain:"\u{1F1EA}\u{1F1F8}", France:"\u{1F1EB}\u{1F1F7}", Italy:"\u{1F1EE}\u{1F1F9}", Switzerland:"\u{1F1E8}\u{1F1ED}", Belgium:"\u{1F1E7}\u{1F1EA}" };
const DOW=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const data = RAW.map(r => {
  const dt = new Date(r.date+"T"+r.time);
  return {...r, dt, country:getCountry(r.location), dow:dt.getDay(), hour:dt.getHours(), month:r.date.slice(0,7), year:r.date.slice(0,4)};
});

const uniqueLocs = new Set(data.map(d=>d.location)).size;
const countries = new Set(data.map(d=>d.country)).size;
const first = data[data.length-1].date, last = data[0].date;
const days = Math.round((new Date(last)-new Date(first))/864e5);
const avg = (data.length/(days/30.44)).toFixed(1);
document.getElementById("subtitle").textContent = `Tesla Supercharger · ${first} to ${last} · Generated ${new Date().toISOString().slice(0,10)}`;

const yc={}; data.forEach(d=>yc[d.year]=(yc[d.year]||0)+1);
const topYear = Object.entries(yc).sort((a,b)=>b[1]-a[1])[0];
const lc2={}; data.forEach(d=>{const k=d.location.replace(/ –.*/,"").replace(/ \(.*/,""); lc2[k]=(lc2[k]||0)+1;});
const topLoc = Object.entries(lc2).sort((a,b)=>b[1]-a[1])[0];

document.getElementById("kpis").innerHTML = [
  {l:"Total Sessions",v:data.length,s:`${first} ~ ${last}`,c:"red"},
  {l:"Unique Locations",v:uniqueLocs,s:(()=>{const lbc={};data.forEach(d=>{if(!lbc[d.country])lbc[d.country]=new Set();lbc[d.country].add(d.location)});return Object.entries(lbc).sort((a,b)=>b[1].size-a[1].size).map(([c,s])=>`${CF[c]||""} ${c} ${s.size}`).join(" · ")})(),c:"blue"},
  {l:"Time Span",v:Math.round(days/365*10)/10+"y",s:days+" days",c:"green"},
  {l:"Avg / Month",v:avg,s:"sessions per month",c:"purple"},
  {l:"Most Active Year",v:topYear[0],s:topYear[1]+" sessions",c:"amber"},
  {l:"Most Used Charger",v:topLoc[0].split(",")[0],s:topLoc[1]+" times",c:"cyan"},
].concat(data.some(d=>d.kwh)?[
  {l:"Total Energy",v:(data.reduce((s,d)=>s+(d.kwh||0),0)/1000).toFixed(1)+"MWh",s:Math.round(data.reduce((s,d)=>s+(d.kwh||0),0))+" kWh · avg "+Math.round(data.reduce((s,d)=>s+(d.kwh||0),0)/data.filter(d=>d.kwh).length)+" kWh/session",c:"green"},
]:[]).map(k=>`<div class="kpi ${k.c}"><div class="label">${k.l}</div><div class="value">${k.v}</div><div class="sub">${k.s}</div></div>`).join("");

// Map
const map = L.map("map").setView([47,2],5);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{attribution:'OpenStreetMap CARTO',maxZoom:18}).addTo(map);
const locCounts={}; data.forEach(d=>locCounts[d.location]=(locCounts[d.location]||0)+1);
const locLatest={}; data.forEach(d=>{const key=d.location;const dt=d.date+" "+d.time;if(!locLatest[key]||dt>locLatest[key])locLatest[key]=dt});
Object.entries(locCounts).forEach(([loc,cnt])=>{
  const c=COORDS[loc]; if(!c)return;
  const color=CC[getCountry(loc)]||"#999";
  const latest=locLatest[loc]||"";
  const closed=CLOSED.has(loc);
  const label=closed?`\u26d4 ${loc}`:loc;
  const badge=closed?`<br><span style="color:#f87171">Permanently Closed</span>`:"";
  L.circleMarker(c,{radius:Math.min(4+Math.sqrt(cnt)*3,20),fillColor:closed?"#666":color,color:closed?"#f87171":"#fff",weight:closed?2:1,fillOpacity:closed?0.4:0.8}).bindPopup(`<b>${label}</b>${badge}<br>${cnt} session${cnt>1?"s":""}<br><small>Last: ${latest}</small>`).addTo(map);
});

// Monthly
const mc={}; data.forEach(d=>mc[d.month]=(mc[d.month]||0)+1);
const mk=Object.keys(mc).sort();
new Chart(document.getElementById("monthlyChart"),{type:"bar",data:{labels:mk.map(m=>{const[y,mo]=m.split("-");return MON[+mo-1]+" '"+y.slice(2)}),datasets:[{data:mk.map(k=>mc[k]),backgroundColor:"rgba(59,130,246,0.6)",borderColor:"#3b82f6",borderWidth:1,borderRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#888",font:{size:9},maxRotation:60},grid:{display:false}},y:{ticks:{color:"#888"},grid:{color:"#333"}}}}});

// kWh charts (only if CSV data exists)
const hasKwh=data.some(d=>d.kwh!=null);
if(hasKwh){
  document.getElementById("kwhSection").style.display="";
  // Monthly kWh
  const mkwh={};data.forEach(d=>{if(d.kwh)mkwh[d.month]=(mkwh[d.month]||0)+d.kwh});
  new Chart(document.getElementById("monthlyKwhChart"),{type:"bar",data:{labels:mk.map(m=>{const[y,mo]=m.split("-");return MON[+mo-1]+" '"+y.slice(2)}),datasets:[{data:mk.map(k=>Math.round(mkwh[k]||0)),backgroundColor:"rgba(34,197,94,0.6)",borderColor:"#22c55e",borderWidth:1,borderRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#888",font:{size:9},maxRotation:60},grid:{display:false}},y:{ticks:{color:"#888",callback:v=>v+"kWh"},grid:{color:"#333"}}}}});
  // Avg kWh per session + unit cost dual axis
  const mavg={};const mcnt2={};const muc={};const muc_n={};
  data.forEach(d=>{if(d.kwh){mavg[d.month]=(mavg[d.month]||0)+d.kwh;mcnt2[d.month]=(mcnt2[d.month]||0)+1}if(d.unit_cost){muc[d.month]=(muc[d.month]||0)+d.unit_cost;muc_n[d.month]=(muc_n[d.month]||0)+1}});
  new Chart(document.getElementById("avgKwhChart"),{type:"bar",data:{labels:mk.map(m=>{const[y,mo]=m.split("-");return MON[+mo-1]+" '"+y.slice(2)}),datasets:[{label:"Avg kWh",data:mk.map(k=>mcnt2[k]?Math.round(mavg[k]/mcnt2[k]*10)/10:null),backgroundColor:"rgba(245,158,11,0.5)",borderRadius:3,yAxisID:"y"},{label:"Avg €/kWh",data:mk.map(k=>muc_n[k]?Math.round(muc[k]/muc_n[k]*1000)/1000:null),type:"line",borderColor:"#06b6d4",pointRadius:2,borderWidth:2,yAxisID:"y1"}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:"#ccc",font:{size:10}}}},scales:{x:{ticks:{color:"#888",font:{size:9},maxRotation:60},grid:{display:false}},y:{position:"left",ticks:{color:"#f59e0b"},grid:{color:"#333"}},y1:{position:"right",ticks:{color:"#06b6d4"},grid:{display:false}}}}});
}

// Top locations
const la=Object.entries(locCounts).sort((a,b)=>b[1]-a[1]).slice(0,15);
const mx=la[0][1];
document.getElementById("topLocations").innerHTML=la.map(([l,c])=>{const color=CC[getCountry(l)]||"#999";return`<li><div class="bar-label"><span>${l.length>40?l.slice(0,38)+"…":l}</span><span>${c}</span></div><div class="bar-track"><div class="bar-fill" style="width:${c/mx*100}%;background:${color}"></div></div></li>`}).join("");

// Country
const cc2={}; data.forEach(d=>cc2[d.country]=(cc2[d.country]||0)+1);
const ce=Object.entries(cc2).sort((a,b)=>b[1]-a[1]);
new Chart(document.getElementById("countryChart"),{type:"doughnut",data:{labels:ce.map(e=>e[0]),datasets:[{data:ce.map(e=>e[1]),backgroundColor:ce.map(e=>CC[e[0]]||"#666"),borderColor:"#1a1a1e",borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"right",labels:{color:"#ccc",font:{size:11},padding:10}}}}});

// Day of week
const dc=Array(7).fill(0); data.forEach(d=>dc[d.dow]++);
new Chart(document.getElementById("dowChart"),{type:"bar",data:{labels:DOW,datasets:[{data:dc,backgroundColor:DOW.map((_,i)=>i===0||i===6?"rgba(245,158,11,0.8)":"rgba(245,158,11,0.5)"),borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#999"},grid:{display:false}},y:{ticks:{color:"#999"},grid:{color:"#333"}}}}});

// Hour
const hc=Array(24).fill(0); data.forEach(d=>hc[d.hour]++);
new Chart(document.getElementById("hourChart"),{type:"bar",data:{labels:Array.from({length:24},(_,i)=>i+"h"),datasets:[{data:hc,backgroundColor:hc.map((_,i)=>i>=6&&i<=10?"rgba(6,182,212,0.8)":i>=17&&i<=21?"rgba(6,182,212,0.7)":"rgba(6,182,212,0.35)"),borderRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#999",font:{size:9}},grid:{display:false}},y:{ticks:{color:"#999"},grid:{color:"#333"}}}}});

// Yearly
const yk=Object.keys(yc).sort();
new Chart(document.getElementById("yearlyChart"),{type:"bar",data:{labels:yk,datasets:[{data:yk.map(y=>yc[y]),backgroundColor:"rgba(59,130,246,0.6)",borderColor:"#3b82f6",borderWidth:1,borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,indexAxis:"y",plugins:{legend:{display:false}},scales:{y:{ticks:{color:"#ccc"},grid:{display:false}},x:{ticks:{color:"#999"},grid:{color:"#333"}}}}});

// Heatmap
const hm=Array.from({length:7},()=>Array(24).fill(0));
data.forEach(d=>hm[d.dow][d.hour]++);
const hmx=Math.max(...hm.flat());
const hl=[0,3,6,9,12,15,18,21];
let hh='<table class="heatmap-table"><tr><th></th>';
hl.forEach(h=>hh+=`<th>${h}h</th>`); hh+="</tr>";
DOW.forEach((day,di)=>{hh+=`<tr><th style="text-align:right;padding-right:8px">${day}</th>`;hl.forEach(h=>{const v=hm[di][h]+(h+1<24?hm[di][h+1]:0)+(h+2<24?hm[di][h+2]:0);const a=Math.min(v/(hmx*2)*2.5+0.05,1);hh+=`<td style="background:rgba(245,158,11,${a.toFixed(2)});color:${a>0.4?"#000":"#888"}">${v||""}</td>`;});hh+="</tr>";});
hh+="</table>"; document.getElementById("heatmap").innerHTML=hh;

// Currency
const curCounts={};
data.forEach(d=>{const cur=d.cost.replace(/[0-9.,\s]/g,"")||"?";curCounts[cur]=(curCounts[cur]||0)+1});
const curE=Object.entries(curCounts).sort((a,b)=>b[1]-a[1]);
const curColors={"£":"#3b82f6","€":"#22c55e","CHF":"#f59e0b"};
new Chart(document.getElementById("currencyChart"),{type:"doughnut",data:{labels:curE.map(e=>e[0]),datasets:[{data:curE.map(e=>e[1]),backgroundColor:curE.map(e=>curColors[e[0]]||"#888"),borderColor:"#1a1a1e",borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"right",labels:{color:"#ccc",font:{size:11},padding:10}}}}});

// Trips
function haversine(a,b){const R=6371,dLat=(b[0]-a[0])*Math.PI/180,dLon=(b[1]-a[1])*Math.PI/180,x=Math.sin(dLat/2)**2+Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.sin(dLon/2)**2;return R*2*Math.asin(Math.sqrt(x))}
function tripDistance(trip){let d=0;for(let i=1;i<trip.length;i++){const a=COORDS[trip[i-1].location],b=COORDS[trip[i].location];if(a&&b)d+=haversine(a,b)}return Math.round(d*1.3)}
const sorted=[...data].sort((a,b)=>a.dt-b.dt);
const trips=[];let buf=[sorted[0]];
for(let i=1;i<sorted.length;i++){const gap=(sorted[i].dt-buf[buf.length-1].dt)/36e5;const same=sorted[i].location.split(",")[0]===buf[buf.length-1].location.split(",")[0];const sameAsStart=buf.length>=3&&sorted[i].location.split(",")[0]===buf[0].location.split(",")[0];if(gap<48&&!same){buf.push(sorted[i])}else if(gap<48&&same&&!sameAsStart){/* same city as previous, trip continues — skip */}else if(gap<48&&sameAsStart){buf.push(sorted[i]);if(buf.length>=3){const c=[...new Set(buf.map(t=>t.location.split(",")[0]))];if(c.length>=3)trips.push([...buf])}buf=[sorted[i]]}else{if(buf.length>=3){const c=[...new Set(buf.map(t=>t.location.split(",")[0]))];if(c.length>=3)trips.push([...buf])}buf=[sorted[i]]}}
if(buf.length>=3){const c=[...new Set(buf.map(t=>t.location.split(",")[0]))];if(c.length>=3)trips.push([...buf])}
trips.reverse();
document.getElementById("trips").innerHTML=trips.map((trip,idx)=>{const c=[...new Set(trip.map(t=>t.location.split(",")[0]))];const cs=[...new Set(trip.map(t=>t.country))];const km=tripDistance(trip);return`<div class="trip" data-trip="${idx}"><div class="trip-title">${c.join(" → ")}</div><div class="trip-meta">${trip[0].date} ~ ${trip[trip.length-1].date} · ${trip.length} stops · ~${km.toLocaleString()}km · ${cs.join(", ")}</div></div>`}).join("")||'<div style="color:var(--text2);padding:20px;text-align:center">No multi-city trips detected</div>';

// Trip map
const tripMap=L.map("tripMap").setView([47,2],5);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{attribution:'OpenStreetMap CARTO',maxZoom:18}).addTo(tripMap);
let tripLayer=L.layerGroup().addTo(tripMap);
let activeTrip=null;

function showTrip(idx){
  tripLayer.clearLayers();
  const trip=trips[idx];
  const points=[];
  trip.forEach((stop,i)=>{
    const co=COORDS[stop.location];if(!co)return;
    points.push(co);
    const color=CC[stop.country]||"#999";
    L.circleMarker(co,{radius:8,fillColor:color,color:"#fff",weight:2,fillOpacity:0.9})
      .bindPopup(`<b>${i+1}. ${stop.location}</b><br>${stop.date} ${stop.time}`)
      .addTo(tripLayer);
    // stop number label
    L.marker(co,{icon:L.divIcon({className:'',html:`<div style="color:#fff;font-size:10px;font-weight:700;text-align:center;margin-top:-4px;text-shadow:0 1px 3px #000">${i+1}</div>`,iconSize:[20,20],iconAnchor:[10,10]})}).addTo(tripLayer);
  });
  if(points.length>=2){
    L.polyline(points,{color:"#06b6d4",weight:2,opacity:0.6,dashArray:"6,8"}).addTo(tripLayer);
  }
  if(points.length>0){
    tripMap.fitBounds(L.latLngBounds(points).pad(0.15));
  }
}

document.getElementById("trips").addEventListener("click",e=>{
  const el=e.target.closest(".trip");if(!el)return;
  const idx=+el.dataset.trip;
  document.querySelectorAll(".trip.active").forEach(t=>t.classList.remove("active"));
  if(activeTrip===idx){activeTrip=null;tripLayer.clearLayers();tripMap.setView([47,2],5);return}
  el.classList.add("active");
  activeTrip=idx;
  showTrip(idx);
});

// Show first trip by default if exists
if(trips.length>0){document.querySelector('.trip[data-trip="0"]')?.classList.add("active");showTrip(0);}

// One-time chargers
const oneTimers=Object.entries(locCounts).filter(([,c])=>c===1).map(([l])=>l);
const otByCountry={};
oneTimers.forEach(l=>{const c=getCountry(l);if(!otByCountry[c])otByCountry[c]=[];otByCountry[c].push(l)});
const otCountries=Object.keys(otByCountry).sort((a,b)=>otByCountry[b].length-otByCountry[a].length);
document.getElementById("oneTimers").innerHTML=
  `<p style="color:var(--text2);font-size:13px;margin-bottom:16px">${oneTimers.length} locations visited only once &mdash; click to locate on map</p>`+
  otCountries.map(c=>{const color=CC[c]||"#999";const locs=otByCountry[c].sort();return`<div class="country-group"><h3><span class="cdot" style="background:${color}"></span>${c} (${locs.length})</h3><div class="ot-grid">${locs.map(l=>{const d=data.find(r=>r.location===l);return`<span class="ot-tag" data-loc="${l.replace(/"/g,'&quot;')}" title="${d.date}">${l.split(",")[0]}${l.includes("–")?" – "+l.split("–")[1].trim():""}</span>`}).join("")}</div></div>`}).join("");

// One-timer map
const otMap=L.map("otMap").setView([47,2],5);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{attribution:'OpenStreetMap CARTO',maxZoom:18}).addTo(otMap);
// Place all one-timer markers (dimmed)
const otMarkers={};
oneTimers.forEach(l=>{
  const co=COORDS[l];if(!co)return;
  const color=CC[getCountry(l)]||"#999";
  const d=data.find(r=>r.location===l);
  const m=L.circleMarker(co,{radius:6,fillColor:color,color:"#fff",weight:1,fillOpacity:0.25}).bindPopup(`<b>${l}</b><br>${d.date}`).addTo(otMap);
  otMarkers[l]=m;
});

let activeLoc=null;
document.getElementById("oneTimers").addEventListener("click",e=>{
  const tag=e.target.closest(".ot-tag");if(!tag)return;
  const loc=tag.dataset.loc;
  // toggle active style
  document.querySelectorAll(".ot-tag.active").forEach(t=>t.classList.remove("active"));
  if(activeLoc===loc){activeLoc=null;Object.values(otMarkers).forEach(m=>m.setStyle({fillOpacity:0.25,radius:6}));otMap.setView([47,2],5);return}
  tag.classList.add("active");
  activeLoc=loc;
  // highlight marker
  Object.entries(otMarkers).forEach(([l,m])=>{if(l===loc){m.setStyle({fillOpacity:1,radius:12});m.openPopup();otMap.setView(m.getLatLng(),10)}else{m.setStyle({fillOpacity:0.15,radius:5})}});
});
</script>
</body>
</html>"""


def save_html(records: list[dict], coords: dict, output_path: Path,
              sites: list[dict] | None = None):
    """Generate the HTML analytics dashboard."""
    data_json = json.dumps(records, ensure_ascii=False)
    locs_in_data = set(r["location"] for r in records)
    coords_subset = {k: v for k, v in coords.items() if k in locs_in_data}
    coords_json = json.dumps(coords_subset, ensure_ascii=False)

    closed = build_closed_set(records, sites) if sites else set()
    closed_json = json.dumps(list(closed), ensure_ascii=False)

    html = (HTML_TEMPLATE
            .replace("%%DATA%%", data_json)
            .replace("%%COORDS%%", coords_json)
            .replace("%%CLOSED%%", closed_json))
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML: {output_path} ({len(records)} records)")


def save_json(records: list[dict], output_path: Path):
    """Save records as JSON."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  JSON: {output_path} ({len(records)} records)")


# ─── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Tesla Supercharger Analytics — Generate a dashboard from your charging CSV data",
        epilog="Example: python supercharger_analytics.py charging-history*.csv",
    )
    parser.add_argument("csv_files", nargs="+", help="Tesla charging history CSV file(s)")
    parser.add_argument("--output", "-o", default="charge_history",
                        help="Output filename without extension (default: charge_history)")
    parser.add_argument("--tz", default="Europe/London",
                        help="Display timezone — match your Tesla app setting (default: Europe/London)")
    args = parser.parse_args()

    # Validate input files
    csv_paths = []
    for p in args.csv_files:
        path = Path(p).resolve()
        if not path.exists():
            print(f"Error: {path} not found", file=sys.stderr)
            sys.exit(1)
        if path.suffix.lower() != ".csv":
            print(f"Warning: skipping non-CSV file: {path.name}", file=sys.stderr)
            continue
        csv_paths.append(path)

    if not csv_paths:
        print("Error: No CSV files provided", file=sys.stderr)
        sys.exit(1)

    output_base = Path(args.output).resolve()
    output_dir = output_base.parent
    output_name = output_base.name
    html_path = output_dir / f"{output_name}.html"
    json_path = output_dir / f"{output_name}.json"
    coords_path = output_dir / "coords.json"
    cache_path = output_dir / "supercharger_db.json"

    # 1. Import CSV
    print("1. Reading CSV files...")
    records = import_csv(csv_paths, display_tz=args.tz)

    if not records:
        print("Error: No charging records found in CSV files", file=sys.stderr)
        sys.exit(1)

    # 2. Normalize & deduplicate
    print("2. Processing records...")
    for r in records:
        r["location"] = normalize_location(r["location"])

    # Merge with existing data if present
    if json_path.exists():
        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"  Existing data: {len(existing)} records")
        records = existing + records

    records = deduplicate(records)
    print(f"  Total: {len(records)} unique records")

    # 3. Resolve coordinates
    print("3. Resolving coordinates...")
    sites = fetch_supercharger_db(cache_path)
    all_locations = list(set(r["location"] for r in records))
    coords = resolve_coords(all_locations, sites, coords_path)

    # 4. Generate output
    print("4. Generating output...")
    save_json(records, json_path)
    save_html(records, coords, html_path, sites=sites)

    print(f"\nDone! {len(records)} charging records -> {html_path.name}")
    print(f"Open {html_path} in your browser to view the dashboard.")


if __name__ == "__main__":
    main()
