# Agent Forge metadata sources

Agent Forge is a set of independently consumable metadata sources for agent tools. It records upstream identity, discovery, and installation links. It does not inspect code, certify publishers, execute installation instructions, or guarantee safety.

## Sources and mirrors

Each category is a separate source with its own manifest, index, URL, and update lifecycle. Consumers choose sources explicitly, similar to enabling repositories in a system package manager.

| Source | Category | Base URL |
| --- | --- | --- |
| `agent-forge:dsh:mcp` | DSH MCP servers | `https://metaone01.github.io/agent-forge/data/dsh/mcp/` |
| `agent-forge:dsh:plugin` | DSH plugins | `https://metaone01.github.io/agent-forge/data/dsh/plugin/` |
| `agent-forge:dsh:skill` | DSH skills | `https://metaone01.github.io/agent-forge/data/dsh/skill/` |
| `agent-forge:dsh:general` | DSH general tools | `https://metaone01.github.io/agent-forge/data/dsh/general/` |
| `agent-forge:dsh:bundle` | DSH bundles | `https://metaone01.github.io/agent-forge/data/dsh/bundle/` |

A source directory contains `source.json`, `index.json`, and optionally `packages/**/*.json`. Paths in an index are relative to that source directory. A source manifest identifies its `sourceId`, Agent projection, type, revision, and optional `sourceMirrors`. These are metadata mirrors only; package installation candidates are stored separately in a package's `distributions` array.

The public contract is static JSON suitable for GitHub Pages. A database is not currently necessary: Git provides review and history, while static files provide cacheable and mirrorable reads. A database may later generate these files if write volume or server-side queries justify it, but it must not replace the published JSON contract.

## Schemas

Human-readable bilingual field references and fully annotated TypeScript-style examples are in [`docs/schema/`](docs/schema/README.md).

The stable schema namespace is:

- `https://metaone01.github.io/agent-forge/package.schema.json`
- `https://metaone01.github.io/agent-forge/index.schema.json`
- `https://metaone01.github.io/agent-forge/advisory.schema.json`
- `https://metaone01.github.io/agent-forge/source.schema.json`

`schemaVersion` is currently `2` and increments only for breaking contract changes. A schema `$id` remains stable for compatible additions.

Package types are `mcp`, `plugin`, `skill`, `general`, and `bundle`. `generalDetails.toolType` and `agentUse` describe a general Agent tool without creating another first-class type. Ordinary system or language packages are out of scope. Registries such as npm, PyPI, Cargo, and OCI may appear in `mcpDetails` or `distributions`; they are not metadata categories.

## Link availability

`checksum` and `signature` fields are recorded claims or supplied materials. They do not establish trust or safety.

Catalog `sourceMirrors` record alternate metadata endpoints with a shared revision. Package `distributions` record alternate plugin/tool locations. Consumers must not silently treat one layer as the other.

Consumers must display this statement whenever they present checksums, signatures, or advisory data:

> Third-party metadata. Availability checks and integrity material are not security reviews. No guarantee of accuracy, completeness, timeliness, or safety.

Script installation records require `scriptIntegrity`, but the repository does not execute or analyze scripts. Consumers must obtain confirmation before execution.

## Extensions

`_meta` is the only open extension point. Keys use a reverse-DNS namespace such as `org.example/tool`; values may contain any JSON. Consumers must preserve unknown values without interpreting them. The validator warns when serialized `_meta` exceeds 4096 UTF-8 bytes but does not reject the record.

Registry URLs are metadata declarations. The mutable registry allowlist belongs in review policy rather than a schema enum.

## Contributing a record

1. Choose exactly one source by category.
2. Add the package document under `sources/<category>/packages/`.
3. Add its version and relative path to that source's `index.json`.
4. Preserve the publisher's original version string; use `versionScheme` only as a comparison hint.
5. Record upstream links and distribution candidates without implying a security review.
6. Run `uv run tools/validate.py --all`.

Examples for each category, an index, and an advisory are in `examples/`.

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
