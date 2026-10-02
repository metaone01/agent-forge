# Community metadata import: 2026-10-02

## Scope and result

The import reads the 30 repository snapshots documented in the [source inventory](source-inventory-2026-10-02.md), all 383 saved MCP Registry pages, two npm searches, and two separately verified plugin manifests found during the general-tool audit. Upstream code was neither installed nor executed. Classification uses declared installable units; metadata acceptance does not establish availability, safety, or runtime compatibility.

| Category | Previous version records | Resulting version records | Distinct packages |
| --- | ---: | ---: | ---: |
| MCP | 14,667 | 40,127 | 37,828 |
| Plugin | 310 | 17,145 | 16,967 |
| Skill | 925 | 3,556 | 2,610 |
| General | 30 | 30 | 30 |
| Bundle | 0 | 0 | 0 |

Historical versions and existing global ids are preserved. Cross-source observations merge into provenance rather than becoming duplicate packages. The resulting totals include previous records; they are not sums of the upstream source counts.

23,505 unresolved observations are retained in [candidates.jsonl](research/2026-10-02-import/candidates.jsonl), with source revision, claimed type, metadata, and a reason. They are observations, not unique packages. Examples include navigation links, installation hints without manifests, bare theme files, npm keyword matches, and four presets whose member package ids cannot yet be resolved. No records were rejected by canonical schema validation in the final preflight.

The MCP snapshot contains 38,278 latest observations; 37,813 have a representable declared package or remote endpoint. Unsupported or package-less records remain candidates. Duplicate upstream distributions/endpoints are deduplicated. Registry lifecycle declarations are preserved, including deprecation.

Each npm query yielded 5,250 unique results, below the platform-reported totals of 6,578 (`dsh-plugin`) and 9,322 (`mcp-server`). Platform pagination limits mean these two searches are incomplete. A keyword alone does not establish an installable Plugin or MCP server.

## Classification

Plugin directories are Plugin discovery sources. A declared DSH manifest or Claude marketplace installable unit establishes Plugin classification. `dsh.bundle` is a Plugin loader declaration, not an Agent Forge Bundle. A Skill is identified by an explicit `SKILL.md` path. MCP classification uses the registry's declared package/endpoint metadata. Search topics and repository names alone do not establish classification.

All original 30 General primary packages retain their classification: zero primary records become Plugins. Their README, root package metadata, and repository trees were audited at recorded revisions. General includes frameworks, runtimes, evaluation tools, memory systems, and other independently usable Agent tools. The MCP inspector is a client/development tool, not a server.

Two General projects contain separately installable Claude Code Plugins, imported as independent records:

- `mem0ai/mem0/integrations/claude-code-plugin`, version `0.3.3`, manifest `integrations/claude-code-plugin/.claude-plugin/plugin.json`.
- `promptfoo/promptfoo/plugins/promptfoo`, version `0.1.4`, manifest `plugins/promptfoo/.claude-plugin/plugin.json`. The example plugin is excluded.

The [30-item audit](research/2026-10-02-import/general-audit.json) records each decision, repository revision, project role, and separate plugin paths. Each General record also carries the audit decision under `_meta`.

## Identity and filtering

Equal names in different categories do not conflict when their global `id` values differ. Versions of the same package share an id. A global id cannot describe different `(type, name)` pairs; a category-local name cannot describe different ids. Validation and projection now reject these conflicts before producing output. Qualified repository/path names distinguish publishers and monorepo children; hashed filenames prevent Windows case collisions.

Consumers should reference dependencies and Bundle members using global ids. Source lookup and cache keys should include source, Agent, type, and name. A consumer keyed only by name can still collide and must be updated by its owner.

General is a specific category, not a wildcard for arbitrary or unclassified packages. Include it in all-types search. Exclude it from exact MCP/Plugin/Skill filters. Include it in an Agent's search only when an explicit `targets` association exists; unknown compatibility remains unknown. This repository's site already filters by exact type and Agent and uses Agent/type/name cache keys.

Canonical indexes include global id, description, keywords, and search text. Projections honor the canonical latest-version designation when that version is eligible, and keep the path/checksum/search metadata tied to that version.

## Evidence and reproduction

The machine-readable [summary](research/2026-10-02-import/summary.json) records source coverage, accepted observations, canonical counts, and apply status. [Schema rejections](research/2026-10-02-import/schema-rejections.json) are retained separately. Collection caches are local ignored files; source revisions and unresolved metadata remain in tracked evidence.

Use the existing uv installation and the project-specific `.venv`:

```sh
uv sync
uv run tools/collect_upstreams.py --help
uv run tools/import_community.py
uv run tools/import_community.py --apply
uv run python -m unittest discover -s tests -v
uv run tools/validate.py --all
```

Schema/structure checks do not replace link availability, installation, or host compatibility testing. No external publication or push is part of this local import.

The dedicated environment uses Python 3.10.16 and was initialized with the locally installed uv. Full canonical validation passed with 3,425 warnings: 3,423 retained historical-version files have no separate latest-record index path, and two records exceed the recommended `_meta` size due to provenance. These are warnings, not schema failures. Projection generation produced 60,858 version records across six nonempty Agent/type sources without projection warnings.

All 33 regression tests passed. Each generated Agent/type source passed complete schema and source-structure validation with zero errors. The checked projections were also regenerated into the local `data/` consumer directory; this remains an ignored build output rather than a publication.
