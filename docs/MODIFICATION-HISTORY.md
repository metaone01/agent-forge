# Agent Forge Modification History

This is a repository-history summary reconstructed from Git, covering the linear history from the initial baseline through `90f5a37`. Commit IDs and subjects are preserved exactly. The changes are grouped by contract intent so a future maintainer can understand why the current files look the way they do.

## Timeline

| Order | Commit | Subject | Main result |
| --- | --- | --- | --- |
| 1 | `1e9a515` | `chore: add initial schema baseline` | Added the first package, index, and advisory schemas |
| 2 | `91f0a67` | `test: define metadata source contracts` | Added initial JSON Schema contract tests and source expectations |
| 3 | `8710ab0` | `feat(schema): define extensible category sources` | Introduced stable public IDs, category-specific sources, and typed Agent targets |
| 4 | `f718a4b` | `feat(validation): enforce source category boundaries` | Added the validator, source-layout checks, and boundary tests |
| 5 | `21b78ea` | `feat: complete metadata source contract` | Added examples, README, CI, full source/index validation, and modern `$ref` registry handling |
| 6 | `90f5a37` | `fix: restrict metadata to agent tools` | Removed ordinary package-manager targets and required explicit Agent scope for `generic` |

All commits were authored and committed on 2026-09-20 (+0800), according to the Git metadata available in this worktree.

## 1. Initial schema baseline: `1e9a515`

The repository began with three schemas:

- `package.schema.json` at `https://example.com/schemas/package.schema.json`;
- `index.schema.json` at `https://example.com/schemas/index.schema.json`;
- `advisory.schema.json` with the same example namespace.

The package schema already established the broad metadata shape: package identity, descriptive fields, platform fields, dependencies/conflicts, lifecycle, security summaries, advisories, install records, and target information. At this point, target types included ordinary package-manager ecosystems such as pacman, apt, rpm, npm, PyPI, Cargo, Go, Gem, Composer, NuGet, plus a generic target.

The initial baseline also contained several package-manager-specific concepts, including repository configuration and version range hints. Installation scripts only required a URL; the later integrity requirement had not yet been introduced. There was no independent source manifest or physical source directory contract.

## 2. Contract tests: `91f0a67`

The repository added `tests/test_contracts.py` with tests for:

- MCP records requiring `target.typeRef`;
- script installation records requiring the expected shape at that stage;
- reverse-DNS `_meta` keys;
- one independent source per expected category.

The tests used `jsonschema` and a local reference store. They turned important design decisions into executable constraints before the source layout was added.

## 3. Independent category sources: `8710ab0`

This commit made the metadata source model explicit.

### Public schema identity

The three existing schema IDs moved from `example.com` to the stable GitHub Pages namespace:

```text
https://metaone01.github.io/agent-forge/package.schema.json
https://metaone01.github.io/agent-forge/index.schema.json
https://metaone01.github.io/agent-forge/advisory.schema.json
```

A new `source.schema.json` was added at:

```text
https://metaone01.github.io/agent-forge/source.schema.json
```

### Four physical source categories

The commit created `sources/mcp/`, `sources/plugin/`, `sources/skill/`, and `sources/other/`. Each received `source.json` and `index.json`. Source names and category values became explicit:

```text
agent-forge[mcp]     -> mcp
agent-forge[plugin]  -> plugin
agent-forge[skill]   -> skill
agent-forge[other]   -> other
```

`source.schema.json` tied a source name to its category through conditional rules. `index.schema.json` gained required `source` and `category` fields, TTL, mirrors, and safe relative paths.

### Typed Agent targets

The package target enum gained `agent-plugin`, `mcp`, and `skill`, alongside the existing generic path. New definitions were added for `mcpRef`, `agentPluginRef`, and `skillRef`. Conditional `typeRef` rules required the corresponding type-specific object for each target type.

The same commit also added or clarified:

- `versionKind` as a comparison hint;
- namespaced `_meta` extension data;
- publication-time `verified`/`verifiedAt` semantics;
- `linkCheck` reachability records;
- `scriptIntegrity` for script installation records;
- lifecycle link-status notes;
- advisory disclaimers and category-specific references.

At this point, the `generic` route was still broad enough to accept a generic object without explicit evidence of direct Agent use.

