-- CI proves the lineage classification only if the built output actually
-- contains all three outcomes. This guards the committed sample fixture: it is
-- shaped so carried-over, dropped and appeared each occur, and this fails if a
-- future edit to the fixture stops exercising one branch.
--
-- It runs ONLY on the fixture build (var fixture_build=true, set in CI), never
-- on a production refresh: a real vintage may legitimately contain only one or
-- two statuses (an all-new state has only 'appeared'), and requiring all three
-- there would reject a valid refresh. Disabled by default so production builds
-- skip it entirely.
{{ config(enabled=var('fixture_build', false)) }}

with present as (
    select count(distinct lineage_status) as n_statuses
    from {{ ref('stg_version_lineage') }}
    where lineage_status in ('carried-over', 'dropped', 'appeared')
)
select n_statuses from present where n_statuses < 3
