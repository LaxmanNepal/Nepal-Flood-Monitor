# Flood data contract

`flood-data.json` is the browser-facing snapshot. It must only contain observations that passed collector validation.

## Status semantics

- `LIVE`: fresh source observation is available.
- `STALE`: the source could not be refreshed; the previous valid snapshot is retained.
- `UNAVAILABLE`: no valid observation exists.

The UI must never infer `danger` from water level alone. Warning/danger classifications require an authoritative threshold from DHM or another explicitly documented source.
