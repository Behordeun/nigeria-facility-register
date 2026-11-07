# Contributing

Thanks for looking at this project. It is a small, focused data pipeline, and the contribution model is kept simple.

## Branching

- `development` is the integration branch. Everyday work lands here through pull requests.
- `main` is the release branch. It is cut from `development` through a release pull request when a milestone is ready, not on every change.
- Feature and fix branches are named for the change they carry, with a type prefix and a concrete description: `feat/dagster-schedule`, `fix/lga-name-join`, `chore/project-governance-docs`. Avoid vague names like `improvements` or `cleanup`.

## Making a change

1. Branch off `development`.
2. Keep one logical concern per branch, and under about 30 changed files so a reviewer can read it.
3. Run the checks locally before you push:
   ```
   make lint
   make test
   ```
4. Open a pull request into `development`. Describe what changed and why, and note what you tested.

## Code and data

- Code is AGPL-3.0-or-later. Data carries its own terms; see LICENSE-DATA before adding or redistributing any dataset.
- Do not commit raw third-party datasets. Add the provenance to `data/sources.yml` and fetch them at build time.
- Keep "missing is not zero" true everywhere: an absent row is an absence, never a measured zero.
- Dependencies are pinned exactly and locked in `uv.lock`. Dependabot (`.github/dependabot.yml`) opens weekly update PRs against `development`, and moves packages that must stay in step as one group (dagster with dagster-webserver, and the dbt packages), for security fixes as well as version updates. Review its PRs like any other: CI must pass before merge.

## Writing

Commit messages, pull request descriptions and docs read as plain, direct prose: short sentences, active voice, no filler. The subject line of a commit is one short imperative line; the body is flowing paragraphs, not hard-wrapped.
