#!/usr/bin/env python3
"""
Helsinki peruspiiri (district) chronic-disease index vs. road-traffic exposure.

What this maps
--------------
Helsinki publishes an age-standardised *kansantauti-indeksi* (chronic-disease
index) per peruspiiri, built from Kela entitlements to special medicine
reimbursement. It is the unweighted mean of seven sub-indices:

    asthma, diabetes, rheumatoid arthritis, psychoses,
    coronary artery disease, heart failure, hypertension

IMPORTANT CAVEAT: the asthma sub-index is NOT published separately at district
level.  Helsinki's open file gives only the 7-disease composite, so asthma
contributes ~1/7 of what you see here.  An asthma-only district series requires
a custom Kela statistics order ("erillistilasto").  See README.md.

Road lines are drawn with width proportional to measured daily traffic from
Fintraffic's automatic counters (TMS/LAM), via the Digitraffic raw-data service.

Exposure is either
  * area   -- share of district land within <buffer> m of a motorway/trunk road, or
  * traffic-- vehicle-km per day per km2 of district land (default), which weights
              each road by what actually drives on it.

Data sources (all open, no key needed)
--------------------------------------
* districts : Helsinki WFS, avoindata:Maavesi_peruspiirit (land only)
* index     : hel.fi XLSX, "Helsingin sairastavuusindeksi", via HRI/CKAN
* roads     : OpenStreetMap via Overpass API
* traffic   : Fintraffic / Digitraffic TMS (LAM) counters, raw per-vehicle CSVs

Usage
-----
    python3 helsinki_morbidity_highways.py
    python3 helsinki_morbidity_highways.py --tms-days 3 --html
    python3 helsinki_morbidity_highways.py --exposure area --buffer 500
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import openpyxl
import pandas as pd
import requests
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
from scipy.stats import spearmanr
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "out"

UA = {"User-Agent": "HelsinkiLungMap/2.0 (research)"}
# Digitraffic requires an identifying header AND gzip; without either it answers
# 406 with a one-line explanation instead of data.
DT_HEADERS = {**UA, "Digitraffic-User": "HelsinkiLungMap/research", "Accept-Encoding": "gzip"}

# Maavesi_peruspiirit = LAND-ONLY district polygons (tyyppi "Maa-alue").
# Do NOT use Piirijako_peruspiiri: those include each district's maritime area,
# which wrecks the framing and dilutes every area-normalised statistic.
WFS_DISTRICTS = (
    "https://kartta.hel.fi/ws/geoserver/avoindata/wfs"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=avoindata:Maavesi_peruspiirit"
    "&outputFormat=application/json&srsName=EPSG:4326"
)

INDEX_XLSX = {
    2023: "https://www.hel.fi/static/avoindata/kanslia/vaesto/Helsingin_sairastavuusindeksi_2023.xlsx",
    2021: "https://www.hel.fi/hel2/tietokeskus/data/helsinki/Terveys/Helsingin_sairastavuusindeksi_2021.xlsx",
    2019: "https://www.hel.fi/hel2/tietokeskus/data/helsinki/Terveys/Helsingin_sairastavuusindeksi_2019.xlsx",
}

# Global-coverage instances only.  (overpass.osm.ch is a Switzerland-only
# extract: it answers 200 OK with zero elements for Helsinki, which looks like
# success.  That is why _overpass() insists on a non-empty result.)
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_ATTEMPTS = 3
OVERPASS_BACKOFF = 20.0

TMS_STATIONS = "https://tie.digitraffic.fi/api/tms/v1/stations"
TMS_RAW = "https://tie.digitraffic.fi/api/tms/v1/history/raw/lamraw_{tms}_{yy}_{doy}.csv"

BBOX = (59.92, 24.55, 60.38, 25.30)  # S, W, N, E
FIN_CRS = "EPSG:3067"  # ETRS-TM35FIN, metric

# ---------------------------------------------------------------- palette ----
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
SERIES_1 = "#2a78d6"
NODATA = "#e1e0d9"
ROAD = "#2b2b29"

DIVERGING = LinearSegmentedColormap.from_list(
    "hel_div",
    ["#0d366b", "#2a78d6", "#cde2fb", "#f0efec", "#f6a9a8", "#e34948", "#8f1f1f"],
)

mpl.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": INK,
        "axes.labelcolor": INK_SECONDARY,
        "xtick.color": INK_MUTED,
        "ytick.color": INK_MUTED,
        "axes.edgecolor": GRID,
    }
)

# Line-width encoding for traffic volume: width is LINEAR in vehicles/day, so the
# legend reads literally.  (A sqrt ramp was tried first and rejected -- the drawn
# network is motorway/trunk only, spanning ~11k-118k veh/day, and compressing that
# range made every road look alike.)
LW_MIN, LW_MAX = 0.8, 5.0


# ------------------------------------------------------------------ utils ----
def norm(name: str) -> str:
    """Join key: fold case and strip accents so 'Taka-Töölö' == 'TAKA-TÖÖLÖ'."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return s.upper().strip()


