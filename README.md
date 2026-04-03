# Lake Burley Griffin Weather

Live wind conditions, forecasts, and water quality for Lake Burley Griffin, Canberra.

https://lbgweather.au

## Features

- **Real-time wind data** from the Canberra Yacht Club weather station (speed, gusts, direction, temperature, humidity)
- **Multi-model forecasts** from BOM, GFS, ICON, GEM, and JMA via Open-Meteo
- **Wind rose** showing direction distribution
- **Historical data** with date navigation
- **Water quality map** showing NCA monitoring results for 11 lake areas with interactive overlays
- **Lake closure warnings** for Yarralumla Bay

## Data Sources

- [Canberra Yacht Club](https://canberrayc.com/) weather station
- [Bureau of Meteorology](http://www.bom.gov.au/) (BOM) hourly forecasts
- [Open-Meteo](https://open-meteo.com/) for GFS, ICON, GEM, JMA forecast models
- [National Capital Authority](https://www.nca.gov.au/environment/lake-burley-griffin/water-quality) water quality results

## Components

| File | Purpose |
|------|---------|
| `index.html` | Main page (HTML, CSS, JS all inline) |
| `wind_logger.py` | Fetches current wind data from CYC weather station |
| `bom_logger.py` | Archives BOM hourly forecast snapshots |
| `forecast_logger.py` | Archives multi-model forecasts (BOM, GFS, ICON, GEM, JMA) |
| `water_quality.py` | Scrapes NCA water quality table, generates `water_quality.json` |
| `ellipse_editor.html` | Visual tool for editing water quality map area positions |
| `lbg_map.jpg` | Lake Burley Griffin base map for water quality overlay |

## Cron Jobs

- `wind_logger.py` + `bom_logger.py` run every few minutes to capture current conditions
- `forecast_logger.py` + `water_quality.py` run hourly

## License

MIT
