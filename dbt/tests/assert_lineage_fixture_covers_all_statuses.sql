-- CI proves the lineage classification only if the built output actually
-- contains all three outcomes. The accepted-values test permits any status to
-- be absent, so a fixture that happened to match every facility (or match none)
-- would pass without ever exercising the dropped or appeared branch. This fails
-- unless carried-over, dropped and appeared each appear at least once. Returns a
-- row only on failure.
with present as (
    select count(distinct lineage_status) as n_statuses
    from {{ ref('stg_version_lineage') }}
    where lineage_status in ('carried-over', 'dropped', 'appeared')
)
select n_statuses from present where n_statuses < 3
