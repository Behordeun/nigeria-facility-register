-- The 774-LGA frame must be unique on its normalised (state, canonical_lga)
-- key. A raw count of 774 rows is not enough: two rows that normalise to the
-- same state (two spellings of one state) and the same LGA would duplicate one
-- logical LGA while another is missing, yet still total 774. That malformed
-- frame would pass assert_coverage_keys_match_frame (set comparison) and
-- assert_coverage_lga_state_unique (which groups on the raw state), so the
-- duplicate is only caught here, at the source. This fails on any normalised
-- key that appears more than once. Returns a row per duplicated key.
select
    {{ norm_state('state') }} as state_key,
    canonical_lga,
    count(*)                  as n
from {{ source('raw', 'frame_master_index') }}
group by 1, 2
having count(*) > 1
