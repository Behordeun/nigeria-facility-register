# 0003. Publish the dashboard through a Cloudflare Tunnel with a shared view-only account

- Status: accepted
- Date: 2026-11-10

## Context

The dashboard runs on a single small host (DEPLOY_ORACLE.md) with both UIs bound to the host's loopback and reached over SSH. It is to be opened to the public: anyone given the address and a login should be able to read and filter it, but not download the data behind it.

Points that shape the decision:

- The Dagster UI has no authentication and must never be reachable from the internet. Superset has its own login.
- The host has one OCPU. Server-side dashboard downloads run headless Chromium on it, so unbounded public use of them would compete with refreshes and page loads.
- The domain's DNS is already on Cloudflare, whose tunnels are free, need no inbound port, and terminate HTTPS with a managed certificate.
- Superset's Gamma role, the usual viewer, can export chart data, drill to the rows behind a chart and build charts in Explore. Its permissions also change between Superset releases.
- A login handed to the public is effectively public. What it protects is the ability to cut access off (rotate the password) and to keep crawlers away from a small host, not secrecy.
- A browser must receive a chart's rows to draw it, and Superset's chart-data API answers any query on a dataset the user may read. No role setting closes that.

## Decision

Publish Superset alone through a Cloudflare Tunnel: a `cloudflared` container, started only with the compose profile `public`, on a network it shares with Superset and nothing else. Superset's own port stays on the host loopback. When `SUPERSET_PUBLIC_URL` is set, Superset trusts one proxy hop, marks its session cookie HTTPS-only and rate-limits sign-ins in Redis.

Give the public one shared account, `public-viewer`, with a `Public viewer` role built from an explicit allowlist in `scripts/superset_public_viewer.py`: read the dashboard and its charts, keep filter state, open permalinks, and access exactly the datasets the dashboard's charts read. Nothing that exports, queries freely, builds charts, renders server-side downloads, creates shared objects or edits the account. The script applies the role and the account's password from `.env` on every run, and a test holds the allowlist clear of those permissions.

## Consequences

- The host opens no port beyond SSH, and the tunnel cannot reach the Dagster UI, Redis or Postgres even if its route in the Cloudflare dashboard is changed.
- Public viewers get no CSV, Excel, PDF or image download and no SQL. The dashboard's Download menu still appears, because Superset 4.1 shows it to every role, and the server refuses the request.
- Rows of the dashboard's eight datasets can still be collected through the chart-data API by someone with the login. This is accepted: the register is derived from openly published sources and the same figures are on screen. A dataset that must not be reachable that way has to stay off the public dashboard; ESPEN's non-commercial terms still apply to its columns.
- The shared password can be rotated at any time by editing `.env` and re-running `make public-viewer`. Sessions already signed in last at most 12 hours (the public session lifetime); deactivating the account in Superset's user list ends them at once.
- The setup depends on Cloudflare for DNS and the tunnel. Moving off it means a reverse proxy with its own certificate and an open port 443 on the host and in the cloud firewall.
