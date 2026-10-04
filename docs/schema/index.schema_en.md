# `index.schema.json` field reference

## Purpose

An index is a compact package index for one source revision. It supports low-bandwidth discovery, version listing, and detail-path lookup; it contains no plugin binaries. The manifest may carry the index checksum so the index does not hash itself recursively.

## Fields

| Field | Purpose |
| --- | --- |
| `schemaVersion` | Fixed at `2`. |
| `sourceId` | Must match the source manifest. |
| `sourceManifest` | Relative path to the source manifest. |
| `sourceUrl` | Optional manifest URL. |
| `agentId` | Must match the manifest; null means all Agents. |
| `type` | One of the five categories and must match the manifest. |
| `revision` | Must match the manifest revision. |
| `generatedAt`/`updatedAt` | Generation and data-update times. |
| `ttl` | Recommended refresh interval in seconds. |
| `indexChecksum` | Optional index digest; producers should document normalized, self-field-omitted calculation. |
| `signature` | Declared signature material, not a security verification. |
| `packages` | Package-name to packageEntry map. |
| `_meta` | Namespaced extensions. |

Each `packageEntry` contains `latest`, `versions`, and `path`, with optional `checksum`, `recordRevision`, and administrator `subtype`. Paths must be safe relative paths.

Optional `id`, `summary`, `keywords`, and generated `searchText` support identity lookup and index-level search. Equal names may appear in different categories; names do not replace global ids. Projections preserve the canonical index's `latest` designation when that version is included, otherwise selecting a deterministic fallback. Path, digest, and search fields always describe the selected version.

## TypeScript example

```typescript
const index = {
  schemaVersion: 2, // Index contract version.
  sourceId: "agent-forge:dsh:plugin", // Source ID.
  sourceManifest: "source.json", // Relative manifest path.
  sourceUrl: "https://metaone01.github.io/agent-forge/data/dsh/plugin/source.json", // Manifest URL.
  agentId: "dsh", // Agent projection.
  type: "plugin", // Type projection.
  revision: "20261001T000000Z", // Shared revision.
  generatedAt: "2026-10-01T00:00:00Z", // Generation time.
  updatedAt: "2026-09-30T23:45:00Z", // Data update time.
  ttl: 28800, // Eight-hour refresh hint.
  indexChecksum: { sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }, // Normalized index digest declaration.
  signature: { url: "https://example.com/index.sig", type: "sigstore", keyId: "catalog-key", keyUrl: "https://example.com/catalog-key" }, // Signature material.
  packages: { "example-skin": { latest: "1.4.0", versions: ["1.3.0", "1.4.0"], path: "packages/example-skin/1.4.0.json", checksum: { sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" }, recordRevision: "20261001T000000Z", subtype: "skin" } }, // Package entries.
  _meta: { "org.example/index": { shard: "dsh-plugin-00" } }, // Extension data.
} as const;
```

Optional entry fields `facets` and `customFacets` copy controlled and user-defined labels from the selected version. Entry `updatedAt` copies its record timestamp for sorting. These are metadata, not guarantees; omit tag containers when labels are absent. They allow cards to render without fetching every package detail.

## Display media summary

Optional `packageEntry.media` copies the icon and at most the first preview from the selected package version. Each image requires a credential-free absolute HTTPS `url` and nonblank `alt`; a preview may have `theme`. The full ordered gallery belongs to the record at `path`. Omit the summary when that version has no media, even if an older version has images. Strict old index validators must be upgraded before producers emit this optional v2 field. References are unverified and do not change package identity, classification or compatibility.

## Package titles

Optional `packageEntry.displayName` copies the selected latest version's display name. Pages titles prefer `displayName`, falling back to the source-local package name. Canonical `id` is not a title or subtitle and is not shown in the default package facts. These facts show `record.name` as the package name; internal IDs remain in machine-readable data. Display names do not change identity, index keys or routes. Omit this field when the selected version has no display name; never inherit an older version's title.
