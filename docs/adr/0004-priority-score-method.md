# 0004. Score LGA priority as an equal-weight sum of ordinal points

- Status: accepted
- Date: 2026-11-10

## Context

`lga_coverage_burden` ranks all 774 LGAs for attention with `priority_score` and reads it as `priority_tier`. The score has changed meaning once already: 0.2.0 introduced it as 0-8 (burden, coverage, access), and 0.6.0 added malaria, moving it to 0-11 with new tier cut-offs. Both changes were recorded in the CHANGELOG. The method behind them was not, so the next change could not be judged against it.

Points that shape the method:

- The inputs are on unrelated scales: ESPEN endemicity ranks (0-3, five diseases, some unknown), facilities per 10,000 people (continuous, skewed), travel time and flood flags (yes/no), and a modelled malaria parasite rate in which nearly every LGA sits between 10% and 35%.
- No outcome is available to fit weights against. Any weighting is a judgment, so it must be simple enough for a reader to check by hand and to argue with.
- An unmeasured input must never make an LGA look less urgent.
- The original rule, `priority_high_burden_thin_coverage`, is kept beside the score so the two can be compared.

## Decision

Each part becomes a small ordinal number of points, and the score is their plain sum:

- Burden, 0-3: the highest known endemicity rank across the five diseases (`greatest`, which ignores nulls). The maximum rather than a sum or mean, so one severe burden is not outranked by several mild ones, and an LGA with fewer known diseases is not penalised or rewarded for the gaps. `ntd_diseases_known` says how many ranks the maximum was taken over.
- Coverage, 0-3: national quartile of facilities per 10,000 people, lowest quarter 3. A value on a boundary takes the less urgent band.
- Access, 0-2: one point for hard to reach on foot (at least a quarter of residents over an hour's walk), one for flooding in 2024. The 2022 state flag is left out because it marks 727 of 774 LGAs.
- Malaria, 0-3: national quartile of the parasite rate, highest quarter 3. Relative, because fixed prevalence bands would not separate LGAs. A value on a boundary takes the more urgent band.

The score is null when any part is null, never low. The tier reads the score on fixed cut-offs: high 8-11, medium 6-7, low 0-5. High was set to take roughly the top tenth of LGAs on the data when the cut-offs were chosen (74 of 774), so a later vintage can move that count; the cut-offs are not re-fitted per vintage, so a tier keeps one meaning across releases.

Every part is published as its own column, so any reader can rebuild the score under another weighting.

## Consequences

- The score is transparent: the "why" text and the map hover spell out each part, and a dbt test recomputes every part, the score and the tier.
- Quartile parts are relative: an LGA's coverage or malaria points can change because other LGAs changed. A score from one vintage is a ranking within that vintage.
- Equal weights give access less pull (0-2) than the other parts (0-3). This is deliberate: access is the coarsest part, two yes/no flags.
- `scripts/score_sensitivity.py` re-ranks the LGAs under alternative weightings and reports how much of the high tier survives each one. On the current register (774 scored LGAs, 74 high), rescaling every part to 0-1 keeps 74 of 74 with 97% overlap, so the access range is not what decides the list. Dropping malaria keeps 34 of 74 (29% overlap) and dropping coverage 45 (38%), so those two parts carry most of the list; dropping burden or access keeps all 74 but widens the top group through ties. Doubling any single part keeps at least 71 of 74.
- Changing a part, a weight or a cut-off changes the meaning of a published column. It is a breaking change for readers of the score: record it in the CHANGELOG with the high-tier overlap before and after, run the sensitivity check, and update this record.
