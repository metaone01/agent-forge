# Resource collection report

This document records what was collected into the four independent Agent Forge
sources, from which public upstream, and how each record was verified. It is an
operational note; the published contract is the JSON under `sources/`.

Collected at `2026-09-22T09:00:00Z`.

## Coverage summary

| Source | Upstream | Records published | Upstream universe |
| --- | --- | --- | --- |
| `agent-forge[mcp]` | MCP official registry (`registry.modelcontextprotocol.io/v0/servers`) | 14,662 | 34,802 latest records |
| `agent-forge[plugin]` | Claude official plugin marketplace (`anthropics/claude-plugins-official`) | 310 | 310 marketplace entries |
| `agent-forge[skill]` | `SKILL.md` files across public repositories | 925 | 925 enumerated skills |
| `agent-forge[other]` | Curated public agent projects on GitHub | 30 | 30 candidates |

## MCP servers

The registry was crawled in full with cursor pagination (`limit=100`, 348 pages)
into 34,802 `latest` records. Coverage of that universe:

- `active` 34,441; `deprecated`/`deleted` excluded.
- Of the active servers, 14,662 carry a package identity (`packages[]`); 19,354 are
  remote-only (`remotes[]` with no package) and 425 declare neither.
- Remote-only and package-less servers cannot satisfy the `mcpRef` contract, which
  requires `registryType` + `identifier`; they are therefore out of scope for this
  source and are counted here rather than silently dropped.

Per-package identity and license were probed against the declared registry:
npm packument, PyPI JSON, crates.io, NuGet, and OCI/MCPB metadata. Registry
`server.json` carries no `license` field (0/34,802), so every license value comes
from the distribution registry probe. Where a probe did not resolve, the record
keeps `license: "unknown"` and `target.verified: false` with the observed
`linkCheck.lastStatus` (1,283 records: 1,168 network errors, 89 HTTP 404, 26
timeouts).

Registry types emitted: npm 9,401; pypi 3,736; mcpb 732; oci 640; nuget 105;
cargo 48.

## Agent plugins

Built from the official Claude plugin marketplace manifest (310 entries).
Source kinds: `url` 161, `git-subdir` 97, local string 52. The pinned revision is
the entry `ref` (96) or commit `sha` (258) where present; 14 carry an explicit
`version`. Repository licenses were resolved from each plugin's upstream GitHub
repository (197 of 223 distinct repos resolved; 79 records remain `unknown`).

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

`target.verified` means the publication-time check succeeded (a registry contained
the identifier, or the URL was reachable). `target.verifiedAt` records when.
`target.linkCheck` records the observed reachability. None of these fields imply a
security review; see the consumer statement in `README.md`.
