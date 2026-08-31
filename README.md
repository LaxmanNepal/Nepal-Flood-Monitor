# 🇳🇵 Nepal Flood Monitor

An independent, mobile-first map for visualizing Nepal river and flood-monitoring information.

## Architecture

```text
DHM public hydrology data
        ↓
GitHub Actions (15 min)
        ↓
scripts/update_flood_data.py
        ↓
data/flood-data.json
        ↓
Leaflet map + station dashboard
        ↓
GitHub Pages / custom domain
```

## Current implementation

- Responsive interactive Nepal map using Leaflet.
- Search by station, river/basin or district.
- Status filtering for normal/warning/danger.
- Station summary cards and map popups.
- GitHub Actions scheduled updater every 15 minutes.
- Fail-closed data updater: it does not publish partial data when DHM changes its markup/API.
- GitHub Pages deployment workflow.

## Data source

The primary source is the Department of Hydrology and Meteorology (DHM), Government of Nepal:

- https://dhm.gov.np/hydrology/river-watch
- https://dhm.gov.np/hydrology/river-watch-map
- https://dhm.gov.np/hydrology/realtime-stream

DHM currently presents River Watch as a JavaScript-driven interface. The public page exposes station fields such as station number, basin, station name, district, water level, warning level, danger level, trend and status. The exact internal data endpoint is intentionally not hard-coded until it is verified.

## Important

This project is **not an official DHM service**. It should not be presented as an official emergency-warning authority. During an emergency, users should verify information with DHM and local authorities.

## Roadmap

1. Identify and verify DHM's current machine-readable River Watch endpoint.
2. Build a complete station-coordinate registry.
3. Add historical observations and trend charts.
4. Add rainfall monitoring.
5. Add official flood bulletins and forecast layers.
6. Add district/province/basin filters.
7. Add PWA/offline caching and resilient CDN delivery.
8. Add alert/deep-link views without making unsupported prediction claims.
