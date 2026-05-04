# protoskipper-iec61850

IEC 61850 (MMS / GOOSE / Sampled Values) plugin for [ProtoSkipper](https://github.com/datasailors/protoskipper).

## Install

```bash
# From the monorepo root during development:
pip install -e "plugins-builtin/protoskipper-iec61850/[all,dev]"

# Once the plugin is on PyPI:
pip install "protoskipper-iec61850[all]"
```

## Status

Phase 8 — active development.  See `docs/internal/IEC61850_PLAN.md` for the
full implementation task breakdown.

| Component | Status |
|-----------|--------|
| Plugin scaffold / entry-point | ✅ P8.A.1 |
| SCL parser (Edition 2.1) | ⬜ P8.A.2 |
| SCL diff engine | ⬜ P8.A.3 |
| MMS connect / browse / read / write | ⬜ P8.B |
| GOOSE subscriber | ⬜ P8.C.2 |
| GOOSE publisher | ⬜ P8.C.3 |
| Sampled Values | ⬜ P8.D |
| IED simulator | ⬜ P8.G |
| PCAP dissector | ⬜ P8.H |
