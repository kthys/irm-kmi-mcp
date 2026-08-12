# irm-kmi-mcp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

An [MCP](https://modelcontextprotocol.io) server exposing **official Belgian weather data** from the
[Royal Meteorological Institute of Belgium](https://www.meteo.be) (IRM/KMI): observed conditions,
daily and hourly forecasts, and weather warnings, for MCP clients such as Claude, Hermes or
Openclaw.

```text
"What's the weather in Namur?"            →  current_conditions("Namur")
"Forecast for Oostende, 5 days, in Dutch" →  daily_forecast("Oostende", days=5, language="nl")
"Any warnings in Belgium right now?"      →  warnings()
```

## Features

- **Observed conditions**: temperature, condition, UV index, sunrise/sunset from the nearest
  IRM weather station.
- **Daily forecast (1–8 days)**: min/max temperature, condition, wind, precipitation, plus the
  **official IRM text bulletin** in fr/nl/en/de.
- **Hourly forecast (1–49 h)**: temperature, precipitation, pressure, wind and gusts.
- **Official weather warnings**: yellow/orange/red alerts, either for a single municipality
  (with level and validity period) or for the whole country.
- **Municipality resolution by name**: "Namur", "Bruxelles", "Oostende", … resolved through the
  IRM's own city search.
- **Caching**: weather data is cached for 10 minutes, city lookups for 24 hours.

## Tools

| Tool                  | Parameters                                       | Returns |
|-----------------------|--------------------------------------------------|---------|
| `current_conditions`  | `commune`                                        | temperature, condition (ww code + canonical English label), UV index, day/night, sunrise/sunset |
| `daily_forecast`      | `commune`, `days` (1–8, default 3), `language` (fr/nl/en/de) | per day: temps, condition (ww code + label), wind, precipitation, official text |
| `hourly_forecast`     | `commune`, `hours` (1–49, default 24)             | per hour: temperature, condition (ww code + label), precipitation, pressure, wind |
| `warnings`            | `commune` (optional), `language`                 | alert type, text; level + validity for a municipality, per-region level/validity otherwise |

## Data source & disclaimer

> ⚠️ **Unofficial.** This project is not affiliated with the IRM/KMI. It works by calling the
> same backend service that powers the official IRM mobile app. That service was never meant for
> public use: it is undocumented and it can break or disappear without notice. Please use it for **personal,
> low-frequency** purposes only.

The API surface was reverse-engineered and documented by
[Jules Dejaeghere](https://github.com/jdejaegh) in
[`jdejaegh/irm-kmi-api`](https://github.com/jdejaegh/irm-kmi-api) and
[`jdejaegh/irm-kmi-ha`](https://github.com/jdejaegh/irm-kmi-ha). Many thanks for the groundwork.
This project is **not affiliated with, sponsored or endorsed by the IRM/KMI**.

## Installation

```bash
git clone https://github.com/kthys/irm-kmi-mcp.git
cd irm-kmi-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

Run the server (stdio transport):

```bash
.venv/bin/irm-kmi-mcp
# or
.venv/bin/python -m irm_kmi_mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "irm-kmi-mcp": {
      "command": "/absolute/path/to/irm-kmi-mcp/.venv/bin/irm-kmi-mcp"
    }
  }
}
```

### Hermes Agent

```yaml
# config.yaml
mcp_servers:
  irm-kmi-mcp:
    command: "/absolute/path/to/irm-kmi-mcp/.venv/bin/irm-kmi-mcp"
    enabled: true
```

No API keys or configuration required. The daily request key is derived automatically.

## Configuration

- `IRM_LANG`: default language for official texts (bulletins, warning texts,
  day names, wind directions): `fr`, `nl`, `en` or `de`. Unset or invalid
  values fall back to English. Read once at server startup; set it before
  launching the server.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check .
```

Tests run against recorded API responses (`tests/fixtures/`) with a mocked HTTP transport,
so no network access is required. Re-record fixtures after upstream API changes.

## Roadmap

- [ ] Radar-based rain nowcast (`getIncaImage` / radar animation)
- [ ] Pollen levels (experimental, the IRM pollen SVG is volatile)
- [ ] Per-province municipality listing
