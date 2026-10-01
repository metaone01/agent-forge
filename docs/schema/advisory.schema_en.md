# `advisory.schema.json` field reference

## Purpose and boundary

An advisory is a third-party security-notice snapshot or integration record. It is not an Agent Forge detection result. `disclaimer` must state that the data is third-party and carries no guarantee of accuracy, completeness, timeliness, or safety. Agent Forge does not scan upstream code or establish that a notice is genuine.

## Fields

| Field | Purpose |
| --- | --- |
| `schemaVersion` | Fixed at `2`. |
| `id` | Stable advisory ID such as GHSA, CVE, OSV, or a source-defined ID. |
| `aliases` | Other IDs for the same notice. |
| `source` | Advisory source name. |
| `sourceUrl` | Source page. |
| `sourceRevision` | Revision of the external advisory source. |
| `asOf` | Time represented by this snapshot. |
| `status` | draft, published, withdrawn, or superseded. |
| `publishedAt`/`withdrawnAt` | Advisory status times. |
| `ttl` | Recommended refresh interval in seconds. |
| `disclaimer` | Required boundary statement. |
| `affectedPackages` | Agent Forge package and range selectors. |
| `advisories` | Detailed advisory objects, including severity, CVSS, CWE, affected ranges, and references. |
| `signature` | Declared external signature material. |
| `_meta` | Namespaced extensions. |

Each `affectedPackage` may contain `name`, `sourceId`, `agentId`, `type`, `range`, `versionScheme`, `introduced`, `fixed`, and `lastAffected`. It does not use the old `typeRef` or `kind` fields.

## TypeScript example

```typescript
const advisory = {
  schemaVersion: 2, // Advisory contract version.
  id: "GHSA-example-1234", // Stable ID.
  aliases: ["CVE-2026-1234", "OSV-2026-example"], // Aliases.
  source: "Example Security Database", // Source name.
  sourceUrl: "https://security.example.com/advisories/GHSA-example-1234", // Source page.
  sourceRevision: "20261001", // External source revision.
  asOf: "2026-10-01T00:00:00Z", // Snapshot time.
  status: "published", // Status.
  publishedAt: "2026-09-29T00:00:00Z", // Publication time.
  withdrawnAt: "2026-12-01T00:00:00Z", // Example withdrawal time.
  ttl: 86400, // Refresh hint.
  disclaimer: "Third-party metadata. This is not a detection result and carries no guarantee of accuracy, completeness, timeliness, or safety.", // Required boundary.
  affectedPackages: [{ name: "example-skin", sourceId: "agent-forge:dsh:plugin", agentId: "dsh", type: "plugin", range: "<1.4.0", versionScheme: "semver", introduced: "1.0.0", fixed: "1.4.0", lastAffected: "1.3.9" }], // Affected package.
  advisories: [{ id: "GHSA-example-1234", aliases: ["CVE-2026-1234"], severity: "high", cvss: { version: "3.1", score: 8.1, vector: "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N" }, cwe: ["CWE-79"], summary: "Example advisory summary.", impact: "Example impact description.", affected: { range: "<1.4.0", versionScheme: "semver", introduced: "1.0.0", fixed: "1.4.0", lastAffected: "1.3.9" }, fixedIn: "1.4.0", references: ["https://security.example.com/advisories/GHSA-example-1234"], publishedAt: "2026-09-29T00:00:00Z", updatedAt: "2026-10-01T00:00:00Z", status: "confirmed", source: "Example Security Database", sourceUrl: "https://security.example.com/advisories/GHSA-example-1234" }], // Detailed notice.
  signature: { url: "https://security.example.com/advisories/GHSA-example-1234.sig", type: "pgp", keyId: "advisory-key", keyUrl: "https://security.example.com/advisory-key" }, // Signature declaration.
  _meta: { "org.example/advisory": { importedBy: "community" } }, // Extension data.
} as const;
```