## 4. Cross-file validation: `f718a4b`

The repository added `tools/validate.py` and `tests/test_validate_cli.py`.

The validator initially mapped known Agent types to physical source categories and treated unknown types as `other`. It validated all four schemas, source manifests, indexes, package records, examples, and the serialized UTF-8 size of `_meta`. A package placed in the wrong category was rejected.

The test suite covered:

- target-to-source mapping;
- UTF-8 byte-size measurement for `_meta`;
- rejection of a package in the wrong category.

This was the first point where the repository enforced rules that JSON Schema alone could not express, such as the relationship between a package's target type and its directory.

## 5. Complete source contract: `21b78ea`

This commit completed the repository workflow around the schemas.

### Documentation, examples, and CI

It added:

- `README.md` describing sources, schemas, verification semantics, extensions, contribution steps, and validation;
- examples for MCP, plugins, skills, generic tools, an index, an advisory, and at that point a traditional pacman target;
- `.github/workflows/validate.yml` to run tests and repository validation on pushes and pull requests;
- `.gitignore` entries for local Hermes and Python artifacts.

The README documented static JSON as the public contract and separated CI structure checks from operational network probing.

### Validator expansion

`validate_source_directory()` was added and became responsible for:

- manifest/index schema validation;
- directory category matching;
- source name and category matching between manifest and index;
- index latest/version consistency;
- indexed path existence;
- package name and version matching against index entries;
- detection of unindexed package files as warnings.

`validate_all()` was changed to use this source-directory validator.

### Reference implementation update

The validator and tests moved from the deprecated `RefResolver` approach to `referencing.Registry` and `Resource`, with both public `$id` and local file URI registrations. The script dependency declaration gained `referencing>=0.36`.

### Category scope at this stage

The README then described `generic` and traditional package-manager targets as belonging to `agent-forge[other]`. That policy was corrected by the next commit.

## 6. Agent-only scope correction: `90f5a37`

The final commit in the current history narrowed the repository to its stated purpose: metadata for tools used in Agent workflows.

### Schema changes

`package.schema.json` removed ordinary target types:

```text
pacman, apt, rpm, npm, pypi, cargo, go, gem, composer, nuget
```

The remaining target enum is:

```text
agent-plugin, mcp, skill, generic
```

The commit removed package-manager-specific `repoConfig` and the extra package-manager comparison range values. It added `genericRef`, requiring:

- `kind`: a concise tool kind;
- `agentUse`: a bounded explanation of direct use in an Agent workflow.

The conditional target rules now require `typeRef` for `generic` as well as MCP, plugin, and skill records.

### Tests and examples

The generic example was updated with explicit `kind` and `agentUse` evidence. The pacman example was deleted. Contract tests now verify:

- ordinary target types are invalid;
- a generic target without `agentUse` is invalid;
- a generic target with both required fields is valid.

### Validator behavior

`CATEGORY_TYPES` now includes `generic: other`, and `category_for_target()` uses direct dictionary indexing rather than a default fallback. An unsupported target therefore raises `KeyError` instead of silently being classified as `other`.

### Documentation correction

The README now states that `generic` is only for Agent-related tools that do not fit MCP, plugin, or skill, and that ordinary system/language packages are out of scope. Registries such as npm, PyPI, Cargo, and OCI may still appear inside a category-specific `typeRef`; they are not source categories.

## Current result

The final repository state has:

- four stable public schema IDs;
- four independent source manifests and indexes;
- four Agent-focused package target types;
- typed category-specific identity data;
- explicit generic Agent-scope evidence;
- schema validation plus cross-file source validation;
- examples, tests, and GitHub Actions coverage;
- no package-manager-only metadata contract.

The current source indexes are empty. Representative package records remain in `examples/`, so populating `sources/<category>/packages/` and the corresponding indexes is a future content operation rather than an unfinished schema definition.

## Reconstructing or checking history

Use these commands from the repository root:

```sh
git log --reverse --format='%h %ad %s' --date=short
git show --stat --oneline <commit>
git show <commit> -- <path>
git diff 1e9a515..90f5a37 --stat
```

The history document should be updated whenever a later commit changes the public contract, source mapping, validator behavior, or the documented safety boundary.