def cached(url: str, filename: str, refresh: bool = False, headers: dict | None = None) -> Path:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / filename
    if path.exists() and path.stat().st_size > 0 and not refresh:
        return path
    print(f"  fetching {filename} ...", flush=True)
    r = requests.get(url, headers=headers or UA, timeout=180)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


# ------------------------------------------------------------- load: geo -----
def load_districts(refresh: bool = False) -> gpd.GeoDataFrame:
    path = cached(WFS_DISTRICTS, "peruspiiri_land.geojson", refresh)
    gdf = gpd.read_file(path)
    gdf = gdf[gdf["tyyppi"] == "Maa-alue"]
    gdf = gdf[["nimi_fi", "nimi_se", "tunnus", "pa", "geometry"]].copy()
    gdf = gdf.rename(columns={"pa": "land_area_m2"})
    gdf["key"] = gdf["nimi_fi"].map(norm)
    gdf["name"] = gdf["nimi_fi"].str.title()
    return gdf.to_crs(FIN_CRS)


# ----------------------------------------------------------- load: index -----
def load_index(year: int, baseline: str, refresh: bool = False) -> pd.DataFrame:
    """Parse the morbidity-index workbook into one row per peruspiiri.

    Aggregate rows are ALL CAPS ('HELSINKI', '1 ETELAINEN SUURPIIRI'); peruspiiri
    rows are Title Case -- that is the discriminator.
    """
    if year not in INDEX_XLSX:
        sys.exit(f"No workbook for {year}. Available: {sorted(INDEX_XLSX)}")
    path = cached(INDEX_XLSX[year], f"sairastavuusindeksi_{year}.xlsx", refresh)
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]

    # Column layout differs by vintage:
    #   2021 / 2023 -> table 1 at column A is Helsinki=100, table 2 at column J
    #                  is Finland=100.
    #   2019        -> ONE table at column A, and it is Finland=100 (its first
    #                  row is "KOKO SUOMI" = 100, not "HELSINKI" = 100).
    if year == 2019:
        if baseline != "finland":
            sys.exit(
                f"The {year} workbook publishes only the Finland=100 table. "
                f"Re-run with --baseline finland, or use --year 2021/2023 for Helsinki=100."
            )
        c0 = 0
    elif baseline == "finland":
        c0 = 9  # column J
    else:
        c0 = 0

    skip = ("lahde", "lähde", "taulukon", "luku", "sairastavuusindeksi",
            "niista", "niistä", "kansantauti", "nailla", "näille")
    rows = []
    for r in ws.iter_rows(min_row=3, values_only=True):
        name = r[c0]
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        if name.lower().startswith(skip) or name.isupper():
            continue
        rows.append(
            {
                "name": name,
                "key": norm(name),
                "morbidity_index": r[c0 + 1],
                "mortality_index": r[c0 + 2],
                "disability_index": r[c0 + 3],
                "reimbursement_index": r[c0 + 4],
                "chronic_disease_index": r[c0 + 5],
                "population": r[c0 + 6],
            }
        )

    df = pd.DataFrame(rows)
    for c in df.columns:
        if c.endswith("index") or c == "population":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["morbidity_index"]).reset_index(drop=True)
    if df.empty:
        sys.exit("Parsed 0 districts -- the workbook layout changed; inspect it by hand.")
    print(f"  parsed {len(df)} peruspiiri rows ({year}, baseline={baseline})")
    return df


