# ADR 0003: uv workspace monorepo and package naming

- Status: Accepted
- Date: 2026-10-25

## Context

Several Python deployables share two internal packages. All of them need one consistent, locked dependency set so builds are reproducible, which validation evidence depends on.

## Decision

- The repo is a single [uv](https://docs.astral.sh/uv/) workspace. The root `pyproject.toml` lists every package and app as a member, and one `uv.lock` pins the full dependency graph. CI installs with `uv sync --frozen`.
- Each member uses the `src/` layout. Distribution names use dashes (`pharmasafe-platform`) and import names use underscores (`pharmasafe_platform`).
- The platform package is imported as `pharmasafe_platform`, not `platform`, because `platform` would shadow the Python standard library module.
- Python is pinned to 3.12, the version the build prompt specifies.
- mypy runs in strict mode across the whole repo, not only on `packages/domain`. Starting strict costs less than tightening later.
- The UI keeps its own `package-lock.json`. npm and uv are not mixed.

## Consequences

- One lock file means one SBOM, so dependency audits cover every Python deployable at once.
- Every deployable shares the same dependency versions. A conflict between deployables has to be resolved instead of being hidden.
