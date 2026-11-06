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
--
-- access_population_lga: every LGA has a positive population (the same reasoning
-- as births); travel minutes are never negative, and the two shares stay within
-- 0-1. A null travel figure or share is allowed (the range checks pass a null
-- by design) and means no population on valid
-- travel-time cells; it is never written as 0 minutes.
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

union all

select 'access_population_lga', null, shapeName, shapeID
from {{ source('raw', 'access_population_lga') }}
where population is null or population <= 0
   or motorised_minutes < 0 or walking_minutes < 0
   or walking_over_60min_share < 0 or walking_over_60min_share > 1
   or access_population_share < 0 or access_population_share > 1