# ----------------------------------------------------------- load: roads -----
def _overpass(query: str) -> dict:
    """POST to Overpass, rotating mirrors with backoff.

    Overpass answers HTTP 200 with an HTML "runtime error ... server is probably
    too busy" body under load, and regional instances answer 200 with an empty
    element list.  Both must count as failures or we cache garbage.
    """
    delay = OVERPASS_BACKOFF
    for attempt in range(1, OVERPASS_ATTEMPTS + 1):
        for mirror in OVERPASS_MIRRORS:
            host = mirror.split("/")[2]
            try:
                print(f"  querying Overpass ({host}, pass {attempt}/{OVERPASS_ATTEMPTS}) ...", flush=True)
                r = requests.post(mirror, data={"data": query}, headers=UA, timeout=300)
                r.raise_for_status()
                if "json" not in r.headers.get("Content-Type", "") and not r.text.lstrip().startswith("{"):
                    raise ValueError(f"non-JSON body: {' '.join(r.text.split())[:160]}")
                raw = r.json()
                n = len(raw.get("elements", []))
                if n == 0:
                    raise ValueError("zero elements returned (regional extract, or bbox/filter mismatch)")
                print(f"    got {n} elements")
                return raw
            except Exception as exc:  # noqa: BLE001 - mirrors fail routinely
                print(f"    {host} failed: {type(exc).__name__}: {exc}")
        if attempt < OVERPASS_ATTEMPTS:
            print(f"  all mirrors busy; waiting {delay:.0f}s before pass {attempt + 1}", flush=True)
            time.sleep(delay)
            delay *= 2
    sys.exit(
        "All Overpass mirrors failed. Retry later, or pre-seed the cache by hand:\n"
        "  overpass-turbo.eu -> run the query -> save GeoJSON to cache/roads_*.geojson"
    )


def load_roads(classes: str, refresh: bool = False) -> gpd.GeoDataFrame:
    s, w, n, e = BBOX
    query = (
        f"[out:json][timeout:240];"
        f'way["highway"~"^({classes})(_link)?$"]({s},{w},{n},{e});'
        f"out geom;"
    )
    path = CACHE / f"roads_{classes.replace('|', '-')}.geojson"
    if not (path.exists() and path.stat().st_size > 0 and not refresh):
        raw = _overpass(query)
        feats = []
        for el in raw.get("elements", []):
            geom = el.get("geometry") or []
            if len(geom) < 2:
                continue
            tags = el.get("tags", {})
            feats.append(
                {
                    "type": "Feature",
                    "properties": {"id": el["id"], "highway": tags.get("highway"),
                                   "ref": tags.get("ref"), "name": tags.get("name")},
                    "geometry": LineString([(p["lon"], p["lat"]) for p in geom]).__geo_interface__,
                }
            )
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    gdf = gpd.read_file(path).set_crs("EPSG:4326", allow_override=True)
    print(f"  {len(gdf)} road segments ({classes})")
    return gdf.to_crs(FIN_CRS)


# --------------------------------------------------- load: Fintraffic TMS ----
def _weekdays_back(n: int, lag_days: int = 2) -> list[dt.date]:
    """The n most recent weekdays, ending `lag_days` ago so files are complete."""
    out, day = [], dt.date.today() - dt.timedelta(days=lag_days)
    while len(out) < n:
        if day.weekday() < 5:  # Mon-Fri
            out.append(day)
        day -= dt.timedelta(days=1)
    return out


