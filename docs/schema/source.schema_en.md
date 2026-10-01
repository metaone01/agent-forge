# `source.schema.json` field reference

## Purpose

A source manifest describes one independently consumable Agent Forge metadata catalog. It may be an Agent/type projection or an all-Agent catalog. It describes metadata endpoints only; it is not a plugin-artifact mirror.

## Fields

| Field | Purpose |
| --- | --- |
| `schemaVersion` | Fixed at `2`. |
| `sourceId` | Stable machine ID such as `agent-forge:dsh:plugin`. |
| `name` | Human-readable source name. |
| `agentId` | Covered Agent; `null` means all Agents. |
| `type` | `mcp`, `plugin`, `skill`, `general`, or `bundle`. |
| `baseUrl` | Base URL containing the manifest and index. |
| `index` | Index path relative to `baseUrl`. |
| `revision` | Immutable revision shared by manifest, index, and generated assets. |
| `generatedAt` | Manifest generation time. |
| `updatedAt` | Last catalog data change. |
| `mirrorOf` | Canonical `sourceId` mirrored by this endpoint; null for a canonical source. |
| `priority` | Consumer selection priority; lower values win. |
| `official` | Operator declaration, not a security proof. |
| `description` | Catalog description. |
| `indexChecksum` | Digest declaration for the index bytes. |
| `sourceMirrors` | Metadata endpoints serving the same revision, with sourceId, URL, mirrorOf, revision, priority, region, and official. |
| `relatedSources` | Related endpoints with sourceId, URL, relation, revision, priority, and region. |
| `_meta` | Namespaced extension data. |

`sourceMirrors` switches Agent Forge metadata endpoints only. It does not describe package-artifact URLs from `package.schema.json`. Consumers should report revision conflicts or apply an explicit priority.

## TypeScript example

```typescript
const source = {
  schemaVersion: 2, // Manifest contract version.
  sourceId: "agent-forge:dsh:plugin", // Stable source ID.
  name: "Agent Forge DSH Plugins", // Human name.
  agentId: "dsh", // Agent projection.
  type: "plugin", // Package category.
  baseUrl: "https://metaone01.github.io/agent-forge/data/dsh/plugin/", // Base URL.
  index: "index.json", // Relative index path.
  revision: "20261001T000000Z", // Immutable publication revision.
  generatedAt: "2026-10-01T00:00:00Z", // Generation time.
  updatedAt: "2026-09-30T23:45:00Z", // Data update time.
  mirrorOf: null, // Canonical source has no parent mirror.
  priority: 0, // Preferred endpoint.
  official: true, // Operator declaration only.
  description: "Read-only DSH plugin metadata.", // Description.
  indexChecksum: { sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }, // Index digest declaration.
  sourceMirrors: [{ sourceId: "agent-forge-cn:dsh:plugin", url: "https://mirror.example.cn/agent-forge/data/dsh/plugin/", mirrorOf: "agent-forge:dsh:plugin", revision: "20261001T000000Z", priority: 10, region: "cn", official: false }], // Metadata mirror.
  relatedSources: [{ sourceId: "agent-forge:dsh:bundle", url: "https://metaone01.github.io/agent-forge/data/dsh/bundle/", relation: "regional", revision: "20261001T000000Z", priority: 20, region: "global" }], // Related catalog.
  _meta: { "org.example/catalog": { generatedBy: "pages" } }, // Extension data.
} as const;
```
