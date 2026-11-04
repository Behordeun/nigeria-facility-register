# Security Policy

## Scope

This project is a data pipeline, not a network service. It reads public health-facility datasets, transforms them in a local DuckDB file, and writes derived tables. It holds no secrets, no credentials and no personal data, and it opens no ports. The security surface is small, but a few things still matter.

## What counts as a security issue here

- A dependency with a known vulnerability (reported by `pip audit` or similar).
- A code path that could execute untrusted input, for example SQL built by string concatenation from a data value rather than a parameter.
- A path-handling bug that could read or write outside the project directory.
- Any committed secret or credential (there should be none; the pipeline needs none).

## What is not a security issue

- The accuracy or completeness of the facility data. That is a data-quality question, raised as a normal issue.
- The upstream datasets' own terms. Those are covered in LICENSE-DATA.

## Reporting

Report a suspected vulnerability privately through GitHub's "Report a vulnerability" advisory form on this repository, or by email to Muhammad Abiodun Sulaiman at abiodun.msulaiman@gmail.com. Do not open a public issue for a security report.

Please include what you found, how to reproduce it, and the impact as you see it. Expect an acknowledgement within a few days.

## Handling

A confirmed issue is fixed on a dedicated branch, shipped through a pull request, and recorded in the CHANGELOG. A fixed vulnerability gets a regression test where one is feasible, so it cannot return unnoticed.