def load_tms(days: int, refresh: bool = False) -> gpd.GeoDataFrame:
    """Fintraffic TMS (LAM) counters in the bbox, with measured vehicles/day.

    The raw endpoint serves ONE ROW PER VEHICLE for one station-day (~4 MB for a
    busy motorway), so the files are streamed and counted, never stored.  Column
    13 is the faulty flag; only clean rows are counted.  Note the file is keyed by
    `tmsNumber`, not the station `id` -- using the id gives HTTP 403.
    """
    meta = json.loads(cached(TMS_STATIONS, "tms_stations.geojson", refresh,
                             headers=DT_HEADERS).read_text())
    s, w, n, e = BBOX
    rows = []
    for f in meta["features"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f["properties"]
        if s <= lat <= n and w <= lon <= e and p.get("collectionStatus") == "GATHERING":
            m = re.match(r"^(vt|kt|st|mt)(\d+)[_ ]", p.get("name") or "")
            rows.append({"id": p["id"], "tms": p["tmsNumber"], "station": p.get("name"),
                         "road_no": m.group(2) if m else None, "lon": lon, "lat": lat})
    st = gpd.GeoDataFrame(rows, geometry=gpd.points_from_xy([r["lon"] for r in rows],
                                                            [r["lat"] for r in rows]),
                          crs="EPSG:4326").to_crs(FIN_CRS)

    dates = _weekdays_back(days)
    counts_path = CACHE / "tms_daily_counts.json"
    store = json.loads(counts_path.read_text()) if counts_path.exists() and not refresh else {}

    todo = [(r.tms, d) for r in st.itertuples() for d in dates
            if f"{r.tms}_{d.isoformat()}" not in store]
    if todo:
        print(f"  counting {len(todo)} station-days from Fintraffic raw data "
              f"({len(st)} counters x {len(dates)} weekdays) ...", flush=True)
    for i, (tms, day) in enumerate(todo, 1):
        key = f"{tms}_{day.isoformat()}"
        url = TMS_RAW.format(tms=tms, yy=day.strftime("%y"), doy=day.timetuple().tm_yday)
        try:
            with requests.get(url, headers=DT_HEADERS, timeout=300, stream=True) as r:
                if r.status_code != 200:
                    store[key] = None
                    continue
                good = 0
                for line in r.iter_lines(decode_unicode=False):
                    if not line:
                        continue
                    parts = line.split(b";")
                    if len(parts) > 12 and parts[12] == b"0":
                        good += 1
                store[key] = good
        except Exception as exc:  # noqa: BLE001
            print(f"    station {tms} {day}: {type(exc).__name__}")
            store[key] = None
        if i % 10 == 0 or i == len(todo):
            print(f"    {i}/{len(todo)}", flush=True)
            counts_path.write_text(json.dumps(store))
    counts_path.write_text(json.dumps(store))

    def mean_count(tms: int) -> float:
        vals = [store.get(f"{tms}_{d.isoformat()}") for d in dates]
        vals = [v for v in vals if v]
        return float(np.mean(vals)) if vals else np.nan

    st["veh_per_day"] = [mean_count(t) for t in st["tms"]]
    ok = st["veh_per_day"].notna().sum()
    print(f"  {ok}/{len(st)} counters with data; "
          f"median {np.nanmedian(st['veh_per_day']):,.0f} veh/day, "
          f"max {np.nanmax(st['veh_per_day']):,.0f}")
    st.attrs["dates"] = [d.isoformat() for d in dates]
    return st.dropna(subset=["veh_per_day"]).reset_index(drop=True)


def attach_traffic(roads: gpd.GeoDataFrame, st: gpd.GeoDataFrame,
                   max_dist: float = 4000.0) -> gpd.GeoDataFrame:
    """Give every road segment a vehicles/day figure from the nearest counter.

    Matched on ROAD NUMBER first (OSM `ref` '101' <-> station 'st101_Malmi'), so a
    Kehä I counter cannot be attributed to the motorway crossing 200 m away.
    Segments with no ref (mostly ramps and links) fall back to the nearest counter
    within `max_dist`; anything left over is drawn hairline and excluded from the
    traffic exposure sum.
    """
    roads = roads.copy()
    roads["ref_no"] = roads["ref"].fillna("").str.extract(r"(\d+)", expand=False)
    reps = roads.geometry.representative_point()

    vol, src = [], []
    by_road = {str(k): v for k, v in st.groupby("road_no")}
    for ref_no, pt in zip(roads["ref_no"], reps):
        pool = by_road.get(str(ref_no))
        matched = "road-number"
        if pool is None or pool.empty:
            pool, matched = st, "nearest"
        d = pool.geometry.distance(pt)
        j = d.idxmin()
        if d[j] > max_dist and matched == "nearest":
            vol.append(np.nan); src.append("none")
        else:
            vol.append(float(pool.at[j, "veh_per_day"])); src.append(matched)
    roads["veh_per_day"] = vol
    roads["match"] = src
    n_ok = roads["veh_per_day"].notna().sum()
    print(f"  traffic attached to {n_ok}/{len(roads)} segments "
          f"({(roads['match'] == 'road-number').sum()} by road number)")
    return roads


# -------------------------------------------------------------- exposure -----
def add_exposure(districts: gpd.GeoDataFrame, roads: gpd.GeoDataFrame,
                 buffer_m: float) -> gpd.GeoDataFrame:
    """Both exposure measures, per district.

    area    : % of land within buffer_m of any motorway/trunk road.
    traffic : vehicle-km per day per km2 of land, summing each road segment's
              measured flow x its length inside the district's buffered outline.
              This is the one that distinguishes a forest beside a motorway from
              an actual roadside neighbourhood.
    """
    road_union = unary_union(roads.geometry.values)
    corridor = road_union.buffer(buffer_m)
    with_vol = roads.dropna(subset=["veh_per_day"])

    shares, dists, tdens = [], [], []
    for geom in districts.geometry:
        area = geom.area
        shares.append(100.0 * geom.intersection(corridor).area / area if area else np.nan)
        dists.append(geom.representative_point().distance(road_union))

        # Numerator and denominator must cover the SAME region.  Summing roads
        # inside the buffered outline while dividing by the bare district area
        # inflates small districts badly (Itä-Pakila is ~1 km2 with two trunk
        # roads just outside it, and scored 2x the city centre that way).
        near = geom.buffer(buffer_m)
        hits = with_vol.iloc[list(with_vol.sindex.query(near, predicate="intersects"))]
        vkm = sum(r.veh_per_day * r.geometry.intersection(near).length / 1000.0
                  for r in hits.itertuples())
        tdens.append(vkm / (near.area / 1e6) if near.area else np.nan)

    out = districts.copy()
    out[f"pct_area_within_{int(buffer_m)}m"] = shares
    out["centroid_dist_m"] = dists
    out["veh_km_per_day_per_km2"] = tdens
    return out


# ------------------------------------------------- hand-rolled map drawing ---
def geom_to_path(geom) -> MplPath:
    """Shapely (Multi)Polygon -> one matplotlib Path, holes included.

    Drawing the map by hand rather than via GeoDataFrame.plot(): the renderer
    below needs per-feature patches and per-segment line widths, which the
    convenience plotter cannot express.
    """
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    subpaths = []
    for poly in polys:
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            xy = np.asarray(ring.coords)
            if len(xy) < 3:
                continue
            codes = np.full(len(xy), MplPath.LINETO, dtype=np.uint8)
            codes[0] = MplPath.MOVETO
            codes[-1] = MplPath.CLOSEPOLY
            subpaths.append(MplPath(xy, codes))
    return MplPath.make_compound_path(*subpaths) if subpaths else MplPath(np.zeros((0, 2)))


def road_segments(roads: gpd.GeoDataFrame, vmax: float):
    """Explode road lines into (segment coords, width, has-data) for a LineCollection."""
    segs, lws, known = [], [], []
    for row in roads.itertuples():
        geoms = row.geometry.geoms if row.geometry.geom_type == "MultiLineString" else [row.geometry]
        v = row.veh_per_day
        if np.isnan(v):
            lw, k = LW_MIN * 0.6, False
        else:
            lw, k = LW_MIN + (LW_MAX - LW_MIN) * (max(v, 0) / vmax), True
        for g in geoms:
            xy = np.asarray(g.coords)
            if len(xy) < 2:
                continue
            segs.append(xy)
            lws.append(lw)
            known.append(k)
    return segs, np.array(lws), np.array(known)


def view_bounds(gdf: gpd.GeoDataFrame, keep_area: float = 0.99, pad: float = 0.02):
    """Map window holding `keep_area` of the land, padded.

    Helsinki's districts include outer-archipelago rocks: one 0.002 km2 skerry
    belonging to Laajasalo sits ~16 km south of the city and, left in the frame,
    stretches the map 60% vertically for no information.  This trims the view
    only -- every statistic is still computed on the full land geometry.
    """
    parts = gdf.explode(index_parts=False)
    parts = parts.assign(_a=parts.area).sort_values("_a", ascending=False)
    share = parts["_a"].cumsum() / parts["_a"].sum()
    keep = parts[share <= keep_area]
    x0, y0, x1, y1 = (keep if len(keep) else parts).total_bounds
    dx, dy = (x1 - x0) * pad, (y1 - y0) * pad
    return x0 - dx, y0 - dy, x1 + dx, y1 + dy


def width_legend(ax, vmax: float) -> None:
    """Sample road widths, since width carries a quantity here."""
    steps = [s for s in (20_000, 60_000, 100_000, 140_000) if s <= vmax * 1.05][-3:]
    lax = ax.inset_axes([0.03, 0.012, 0.26, 0.068])
    lax.set_axis_off()
    lax.set_xlim(0, 1)
    lax.set_ylim(-0.6, len(steps) - 0.4)
    for i, v in enumerate(steps):
        lw = LW_MIN + (LW_MAX - LW_MIN) * (v / vmax)
        lax.add_line(Line2D([0.02, 0.42], [i, i], color=ROAD, lw=lw,
                            solid_capstyle="round"))
        lax.text(0.48, i, f"{v // 1000:,} 000", fontsize=8, color=INK_SECONDARY,
                 va="center")
    lax.text(0.02, len(steps) - 0.35, "Vehicles per day (Fintraffic counters)",
             fontsize=8.5, color=INK_SECONDARY, va="bottom")


def plot_map(gdf: gpd.GeoDataFrame, roads: gpd.GeoDataFrame, value: str, year: int,
             baseline: str, buffer_m: float, tms_dates: list[str], path: Path) -> None:
    ref = "Helsinki" if baseline == "helsinki" else "Finland"
    vals = gdf[value].dropna()
    span = max(abs(vals.max() - 100), abs(100 - vals.min()))
    norm_ = TwoSlopeNorm(vmin=100 - span, vcenter=100, vmax=100 + span)
    cmap = DIVERGING

    x0, y0, x1, y1 = view_bounds(gdf)
    aspect = (y1 - y0) / (x1 - x0)
    width = 10.0
    # Helsinki's south-east quadrant is open sea, so the title block and the
    # legends go inside the map frame rather than stealing rows beneath it.
    fig, ax = plt.subplots(figsize=(width, width * aspect + 1.1))

    # --- districts, drawn patch by patch -------------------------------------
    patches, colors = [], []
    for row in gdf.itertuples():
        v = getattr(row, value)
        patches.append(PathPatch(geom_to_path(row.geometry)))
        colors.append(NODATA if (v is None or np.isnan(v)) else cmap(norm_(v)))
    ax.add_collection(
        PatchCollection(patches, facecolors=colors, edgecolors=SURFACE,
                        linewidths=2.0, zorder=2)  # 2px surface gap between fills
    )
    missing = [PathPatch(geom_to_path(r.geometry)) for r in gdf.itertuples()
               if np.isnan(getattr(r, value))]
    if missing:
        ax.add_collection(
            PatchCollection(missing, facecolors="none", edgecolors=INK_MUTED,
                            linewidths=0.0, hatch="///", zorder=3)
        )

    # --- roads, width = measured traffic -------------------------------------
    # Draw only the through-carriageways: the ~2200 *_link ramp ways stack on top
    # of each other at every junction and, at traffic-scaled widths, turn each
    # interchange into an ink blob.  They still count toward exposure.
    through = roads[~roads["highway"].fillna("").str.endswith("_link")]
    vis = gpd.clip(through, gpd.GeoSeries([gdf.union_all()], crs=gdf.crs)
                   .buffer(buffer_m).iloc[0])
    vmax = float(np.nanmax(roads["veh_per_day"])) if roads["veh_per_day"].notna().any() else 1.0
    segs, lws, known = road_segments(vis, vmax)
    if len(segs):
        ax.add_collection(LineCollection(
            segs, linewidths=lws, colors=np.where(known, ROAD, INK_MUTED),
            alpha=0.85, capstyle="round", joinstyle="round", zorder=5,
        ))

    # --- selective direct labels: five highest and five lowest ---------------
    lab = gdf.dropna(subset=[value]).sort_values(value)
    for row in pd.concat([lab.head(5), lab.tail(5)]).itertuples():
        pt = row.geometry.representative_point()
        ax.annotate(
            f"{row.name}\n{getattr(row, value):.0f}",
            xy=(pt.x, pt.y), ha="center", va="center", fontsize=8,
            color=INK, zorder=10,
            bbox=dict(boxstyle="round,pad=0.25", fc=SURFACE, ec="none", alpha=0.82),
        )

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(f"Helsinki chronic-disease index by peruspiiri, {year}\n",
                 fontsize=15, color=INK, loc="left", pad=16)
    ax.text(
        0.0, 1.005,
        f"Age-standardised, {ref} = 100. Mean of 7 Kela-reimbursed diseases — asthma is one of them.\n"
        f"Road width scales with measured daily traffic, Fintraffic counters ({', '.join(tms_dates)}); motorway and trunk roads only.",
        transform=ax.transAxes, fontsize=9.5, color=INK_SECONDARY, va="bottom",
    )

    cax = ax.inset_axes([0.03, 0.165, 0.30, 0.014])
    cb = fig.colorbar(mpl.cm.ScalarMappable(norm=norm_, cmap=cmap), cax=cax,
                      orientation="horizontal")
    cb.set_label(f"Chronic-disease index ({ref} = 100)", fontsize=9,
                 color=INK_SECONDARY, labelpad=4)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0, labelsize=8.5, pad=2)
    width_legend(ax, vmax)

    fig.text(
        0.01, 0.010,
        "Sources: Kaupunkitieto / City of Helsinki (Kela + Statistics Finland); districts Helsinki WFS; "
        "roads OpenStreetMap; traffic Fintraffic/Digitraffic TMS.  Hatched = not published (Östersundom).\n"
        "The index is a 7-disease composite, not an asthma measure. Land area only; outer-archipelago skerries cropped from the view.",
        fontsize=7.5, color=INK_MUTED,
    )
    fig.tight_layout(rect=(0, 0.028, 1, 1))
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"  wrote {path}")


