# Agent Forge metadata sources

Agent Forge is a set of independently consumable metadata sources for agent tools. It records upstream identity, discovery, and installation links. It does not inspect code, certify publishers, execute installation instructions, or guarantee safety.

## Sources

Each category is a separate source with its own manifest, index, URL, and update lifecycle. Consumers choose sources explicitly, similar to enabling repositories in a system package manager.

| Source | Category | Base URL |
| --- | --- | --- |
| `agent-forge[mcp]` | MCP servers | `https://metaone01.github.io/agent-forge/sources/mcp/` |
| `agent-forge[plugin]` | Agent plugins | `https://metaone01.github.io/agent-forge/sources/plugin/` |
| `agent-forge[skill]` | Agent skills | `https://metaone01.github.io/agent-forge/sources/skill/` |
| `agent-forge[other]` | Uncategorized and traditional tools | `https://metaone01.github.io/agent-forge/sources/other/` |

A source directory contains `source.json`, `index.json`, and optionally `packages/**/*.json`. Paths in an index are relative to that source directory. A package record must be stored in the source matching `target.type`; validation rejects cross-category placement and manifest/index mismatches. Adding or removing a first-class category requires a schema change.

The public contract is static JSON suitable for GitHub Pages. A database is not currently necessary: Git provides review and history, while static files provide cacheable and mirrorable reads. A database may later generate these files if write volume or server-side queries justify it, but it must not replace the published JSON contract.

## Schemas

The stable schema namespace is:

- `https://metaone01.github.io/agent-forge/package.schema.json`
- `https://metaone01.github.io/agent-forge/index.schema.json`
- `https://metaone01.github.io/agent-forge/advisory.schema.json`
- `https://metaone01.github.io/agent-forge/source.schema.json`

`schemaVersion` starts at `1` and increments only for breaking contract changes. A schema `$id` remains stable for compatible additions.

Known package target types are `mcp`, `agent-plugin`, and `skill`. `generic` and traditional package-manager types belong to `agent-forge[other]`. Category-specific identity data lives in `target.typeRef`. New first-class categories are added by changing the schemas and source layout, not by silently overloading `_meta`.

## Link availability

`target.verified` means publication-time checks succeeded: a URL was reachable, a supplied checksum matched, or a registry contained the identifier. `target.verifiedAt` records when that check occurred. These fields do not establish trust or safety.

`target.linkCheck` records periodic reachability checks with `lastCheckedAt`, `lastStatus`, redirect destination, and optional continuity timestamps. Operators can explain degraded links in `lifecycle.linkStatusNote`. Mirrors may be provided on both package targets and indexes.

Consumers must display this statement whenever they present `verified`, checksums, signatures, or advisory data:

> Third-party metadata. Availability checks and integrity material are not security reviews. No guarantee of accuracy, completeness, timeliness, or safety.

Script installation records require `scriptIntegrity`, but the repository does not execute or analyze scripts. Consumers must obtain confirmation before execution.

## Extensions

`_meta` is the only open extension point. Keys use a reverse-DNS namespace such as `org.example/tool`; values may contain any JSON. Consumers must preserve unknown values without interpreting them. The validator warns when serialized `_meta` exceeds 4096 UTF-8 bytes but does not reject the record.

For `mcpRef.registryBaseUrl`, maintainers currently accept the canonical public registries for the declared `registryType` and private registries controlled by the submitter. The mutable allowlist belongs in review policy rather than a schema enum.

## Contributing a record

1. Choose exactly one source by category.
2. Add the package document under `sources/<category>/packages/`.
3. Add its version and relative path to that source's `index.json`.
4. Preserve the publisher's original version string; use `versionKind` only as a comparison hint.
5. Record availability evidence in `verifiedAt` and `linkCheck` without implying a security review.
6. Run `uv run tools/validate.py --all`.

Examples for each category, a traditional package, an index, and an advisory are in `examples/`.

## Validation

Run all contract tests and repository validation:

```sh
uv run --with jsonschema --with referencing python -m unittest discover -s tests -v
uv run tools/validate.py --all
```

Validate one document against a chosen schema:

```sh
uv run tools/validate.py examples/package-mcp.json package.schema.json
```

CI performs schema and structure validation only. Periodic network probing is an operational job that writes `linkCheck`; CI success is not proof that every external URL remains reachable.
