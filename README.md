# Helsinki district morbidity vs. motorway proximity

`helsinki_morbidity_highways.py` maps Helsinki's age-standardised **chronic-disease
index** (*kansantauti-indeksi*) for all 33 published peruspiiri (districts) and tests
it against road-traffic exposure built from **Fintraffic's automatic counters**.

The map is drawn by hand — districts as matplotlib `Path`/`PathPatch` compound paths
(holes included) and roads as a `LineCollection` with a per-segment width — rather than
through `GeoDataFrame.plot()`, which cannot vary line width per feature.

```bash
python3 helsinki_morbidity_highways.py                 # 2023, Helsinki = 100, 300 m corridor
python3 helsinki_morbidity_highways.py --html          # + interactive folium map
python3 helsinki_morbidity_highways.py --year 2019 --baseline finland
python3 helsinki_morbidity_highways.py --exposure area --buffer 500
python3 helsinki_morbidity_highways.py --tms-days 3   # average 3 weekdays of counts
```

## Traffic data

Road line width is **linear in measured vehicles/day** from Fintraffic's TMS (LAM)
counters — 72 counters in the Helsinki region, via Digitraffic. There is no AADT field
in the API, so the script takes the raw per-vehicle history files
(`/api/tms/v1/history/raw/lamraw_{tmsNumber}_{yy}_{dayofyear}.csv`, one row per vehicle,
~4 MB per station-day), streams them, and counts rows whose faulty flag is 0. Counts are
cached in `cache/tms_daily_counts.json`, so only the first run is slow.

Counters are matched to road segments **by road number** (OSM `ref` `101` ↔ station
`st101_Malmi`), not by blind nearest-neighbour, so a Kehä I counter cannot be attributed
to a motorway crossing 200 m away. Ramps and links (no `ref`) fall back to the nearest
counter within 4 km.

Two Digitraffic gotchas: requests must send `Accept-Encoding: gzip` and a
`Digitraffic-User` header or you get HTTP 406 with a one-line explanation instead of
data; and the raw files are keyed by `tmsNumber` (`149`), not the station `id` (`23149`),
which returns 403.

Requires `geopandas shapely pyproj matplotlib pandas openpyxl requests scipy` (+ `folium`
for `--html`). Outputs land in `out/`, downloads cache in `cache/`.

## Read this before you believe any number here

**1. This is not an asthma measure.** Helsinki publishes only the 7-disease composite:
asthma, diabetes, rheumatoid arthritis, psychoses, coronary artery disease, heart failure,
hypertension — an *unweighted* mean, so asthma is ~1/7 of the signal, diluted by six
conditions with no plausible traffic pathway. An asthma-only district series is **not open
data**; the city obtained these from Kela as a custom statistics order
(*erillistilasto*), and that is the route to an asthma-only version.

**2. Even asthma-only would be severity-truncated.** The underlying variable is
*entitlement to special medicine reimbursement*, which requires meeting clinical
criteria over months — it counts fairly severe, treated, diagnosed disease, not
prevalence.

**3. It is an ecological correlation across 33 units.** Nothing is adjusted for income,
education, smoking or housing age, all of which track both motorway proximity and
morbidity. With n = 33 the study is also underpowered for anything but a large effect.

**4. Exposure is not population exposure.** Both measures are per unit of *land*, not per
resident. A district can be mostly forest beside a motorway and score high while nobody
lives there — Länsi-Pakila and Itä-Pakila top the traffic-density ranking while being among
the healthiest districts, which is exactly this artefact. Weighting by Statistics Finland's
250 m population grid is the obvious next step.

**5. One weekday of counts is a weekday, not a year.** The default is the most recent
complete weekday; `--tms-days 3` averages three. Fintraffic counts also miss seasonal
variation and say nothing about the fleet's emission standard.

## Traps already fixed in this code (don't reintroduce them)

- **Use `avoindata:Maavesi_peruspiirit` (tyyppi `Maa-alue`), not `Piirijako_peruspiiri`.**
  The latter includes each district's *maritime* area. It wrecks the map framing and
  silently dilutes every area-normalised statistic for coastal districts — switching to
  land-only moved Spearman ρ from +0.21 to +0.09.
- **`overpass.osm.ch` is a Switzerland-only extract.** It answers `200 OK` with zero
  elements for Helsinki, which looks like success and caches as valid. `_overpass()`
  rejects empty and non-JSON responses (Overpass also returns HTML "server too busy"
  bodies under HTTP 200) and rotates mirrors with backoff.
- **The 2019 workbook's single table is Finland = 100**, while 2021/2023 put Helsinki = 100
  in column A and Finland = 100 in column J. Mixing them up silently rescales everything.
- **One 0.002 km² skerry of Laajasalo sits ~16 km offshore** and stretched the map frame
  by 60%. `view_bounds()` crops the *view* to 99% of land area; all statistics still use
  full geometry.
- **Different vintages are not comparable** — the city says so explicitly. Don't build a
  time series from the 2019/2021/2023 files.
- **Traffic density must divide by the same region it sums over.** Summing roads inside the
  buffered district while dividing by the bare district area inflated small districts
  (Itä-Pakila scored twice the city centre); fixing it moved ρ from +0.28 to +0.34.
- **Drop `*_link` ways from the drawing**, not from the exposure sum: at traffic-scaled
  widths the ramp ways stack and turn every interchange into an ink blob.

## Result as it stands (2023, 300 m, motorway+trunk)

| Exposure | Spearman ρ | p | n |
|---|---|---|---|
| Traffic density (vehicle-km/day/km²) | **+0.34** | 0.054 | 33 |
| Area share within 300 m | +0.09 | 0.62 | 33 |

Weighting roads by what actually drives on them roughly quadruples the association over
plain geometric proximity — which is the expected direction if traffic matters at all, and
the main argument for pulling the counter data. It is stable against the count window
(3-weekday average: ρ = +0.339 vs. +0.338 for one day), so it is not an artefact of which
day was sampled.

It is still not evidence of a traffic effect. p = 0.054 across 33 unadjusted ecological
units is weak; the index is 6/7 non-respiratory; and the two highest-exposure districts
(Länsi-Pakila, Itä-Pakila) are among the *healthiest*, so the correlation is carried by the
inner and north-eastern districts, where income and housing stock differ too.

## If you want a real answer

The exposure side is the easy half in Helsinki: FMI's **ENFUSER** gives modelled NO₂/PM2.5
at 10–15 m resolution over the metropolitan area (open, WFS/NetCDF — near-real-time and
forecast only, so an annual mean needs accumulation). The outcome side is the binding
constraint, and the credible route is register data with residential coordinates via a
**Findata** permit, not district averages.

## Sources

- Districts (land): Helsinki WFS `avoindata:Maavesi_peruspiirit`, kartta.hel.fi — CC BY 4.0
- Index: "Helsingin sairastavuusindeksi", Kaupunkitieto / City of Helsinki (sources: Kela,
  Statistics Finland), via Helsinki Region Infoshare — CC BY 4.0
- Roads: OpenStreetMap contributors, via Overpass API — ODbL
- Traffic: Fintraffic / Digitraffic TMS (LAM) counters — CC BY 4.0