EXPOSURE_LABEL = {
    "traffic": "Road-traffic density (1000 vehicle-km per day per km²)",
    "area": "Share of district land within {b} m of a motorway/trunk road (%)",
}


def plot_scatter(gdf: gpd.GeoDataFrame, value: str, exposure: str, kind: str,
                 year: int, baseline: str, buffer_m: float, path: Path) -> tuple[float, float]:
    ref = "Helsinki" if baseline == "helsinki" else "Finland"
    d = gdf.dropna(subset=[value, exposure]).copy()
    if kind == "traffic":
        d[exposure] = d[exposure] / 1000.0
    rho, p = spearmanr(d[exposure], d[value])

    fig, ax = plt.subplots(figsize=(8.5, 6.2))
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    ax.axhline(100, color=INK_MUTED, linewidth=1.0, linestyle=(0, (4, 4)), zorder=1)
    sizes = 40 + 260 * (d["population"] / d["population"].max())
    ax.scatter(d[exposure], d[value], s=sizes, color=SERIES_1, alpha=0.8,
               edgecolor=SURFACE, linewidth=2, zorder=3)  # 2px surface ring

    labelled = pd.concat([d.nlargest(4, value), d.nsmallest(3, value),
                          d.nlargest(3, exposure)]).drop_duplicates(subset="name")
    for i, row in enumerate(labelled.itertuples()):
        dy = 8 if i % 2 == 0 else -14
        ax.annotate(row.name, xy=(getattr(row, exposure), getattr(row, value)),
                    xytext=(7, dy), textcoords="offset points",
                    fontsize=8.5, color=INK_SECONDARY)

    ax.set_xlabel(EXPOSURE_LABEL[kind].format(b=int(buffer_m)))
    ax.set_ylabel(f"Chronic-disease index ({ref} = 100)")
    ax.set_title(f"Traffic exposure vs. chronic-disease index, Helsinki {year}",
                 fontsize=14, color=INK, loc="left", pad=34)
    ax.text(
        0.0, 1.015,
        f"Spearman ρ = {rho:+.2f}  (p = {p:.3f}, n = {len(d)});  marker area scales with population.  "
        "Not adjusted for income, age or smoking.",
        transform=ax.transAxes, fontsize=9.5, color=INK_SECONDARY, va="bottom",
    )
    fig.text(
        0.01, 0.015,
        "Ecological association across 33 districts; the index averages 7 diseases, so this is NOT an asthma–traffic estimate.",
        fontsize=7.5, color=INK_MUTED,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"  wrote {path}  (Spearman rho={rho:+.3f}, p={p:.4f})")
    return rho, p


