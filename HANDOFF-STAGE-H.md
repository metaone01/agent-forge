# HANDOFF-STAGE-H

## Handoff purpose

This handoff records the completed repository-definition and history-documentation stage for Agent Forge. It is written for a fresh context that must continue from the current worktree without relying on prior conversation.

## Repository state

- Worktree branch: `wt/t_f1d08aa9`
- Starting commit: `90f5a37` (`fix: restrict metadata to agent tools`)
- Public contract: static JSON files validated against JSON Schema Draft 2020-12
- Stable schema IDs are under `https://metaone01.github.io/agent-forge/`
- First-class source categories: `mcp`, `plugin`, `skill`, `other`
- Package target types: `mcp`, `agent-plugin`, `skill`, `generic`
- `generic` is restricted to direct Agent workflow tools and requires `target.typeRef.kind` plus `target.typeRef.agentUse`
- Ordinary OS and language package records are out of scope
- Physical source indexes currently contain no package entries

## Work completed in this stage

Added the following documentation files:

- `docs/ARCHITECTURE.md`
  - repository map;
  - stable schema namespace and responsibilities;
  - source/category mapping;
  - package, target, typeRef, integrity, installation, lifecycle, security, advisory, and extension rules;
  - validator pipeline and cross-file invariants;
  - CI commands and current limitations;
  - procedure for adding a package record or a new first-class category.
- `docs/MODIFICATION-HISTORY.md`
  - Git-derived history for commits `1e9a515`, `91f0a67`, `8710ab0`, `f718a4b`, `21b78ea`, and `90f5a37`;
  - exact contract evolution from the initial broad package schema to Agent-only metadata;
  - notes on source layout, validator behavior, examples, CI, and the `RefResolver` to `referencing.Registry` transition.
- `HANDOFF-STAGE-H.md`
  - this continuation record.

No schema, validator, example, test, or source JSON was changed in this stage.

## Verification evidence

Run from the repository root:

```sh
uv run --with jsonschema --with referencing python -m unittest discover -s tests -v
uv run tools/validate.py --all
```

Observed result during this stage:

- `Ran 13 tests in 1.508s`
- `OK`
- `validation passed (0 warning(s))`

The working tree was clean before documentation files were added. After this stage, expected changes are the three new documentation files only.

## Important files for the next context

- `README.md`: concise public-facing contract and contributor workflow.
- `package.schema.json`: main package record schema; root and nested objects are mostly closed with `additionalProperties: false`.
- `source.schema.json`: source manifest names and category coupling.
- `index.schema.json`: source index shape and safe relative paths.
- `advisory.schema.json`: advisory snapshot wrapper.
- `tools/validate.py`: semantic checks that span multiple files.
- `tests/test_contracts.py`: static schema contract tests.
- `tests/test_validate_cli.py`: validator and source-layout tests.
- `sources/<category>/source.json`: independent source manifests.
- `sources/<category>/index.json`: currently empty package indexes.

## Recommended next-stage choices

The next stage is a content or operational stage, not a schema-definition stage. Decide one of these scopes before editing:

1. Populate one source category with real package records.
   - Copy or adapt a representative record into `sources/<category>/packages/`.
   - Add the exact package name, latest version, all indexed versions, and relative path to that source's `index.json`.
   - Preserve upstream version strings and record evidence without implying safety review.
   - Run both validation commands.
2. Add an operational link-check job.
   - Keep it separate from schema/CI validation.
   - Update `target.linkCheck` only from actual reachability observations.
   - Do not treat HTTP success as trust or security approval.
3. Add stronger semantic validation tests.
   - Candidate rules include `verified`/`verifiedAt` consistency, checksum verification against indexed bytes, index/source base URL checks, and advisory status/timestamp relationships.
   - Each new rule should be justified as a contract requirement before implementation.
4. Review the public contract for a version-2 breaking change.
   - Do not add a new first-class category by overloading `_meta`.
   - Update all four schemas, category mapping, source layout, tests, examples, README, architecture, and history together.

Do not combine source population, operational probing, and a schema redesign in one unbounded change. They have different evidence and review requirements.

## Constraints to preserve

- Do not execute or analyze upstream installation scripts.
- Do not reintroduce ordinary pacman, apt, rpm, npm, PyPI, Cargo, Go, Gem, Composer, or NuGet target types as first-class package targets.
- Registries can appear inside category-specific `typeRef` objects; registries are not metadata source categories.
- Preserve unknown namespaced `_meta` values without interpreting them.
- Keep the public JSON files as the consumer contract even if another storage backend is introduced later.
- Update documentation whenever a later commit changes schema IDs, target enums, category mapping, validator semantics, or safety disclaimers.
