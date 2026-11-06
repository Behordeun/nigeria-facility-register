# 0001. Expose the register to Superset SQL Lab, gated by role

- Status: accepted
- Date: 2026-11-06

## Context

The Superset dashboard bundle (`superset/dashboards/nfr_coverage_burden/`) defines one database connection to the published DuckDB register. A Superset connection can be exposed to SQL Lab (`expose_in_sqllab`), which lets a user with SQL Lab permission run ad hoc `SELECT` queries against every table in the register: the serving marts, the staging models, the raw source tables and the published-vintage fingerprint, not only the datasets the dashboard uses.

Points that shape the decision:

- The connection opens the register read-only (`access_mode=read_only`, `allow_dml: false`, no file upload), so SQL Lab cannot change it.
- The register holds open data (GRID3 facilities and geoBoundaries, CC BY 4.0) and derived summaries. It has no personal data and no row- or column-level restrictions for SQL Lab to bypass.
- The ESPEN endemicity summaries are CC BY-NC-SA 3.0 IGO: non-commercial. SQL Lab does not change those terms, but it makes the full per-LGA table easy to export in one query.
- Superset gates SQL Lab by role. The default viewer role (Gamma) does not include the `sql_lab` role, and self-registration is off by default (`AUTH_USER_REGISTRATION = False`), so a user gets SQL Lab only when an admin grants it.
- Questions the dashboard does not answer (for example, which dropped facilities lie in a given LGA) need SQL. Without SQL Lab, analysts would need a copy of the DuckDB file, which is harder to control than a role.

## Decision

Keep `expose_in_sqllab: true` on the register connection, and control who can query it with Superset roles: viewers get Gamma plus access to the dashboard's datasets; the `sql_lab` role is granted to named analysts who have accepted the ESPEN non-commercial terms.

`tests/test_superset_bundle.py` pins the setting together with the read-only guarantees, so changing it fails the tests until this record is updated.

## Consequences

- Analysts can explore the register in place, against the published file, without copies.
- Access control lives in Superset roles, so it must be managed there: granting `sql_lab` is a deliberate admin action, and the role list should be reviewed when people join or leave.
- Before the dashboard is opened to anyone outside the team (self-registration, public or guest access, embedding), the checklist in `docs/DEPLOYMENT.md` ("Who can see what") must be completed. If that audience should never run SQL, revisit this decision, set `expose_in_sqllab: false` in the bundle, and update this record and the test together.