def write_html(gdf: gpd.GeoDataFrame, roads: gpd.GeoDataFrame, value: str,
               exposure: str, baseline: str, path: Path) -> None:
    import branca.colormap as bcm
    import folium

    ref = "Helsinki" if baseline == "helsinki" else "Finland"
    # Simplify in the metric CRS before export: at full precision the road layer
    # alone makes a ~10 MB page, and 8-20 m of tolerance is sub-pixel at city zoom.
    g = gdf.copy()
    g["geometry"] = g.geometry.simplify(8)
    g = g.to_crs("EPSG:4326")
    vals = g[value].dropna()
    span = max(abs(vals.max() - 100), abs(100 - vals.min()))
    cmap = bcm.LinearColormap(
        ["#0d366b", "#2a78d6", "#cde2fb", "#f0efec", "#f6a9a8", "#e34948", "#8f1f1f"],
        vmin=100 - span, vmax=100 + span,
    )
    cmap.caption = f"Chronic-disease index ({ref} = 100)"

    m = folium.Map(location=[60.20, 24.95], zoom_start=11, tiles="CartoDB positron")

    def style(feat):
        v = feat["properties"][value]
        return {"fillColor": NODATA if v is None else cmap(v),
                "color": "#ffffff", "weight": 1.5, "fillOpacity": 0.82}

    folium.GeoJson(
        json.loads(g.to_json()), style_function=style,
        highlight_function=lambda f: {"weight": 3, "color": INK},
        tooltip=folium.GeoJsonTooltip(
            fields=["name", value, exposure, "centroid_dist_m", "population"],
            aliases=["District", f"Index ({ref}=100)", "Exposure",
                     "Centroid distance (m)", "Population"],
            localize=True),
        name="Chronic-disease index",
    ).add_to(m)

    road_layer = gpd.clip(roads, gdf.union_all().buffer(500)).copy()
    road_layer["geometry"] = road_layer.geometry.simplify(20)
    vmax = float(np.nanmax(roads["veh_per_day"])) if roads["veh_per_day"].notna().any() else 1.0
    road_layer["veh_per_day"] = road_layer["veh_per_day"].fillna(0)

    def road_style(feat):
        v = feat["properties"].get("veh_per_day") or 0
        return {"color": ROAD if v else INK_MUTED, "opacity": 0.8,
                "weight": LW_MIN + (LW_MAX - LW_MIN) * (v / vmax)}

    folium.GeoJson(
        json.loads(road_layer.to_crs("EPSG:4326").to_json()), style_function=road_style,
        tooltip=folium.GeoJsonTooltip(
            fields=[f for f in ("ref", "name", "veh_per_day") if f in road_layer.columns],
            aliases=[a for f, a in (("ref", "Road"), ("name", "Name"),
                                    ("veh_per_day", "Vehicles/day"))
                     if f in road_layer.columns],
            localize=True),
        name="Roads (width = vehicles/day)",
    ).add_to(m)

    cmap.add_to(m)
    folium.LayerControl().add_to(m)
    m.save(path)
    print(f"  wrote {path}")


