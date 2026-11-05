-- The mart must contain exactly the canonical frame LGA keys, not merely 774
-- rows. A count check alone passes if a frame LGA is dropped and a non-frame key
-- takes its place, so this checks set equality on (state, canonical_lga):
-- any mart key absent from the frame, or any frame key absent from the mart,
-- is returned. Both sides should be empty.
with frame as (
    select {{ norm_state('state') }} as state, canonical_lga
    from {{ source('raw', 'frame_master_index') }}
),
mart as (
    select {{ norm_state('state') }} as state, canonical_lga
    from {{ ref('lga_coverage_burden') }}
),
missing_from_mart as (
    select state, canonical_lga, 'missing_from_mart' as problem from frame
    except
    select state, canonical_lga, 'missing_from_mart' from mart
),
extra_in_mart as (
    select state, canonical_lga, 'not_in_frame' as problem from mart
    except
    select state, canonical_lga, 'not_in_frame' from frame
)
select * from missing_from_mart
union all
select * from extra_in_mart
