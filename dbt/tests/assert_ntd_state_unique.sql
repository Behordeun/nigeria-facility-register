-- The NTD burden table must hold at most one row per state. The coverage mart
-- joins burden to each LGA on the state, so a second row for a state multiplies
-- every LGA in that state, duplicating LGA records and double-counting the
-- burden. Existence in the frame (assert_ntd_states_in_frame) is not enough;
-- this pins uniqueness. State is normalised so two spellings of one state do
-- not hide a duplicate. Returns a row per state that appears more than once.
select
    {{ norm_state('state') }} as state_key,
    count(*)                  as n
from {{ source('raw', 'ntd_sth_state') }}
group by 1
having count(*) > 1
