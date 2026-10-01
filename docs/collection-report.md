# Resource collection report

This document records the provenance of the initial metadata migration. Agent
Forge integrates metadata only: it does not execute packages, audit sources, or
guarantee availability, security, licensing, or compatibility. The published
contract is the JSON under `sources/`.

Collected at `2026-09-22T09:00:00Z`.

## Coverage summary

| Source | Upstream | Records published | Upstream universe |
| --- | --- | --- | --- |
| `agent-forge[mcp]` | MCP official registry (`registry.modelcontextprotocol.io/v0/servers`) | 14,662 | 34,802 latest records |
| `agent-forge[plugin]` | Claude official plugin marketplace (`anthropics/claude-plugins-official`) | 310 | 310 marketplace entries |
| `agent-forge[skill]` | `SKILL.md` files across public repositories | 925 | 925 enumerated skills |
| `agent-forge[general]` | Curated public agent projects on GitHub | 30 | 30 candidates |

## MCP servers

The registry was crawled in full with cursor pagination (`limit=100`, 348 pages)
into 34,802 `latest` records. Coverage of that universe:

- `active` 34,441; `deprecated`/`deleted` excluded.
- Of the active servers, 14,662 carry a package identity (`packages[]`); 19,354 are
  remote-only (`remotes[]` with no package) and 425 declare neither.
- Remote-only and package-less servers cannot satisfy the `mcpRef` contract, which
  requires `registryType` + `identifier`; they are therefore out of scope for this
  source and are counted here rather than silently dropped.

These counts describe the archived collection only. The migration retained
declared links and license strings as metadata and did not re-query registries.
Unknown values remain unknown; no migrated v2 record is a source-verification
claim.

Registry types emitted: npm 9,401; pypi 3,736; mcpb 732; oci 640; nuget 105;
cargo 48.

## Agent plugins

Built from the official Claude plugin marketplace manifest (310 entries).
Source kinds: `url` 161, `git-subdir` 97, local string 52. The pinned revision is
the entry `ref` (96) or commit `sha` (258) where present; 14 carry an explicit
`version`. Repository links and declared license values were retained as
unverified metadata.

## Agent skills

Enumerated `SKILL.md` files across public repositories, including
`anthropics/skills`, `addyosmani/agent-skills`, `ComposioHQ/awesome-claude-skills`,
and `obra/superpowers`. Names are namespaced by repository owner so identical
frontmatter names in different repositories stay distinct. Version is the pinned
default-branch commit SHA. Skill frontmatter rarely declares a license (852 of 925
remain `unknown`); repository license is used as a fallback.

## Other agent tools

Thirty public projects whose direct role is agent workflows (frameworks, runtimes,
sandboxes, memory, evaluation, observability, gateways). Metadata (name, license,
stars, topics, default branch, archived flag) is fetched live from the GitHub API;
a candidate that does not resolve is skipped rather than invented.

## Verification semantics

The v1 `verified`, `verifiedAt`, and `linkCheck` fields were intentionally removed
during migration. A record's presence means only that it was integrated into this
catalog; consumers must independently inspect and validate every distribution.
