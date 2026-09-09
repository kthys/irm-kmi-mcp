# irm-kmi-mcp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![PyPI version](https://img.shields.io/pypi/v/irm-kmi-mcp.svg)](https://pypi.org/project/irm-kmi-mcp/)
[![CI](https://github.com/kthys/irm-kmi-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/kthys/irm-kmi-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server exposing **official Belgian weather data** from the
[Royal Meteorological Institute of Belgium](https://www.meteo.be) (IRM/KMI): observed conditions,
daily and hourly forecasts, a radar-based rain nowcast, pollen levels and weather warnings, for
MCP clients such as Claude, Hermes or Openclaw.

```text
"What's the weather in Namur?"            →  current_conditions("Namur")
"Forecast for Oostende, 5 days, in Dutch" →  daily_forecast("Oostende", days=5, language="nl")
"Will it rain soon in Brussels?"          →  rain_forecast("Bruxelles")
"How bad is the pollen today?"            →  pollen()
"Any warnings in Belgium right now?"      →  warnings(country="BE")
```

<p align="center">                                                                                                                 
<a href="https://glama.ai/mcp/servers/kthys/irm-kmi-mcp">                                                                          
<img alt="irm-kmi-mcp on Glama" src="https://glama.ai/mcp/servers/kthys/irm-kmi-mcp/badges/card.svg" />
</a>                                                                                                                               
</p>                                                                                                                             

## Features

- **Observed conditions**: temperature, condition, UV index, sunrise/sunset from the nearest
  IRM weather station.
- **Daily forecast (1–8 days)**: min/max temperature, condition, wind, precipitation,
  sunrise/sunset, plus the **official IRM text bulletin** in fr/nl/en/de (can be omitted to
  save tokens).
- **Hourly forecast (1–49 h)**: temperature, precipitation, pressure, wind and gusts — every
  row carries an absolute ISO-8601 timestamp (Brussels time).
- **Rain nowcast**: radar-based precipitation per 10-minute frame for roughly the next 5 hours;
  answers "will it rain soon?" far more cheaply than the hourly forecast.
- **Pollen levels**: official daily pollen levels (grasses, birch, mugwort, …) when in season.
- **Official weather warnings**: yellow/orange/red alerts, either for a single municipality
  (with level and validity period) or country-wide (filterable to BE, NL or LU).
- **Locations by name or coordinates**: "Namur", "Bruxelles", "Oostende", … resolved through
  the IRM's own city search, or given directly as WGS84 latitude/longitude.
- **Token-lean output**: unknown fields are omitted rather than sent as nulls.
- **Caching & resilience**: weather data is cached for 10 minutes, city lookups for 24 hours;
  transient network errors are retried automatically.

## Tools

Every location-taking tool accepts either `commune` (municipality name) or `latitude` +
`longitude` (WGS84).

| Tool                  | Parameters (besides location)                    | Returns |
|-----------------------|--------------------------------------------------|---------|
| `current_conditions`  | —                                                | temperature, condition (ww code + canonical English label), UV index, day/night, sunrise/sunset |
| `daily_forecast`      | `days` (1–8, default 3), `language` (fr/nl/en/de), `include_text` (default true) | per day: temps, condition (ww code + label), wind, precipitation, sunrise/sunset, official text |
| `hourly_forecast`     | `hours` (1–49, default 24)                       | per hour: absolute time, temperature, condition (ww code + label), precipitation, pressure, wind |
| `rain_forecast`       | `language`                                       | unit, official IRM hint, precipitation per 10-min frame (only frames expecting rain) |
| `pollen`              | —                                                | per-species pollen level (none, active, low, moderate, high, very high), national |
| `warnings`            | `language`, `country` (BE/NL/LU, country-wide only) | alert type (name + canonical slug), text; level + validity for a municipality, per-region level/validity otherwise |

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

From [PyPI](https://pypi.org/project/irm-kmi-mcp/):

```bash
pip install irm-kmi-mcp
```

…or run it zero-install on every invocation with
[`uvx`](https://docs.astral.sh/uv/guides/tools/) (recommended for MCP clients —
no venv to manage):

```bash
uvx irm-kmi-mcp
```

See [Development](#development) below for installing from source.

## Usage

Run the server (stdio transport):

```bash
irm-kmi-mcp
# or
python -m irm_kmi_mcp
```

### Claude Desktop

Zero-install with `uvx` (recommended):

```json
{
  "mcpServers": {
    "irm-kmi-mcp": {
      "command": "uvx",
      "args": ["irm-kmi-mcp"]
    }
  }
}
```

Or, after `pip install irm-kmi-mcp`:

```json
{
  "mcpServers": {
    "irm-kmi-mcp": {
      "command": "irm-kmi-mcp"
    }
  }
}
```

To set a default language (see [Configuration](#configuration)), add an `"env"`
key to either form, e.g. `"env": { "IRM_LANG": "fr" }`.

### Hermes Agent

After `pip install irm-kmi-mcp` (so the `irm-kmi-mcp` command is on `PATH`):

```yaml
# config.yaml
mcp_servers:
  irm-kmi-mcp:
    command: "irm-kmi-mcp"
    enabled: true
    # env:
    #   IRM_LANG: "fr"  # optional: default language for official texts
```

No API keys or configuration required. The daily request key is derived automatically.

## Configuration

- `IRM_LANG`: default language for official texts (bulletins, warning texts,
  day names, wind directions): `fr`, `nl`, `en` or `de`. Unset or invalid
  values fall back to English. Read once at server startup; set it before
  launching the server.

## Development

Install from source in an editable dev environment (requires [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/kthys/irm-kmi-mcp.git
cd irm-kmi-mcp
uv venv
uv pip install -e ".[dev]"
uv run --no-project pytest
uv run --no-project ruff check .
```

Tests run against recorded API responses (`tests/fixtures/`) with a mocked HTTP transport,
so no network access is required. Re-record fixtures after upstream API changes.
