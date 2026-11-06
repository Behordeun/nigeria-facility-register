-- Missing is not zero, for the enrichment inputs.
--
-- ntd_espen_lga: endemicity_latest_known must never hold an "Unknown ..."
-- status, and must be null exactly when year_latest_known is null. An unknown
-- that leaked into the "known" column would later rank as a measured status;
-- a known label without its year (or the reverse) means the derivation broke.
--
-- flood_event_affected_lga: each event is reported at one resolution, so a
-- state-level event (2022) is never mistaken for an LGA measurement.
--
-- births_lga: every LGA carries a positive births figure. Zero births would
-- make a later facilities-per-births rate infinite or read as "no demand"; the
-- source has a real figure for all 774, so a zero or null means a broken input.
-- Returns one row per violation.
select 'ntd_espen_lga' as source, state, canonical_lga, disease as detail
from {{ source('raw', 'ntd_espen_lga') }}
where lower(trim(endemicity_latest_known)) like 'unknown%'
   or (endemicity_latest_known is null) <> (year_latest_known is null)

union all

select 'flood_event_affected_lga', null, null, cast(event_year as varchar)
from {{ source('raw', 'flood_event_affected_lga') }}
group by event_year
having count(distinct resolution) <> 1

union all

select 'births_lga', null, shapeName, shapeID
from {{ source('raw', 'births_lga') }}
where births is null or births <= 0
