-- CI proves both authority paths only if the built output actually contains
-- facilities from each version. The accepted-values test permits either version
-- to be absent, and if the committed fixture ever loses its v2-only or
-- v3-covered data the build would still pass without exercising that path.
-- This fails unless both 'v2.0' and 'v3.0' appear in the authority layer, so a
-- fixture that stops covering a path is caught. Returns a row only on failure.
with versions as (
    select count(distinct source_version) as n_versions
    from {{ ref('stg_facilities_authority') }}
    where source_version in ('v2.0', 'v3.0')
)
select n_versions from versions where n_versions < 2