# ------------------------------------------------------------------ main -----
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2023, choices=sorted(INDEX_XLSX))
    ap.add_argument("--baseline", choices=["helsinki", "finland"], default="helsinki")
    ap.add_argument("--value", default="chronic_disease_index",
                    choices=["chronic_disease_index", "morbidity_index",
                             "reimbursement_index", "mortality_index", "disability_index"])
    ap.add_argument("--exposure", choices=["traffic", "area"], default="traffic",
                    help="traffic = vehicle-km/day/km2 (default); area = %% of land near a road")
    ap.add_argument("--buffer", type=float, default=300.0,
                    help="corridor half-width in metres, for both exposure measures")
    ap.add_argument("--road-classes", default="motorway|trunk",
                    help="OSM highway values, regex-alternated (e.g. 'motorway|trunk|primary')")
    ap.add_argument("--tms-days", type=int, default=1,
                    help="how many recent weekdays of Fintraffic counts to average")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    print("Loading data")
    districts = load_districts(args.refresh)
    index = load_index(args.year, args.baseline, args.refresh)
    roads = load_roads(args.road_classes, args.refresh)
    stations = load_tms(args.tms_days, args.refresh)
    roads = attach_traffic(roads, stations)

    gdf = districts.merge(index.drop(columns=["name"]), on="key", how="left")
    missing = sorted(gdf.loc[gdf["chronic_disease_index"].isna(), "name"])
    orphans = sorted(set(index["key"]) - set(districts["key"]))
    if missing:
        print(f"  no index value for: {', '.join(missing)}")
    if orphans:
        print(f"  WARNING: table rows with no geometry: {', '.join(orphans)}")

    print("Computing exposure")
    gdf = add_exposure(gdf, roads, args.buffer)
    exposure = ("veh_km_per_day_per_km2" if args.exposure == "traffic"
                else f"pct_area_within_{int(args.buffer)}m")

    print("Rendering")
    tag = f"{args.year}_{args.baseline}_{int(args.buffer)}m_{args.exposure}"
    plot_map(gdf, roads, args.value, args.year, args.baseline, args.buffer,
             stations.attrs["dates"], OUT / f"map_{args.value}_{tag}.png")
    rho, p = plot_scatter(gdf, args.value, exposure, args.exposure, args.year,
                          args.baseline, args.buffer, OUT / f"scatter_{args.value}_{tag}.png")
    if args.html:
        write_html(gdf, roads, args.value, exposure, args.baseline,
                   OUT / f"map_{args.value}_{tag}.html")

    cols = ["name", "tunnus", "population", "chronic_disease_index", "morbidity_index",
            "mortality_index", "disability_index", "reimbursement_index",
            f"pct_area_within_{int(args.buffer)}m", "veh_km_per_day_per_km2",
            "centroid_dist_m"]
    csv = OUT / f"helsinki_districts_{tag}.csv"
    gdf[cols].sort_values(args.value, ascending=False).to_csv(csv, index=False, float_format="%.2f")
    print(f"  wrote {csv}")

    print(f"\nSpearman({exposure}, {args.value}) = {rho:+.3f}  p = {p:.4f}")
    print("Reminder: the index is a 7-disease composite; asthma alone is not published per district.")


if __name__ == "__main__":
    main()
