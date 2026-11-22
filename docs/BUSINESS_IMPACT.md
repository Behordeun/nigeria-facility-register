# Nigeria Facility Register: Business Case & Impact

Nigeria doesn't have one trustworthy list of its own health facilities. It has several, they disagree with each other, and nobody can say with confidence which one is right for a given state right now. This project builds that missing list, version-aware and joined to disease burden down to the LGA level, so that the people who decide where health money and staff go have something solid to stand on instead of guessing.

## The problem

GRID3 is the only national source for this data, and it has published two versions that don't talk to each other. v2.0 covers the whole country: 51,022 facilities, current as of October 2024. v3.0 covers 24 states: 41,778 facilities, current as of December 2025, and it uses a completely different id scheme from v2.0. There's no shared key linking a facility in one version to the same facility in the other. So anyone using this data either commits to one version and accepts it's partly stale, or builds their own reconciliation from scratch.

That second problem compounds the first. Without something tracking which state sits on which version, nobody downstream can tell if a number reflects last December or late 2024, and a plan built on the wrong one fails quietly, not loudly. Claims systems, surveillance tools, donor dashboards: each one currently solves "where are the facilities" on its own, which means duplicated effort and numbers that don't match across systems that are supposedly describing the same country.

## Who benefits

| Stakeholder | What they actually need | What this delivers today |
| --- | --- | --- |
| Federal and state health ministries | Where to build, staff, and supply next | A per-LGA table of facility counts, coverage per 10,000 people, and a 0 to 11 priority score flagging the places with high burden and thin coverage |
| Donors and funders (WHO, Gates Foundation, GFF) | A base they can trust enough to size an intervention around | One layer that states its source version and freshness per state, instead of forcing a funder to reconcile conflicting lists themselves |
| Civil society, researchers, journalists | Public data they can check for themselves | An open data export and a public dashboard, so a claim about facility access isn't something you just have to take on faith |
| Disease-control programmes (NTD, malaria) | Facility coverage next to burden data, not two separate spreadsheets | ESPEN NTD ranks and Malaria Atlas Project rates joined to facility coverage on the same 774-LGA grid |
| Health insurance schemes (NHIA and state schemes) | A way to tell a real facility from a fake one before paying a claim | Not built yet. The facility id is stable enough to support claims validation, but no insurance scheme is integrated today |
| Health-IT systems (openIMIS, DHIS2) | One shared key for facility identity | Not built yet. The id and lineage table are designed to make that integration possible, but there's no current openIMIS or DHIS2 consumer of this register |

## Where the value actually shows up

Four places, and all of them are about the register acting as plumbing other decisions run through, not as a dataset someone browses once and forgets.

**Investment gets pointed at the right places.** The priority score pulls together disease burden, per-capita facility coverage, travel-time access, and malaria incidence into one ranked list of LGAs. A ministry or donor deciding where the next facility or outreach team goes can start from that list instead of paying for a fresh survey every time.

**Claims leakage could go down, once something is wired to it.** A stable facility id is what an insurance system would need to stop paying out for care supposedly delivered somewhere that doesn't exist, or exists twice under two different names. That's a real integration project, not something this register does on its own; no claims system currently checks against it.

**Systems that rebuild their own facility list don't have to.** The v2-to-v3 lineage table matches 89.8% of v2.0 facilities to a v3.0 point within 100 metres, which means a platform (claims software, a DHIS2 instance, a donor tracker) built on the older version wouldn't be stranded if it adopted the newer data through this register. That's a capability the register has today; no such platform has adopted it yet.

**Someone outside government can actually check the claims.** The open export and the public dashboard mean a journalist, a community group, or an auditor can go look for themselves: is this facility that's supposed to exist in my LGA actually there, and is it marked functional? That accountability didn't have an independent source before.

## Why you should actually believe the numbers

A register that's confidently wrong is worse than no register at all, because people act on it anyway. A few choices here exist specifically to stop that from happening.

Missing data stays missing. It's never quietly turned into a zero. A stale state, an un-surveyed burden unit, a facility whose name didn't match to an LGA (roughly 3% of them): all of these get flagged, not folded into a count that reads as "no facilities here." That distinction matters for money. A funder reading a zero as "nothing exists" instead of "this hasn't been matched yet" ends up allocating in the wrong direction.

A facility that vanishes or appears between GRID3 versions goes into a review queue, not straight into "closed" or "opened." That stops a data-matching glitch from turning into a false claim about a facility shutting down.

And the whole thing runs on a written, tested data contract: column types, keys, grain, freshness, all enforced by dbt rather than left to whoever's writing the query that day. Anyone building on top of this, whether it's an insurance platform or a grad student's thesis, can trust the contract will hold instead of finding out the hard way that it broke. Details are in [`DATA_CONTRACT.md`](DATA_CONTRACT.md).

## What could actually limit this

This isn't a guaranteed win, and it's worth being honest about where it could fall short.

The register is only as fresh as GRID3 chooses to make it. If GRID3's publishing cadence slows down or stops, the freshness advantage this project depends on erodes, and there's no independent way to go collect that data if the upstream source dries up.

The coverage gap is real right now, not hypothetical: 13 of 37 states are still running on v2.0, which by the time you read this is over a year old. Any claim about business impact in those states is weaker than the same claim in a v3.0 state, and that gap should be said out loud rather than glossed over.

Existing isn't the same as being used. The register only creates value once an insurance scheme, a ministry, or a donor actually wires their workflow to it. Nothing about building this forces that adoption; it still has to be sold and integrated, one agency and one state at a time.

And the NTD burden data specifically carries a CC BY-NC-SA licence, which rules out a commercial product built directly on top of it without a separate licensing conversation.

None of the insurance, openIMIS, or DHIS2 benefits above are live integrations. They describe what the facility id and lineage table are built to support, not a partnership or pilot that exists right now. Treat those as a pitch for future work, not a claimed result.

## How you'd actually know it's working

| Signal | What it tells you |
| --- | --- |
| Share of states on v3.0 vs. v2.0 | How much of the national picture is current versus stale |
| Number of outside systems (claims, DHIS2, dashboards) referencing the facility id | Real adoption, not just that the data exists somewhere |
| High-burden, low-coverage LGAs that get resolved over time | Whether the priority list is changing actual spending decisions |
| v2-to-v3 match rate and the name-reconciliation backlog | Whether data quality is trending up, not a one-time snapshot |
| Public dashboard traffic and open-data export downloads | Whether the transparency case is being used, not just offered |
