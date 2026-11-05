# Documentation

Where to look for what.

| Document | What it covers |
| --- | --- |
| [../README.md](../README.md) | What the project produces, how it is built, how to run it, and the licensing split. Start here. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The data flow from raw sources through staging to the serving marts, the spatial-matching approach behind the lineage, and the orchestration lifecycle. Diagrams are Mermaid, rendered inline on GitHub. |
| [DATA_CONTRACT.md](DATA_CONTRACT.md) | The promise each published table makes: grain, keys, column types, nullability, value domains, the "missing is not zero" rule, and the freshness and versioning policy. Enforced by dbt model contracts and tests. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Running an always-on deployment: the Dockerfile and docker-compose (webserver + daemon), shared state, placing raw inputs, and the security notes. The dev/CI loop does not need this. |
| [../data/ATTRIBUTION.md](../data/ATTRIBUTION.md) | Source provenance and the licence each input carries (GRID3, geoBoundaries, ESPEN), and which derived artifacts inherit non-commercial terms. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | How to set up, build, test, and open a change, and the git and review expectations. |
| [../SECURITY.md](../SECURITY.md) | How to report a security issue. |
| [../CHANGELOG.md](../CHANGELOG.md) | What changed in each release, newest first. |
