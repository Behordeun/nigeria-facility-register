# Attribution

Every source feeding this project, with its license and link. Reuse of the
derived artifacts must carry this attribution.

| Source | What it provides | License | Link |
|---|---|---|---|
| GRID3 NGA Health Facilities v2.0 | National facility points, to Oct 2024 | CC BY 4.0 | [HDX](https://data.humdata.org/dataset/grid3-nga-health-facilities-v2-0) |
| GRID3 NGA Health Facilities v3.0 | Facility points for 24 states, to Dec 2025 | CC BY 4.0 | [HDX](https://data.humdata.org/dataset/grid3-nga-health-facilities-v3-0), DOI 10.7916/s6cq-1q98 |
| geoBoundaries ADM2 | 774-LGA boundaries and centroids; a simplified boundary polygon set is vendored for the choropleth (see data/sources.yml) | CC BY 4.0 | [geoBoundaries](https://www.geoboundaries.org) |
| ESPEN (WHO/AFRO) | NTD endemicity: a per-state STH summary, and a per-LGA summary for STH, schistosomiasis, lymphatic filariasis, onchocerciasis and Loa loa | CC BY-NC-SA 3.0 IGO | [ESPEN Nigeria](https://espen.afro.who.int/countries/nigeria) |
| NASA MODIS flood products | Per-LGA flood-event exposure flags (2022, 2024), summarised in the author's flood_health project | NASA open data | [NASA Earthdata](https://www.earthdata.nasa.gov) |
| WorldPop | Annual births per LGA (2015, UN-adjusted), zonal-summed into the 774 LGAs | CC BY 4.0 | [WorldPop](https://www.worldpop.org) |

GRID3 datasets are produced by the Center for Integrated Earth System
Information (CIESIN), Columbia University, with funding from the Bill & Melinda
Gates Foundation.

ESPEN data is non-commercial. See LICENSE-DATA.

WorldPop births: WorldPop (www.worldpop.org - School of Geography and
Environmental Science, University of Southampton). Flood exposure derives from
NASA MODIS products; attribution to NASA is requested.

Only small derived per-LGA tables of these sources are committed (see
data/sources.yml); the raw files are not redistributed.
