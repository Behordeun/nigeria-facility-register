# 0005. Build the register's readable text in dbt

- Status: accepted
- Date: 2026-11-10

## Context

The dashboard shows sentences, not only numbers: why an LGA scores what it does (`priority_reasons`), a one-line summary for the map hover (`lga_summary`), and the LGA profile link. That text has to be built somewhere: in the mart, in Superset (calculated columns, chart templates), or in a separate presentation layer.

Points that shape the decision:

- Superset is the only consumer today. Its calculated columns live in the exported bundle as SQL strings, untested, and its deck.gl tooltip can show only one prepared field.
- The text quotes the score's parts. If it were assembled away from them, the wording could drift from the numbers without any test noticing.
- Missing values must read as missing ("n/a", "unknown", "not measured"), never as 0, the same rule the numeric columns follow.

## Decision

Build reader-facing text in the mart, in the same model as the figures it describes, and publish it as contract columns (`priority_reasons`, `lga_summary`). dbt tests check the wording against the figures, including how missing values are written. Superset displays these columns as they are; its own calculated columns are kept to formatting and links (`name`, `lga_profile_link`), which the bundle tests check.

## Consequences

- One wording for every reader: the dashboard, SQL Lab and anyone querying the register see the same sentence, and a change to the score's parts and its text land in one model and one review.
- The mart carries English text, so it is wider and the wording is part of the data contract: rewording a column is a contract change recorded in the CHANGELOG.
- Translation and per-audience wording are not supported. Revisit this decision when a second consumer appears (an API, a report, another language) that needs different wording; at that point move the text into a presentation model or the consumer, and keep the mart to figures.
