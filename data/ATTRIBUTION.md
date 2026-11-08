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
| WorldPop Global2 R2025A | Constrained population counts (about 100 m), summed per LGA for each year 2025-2030 | CC BY 4.0 | [WorldPop](https://hub.worldpop.org/geodata/listing?id=135), DOI 10.5258/SOTON/WP00839 |
| Malaria Atlas Project | Travel time to the nearest health facility (2020 release, motorised and walking-only, about 1 km), population-weighted per LGA | CC BY (MAP catalogue: 4.0; MAP open-access policy: 3.0) | [MAP](https://malariaatlas.org), Weiss et al., Nature Medicine 26, 1835-1838 (2020) |
| Malaria Atlas Project | P. falciparum parasite rate (age 2-10) and clinical incidence (202608 release, 2025, about 5 km), population-weighted per LGA | CC BY (MAP catalogue: 4.0; MAP open-access policy: 3.0) | [MAP](https://malariaatlas.org) |

GRID3 datasets are produced by the Center for Integrated Earth System
Information (CIESIN), Columbia University, with funding from the Bill & Melinda
Gates Foundation.

ESPEN data is non-commercial. See LICENSE-DATA.

WorldPop births and population: WorldPop (www.worldpop.org - School of
Geography and Environmental Science, University of Southampton). The R2025A
population counts are part of the Global Demographic Data Project, funded by
the Bill & Melinda Gates Foundation (INV-045237).

Travel time to healthcare: Weiss, D.J., Nelson, A., Vargas-Ruiz, C.A. et al.
Global maps of travel time to healthcare facilities. Nature Medicine 26,
1835-1838 (2020). Malaria parasite rate and incidence: Malaria Atlas Project, 202608 release. Data from the Malaria Atlas Project (malariaatlas.org). Flood exposure derives from
NASA MODIS products; attribution to NASA is requested.

Only small derived per-LGA tables of these sources are committed (see
data/sources.yml); the raw files are not redistributed.
