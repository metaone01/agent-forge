# `package.schema.json` field reference

## Purpose

This Schema describes one version of one package. It stores metadata only: no binary is included, and Agent Forge does not verify sources, compatibility, checksums, signatures, facets, or installation commands. `bundle` is special: it may omit `distributions` because installers select distributions for each member.

## Top-level fields

| Field | Type / values | Purpose |
| --- | --- | --- |
| `schemaVersion` | `2` | Contract version. |
| `id` | string | Stable logical package ID across versions, Agents, and projections. |
| `name` | string | Human-readable name or source-local slug. |
| `displayName` | string | Presentation name. |
| `version` | string | Original upstream version; never rewritten. |
| `versionScheme` | enum | Comparison hint only. |
| `description` | string | Package description. |
| `releaseNotes` | string | Notes for this version. |
| `license` | string or string[] | Upstream license declaration. |
| `links` | object | repository, homepage, readme, license, documentation, changelog, issues URLs. |
| `keywords` | string[] | Free-text search terms. |
| `maintainers` | object[] | Maintainer identity and role. |
| `platform` | object | OS, architecture, libc, and runtime hints. |
| `type` | `mcp`/`plugin`/`skill`/`general`/`bundle` | First-class Agent Forge category. |
| `subtype` | string or null | One administrator-controlled subtype. |
| `mcpDetails` | object | MCP-specific registry and transport details. |
| `pluginDetails` | object | Plugin manifest and entrypoint details. |
| `skillDetails` | object | Skill path and allowed-tool details. |
| `generalDetails` | object | Requires `toolType` and `agentUse`; no `kind` or `typeRef.type`. |
| `bundleDetails` | object | Members, effective targets, and facet union. |
| `targets` | object[] | Agent compatibility and target metadata. |
| `distributions` | object[] | Git, Release, Archive, Registry, OCI, or other artifact candidates. Required for non-Bundles. |
| `dependencies`/`conflicts`/`provides`/`replaces` | object[] | Shared relationship declarations for every type. |
| `facets` | object | Controlled administrator vocabulary. |
| `customFacets` | object | User vocabulary kept separate and unverified. |
| `lifecycle` | object | Active, deprecated, archived, yanked, or experimental state. |
| `createdAt`/`updatedAt`/`publishedAt` | date-time | Lifecycle timestamps. |
| `_meta` | namespaced object | Preserved extension data with no Agent Forge interpretation. |

## Reusable definitions

`identifier`, `packageName`, `url`, `timestamp`, `stringOrArray`, and `stringList` provide shared primitive constraints. `agentTarget` requires `agentId` and `compatibilityStatus`; known targets require a range, while unknown targets require a null range and note. `distribution` requires `id`, `type`, and `url`, with optional Agent filters, refs, regions, checksum, signature, and install hints. `bundleMember` uses `memberType` (`package` or `bundle`) and `memberId`; cycles are rejected by semantic validation.

## Commented TypeScript-style example

The example fills every co-existing top-level field and distribution field. The five type-specific detail objects are intentionally mutually exclusive, so no valid instance can contain all five at once; the remaining four detail branches are covered by the field reference and the branch note below.

```typescript
const record = {
  schemaVersion: 2, // Public contract version.
  id: "dsh.example-skin", // Stable logical ID.
  name: "example-skin", // Source-local package name.
  displayName: "Example Skin", // Display label.
  version: "1.4.0", // Original upstream version.
  versionScheme: "semver", // Comparison hint only.
  description: "A GUI skin plugin for DSH.", // Human-readable description.
  releaseNotes: "Adds compact navigation icons.", // Version notes.
  license: "MIT", // Recorded declaration, not legal verification.
  links: { // URLs are indexed, not copied or reviewed.
    repository: "https://github.com/example/example-skin", // Source repository.
    homepage: "https://example.com/example-skin", // Homepage.
    readme: "https://github.com/example/example-skin#readme", // README.
    license: "https://github.com/example/example-skin/blob/v1.4.0/LICENSE", // LICENSE.
    documentation: "https://example.com/example-skin/docs", // Documentation.
    changelog: "https://github.com/example/example-skin/blob/v1.4.0/CHANGELOG.md", // Changelog.
    issues: "https://github.com/example/example-skin/issues", // Issue tracker.
  },
  keywords: ["gui", "skin", "desktop"], // Search terms.
  maintainers: [{ name: "Example Maintainer", url: "https://github.com/example", email: "maintainer@example.com", role: "maintainer" }], // Maintainer.
  platform: { os: ["linux", "windows"], arch: ["x86_64", "arm64"], libc: ["glibc"], runtime: ["nodejs>=20"] }, // Platform hints.
  type: "plugin", // First-class category.
  subtype: "skin", // Administrator-controlled subtype.
  pluginDetails: { manifestPath: ".dsh/plugin.json", sourceType: "git", marketplaceUrl: "https://example.com/marketplace/example-skin", entrypoint: "dist/index.js", permissions: ["filesystem.workspace-read"] }, // Plugin details.
  targets: [{ agentId: "dsh", agentVersionRange: "^0.2.0", versionScheme: "semver", compatibilityStatus: "known", targetMetadata: { desktop: true }, installMetadata: { mode: "plugin" }, status: "active" }, { agentId: "future-agent", agentVersionRange: null, versionScheme: "unknown", compatibilityStatus: "unknown", compatibilityNote: "Upstream did not publish a parseable range.", targetMetadata: {}, installMetadata: {}, status: "experimental" }], // Known and unknown targets.
  distributions: [{ id: "github-release", type: "release", url: "https://github.com/example/example-skin/releases/download/v1.4.0/example-skin.zip", agentIds: ["dsh"], version: "1.4.0", ref: "v1.4.0", registry: "github", priority: 10, regions: ["global"], checksum: { sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }, signature: { url: "https://example.com/example-skin.sig", type: "sigstore", keyId: "example-key", keyUrl: "https://example.com/keys/example.asc" }, install: { type: "url", url: "https://example.com/example-skin.zip", command: "dsh plugin install example-skin", scriptIntegrity: "sha256-BASE64-VALUE", requires: ["dsh>=0.2.0"], notes: "Review upstream instructions." }, notes: "Primary distribution." }], // Artifact candidates, not catalog mirrors.
  dependencies: [{ id: "dsh.ui-runtime", versionRange: "^0.2.0", versionScheme: "semver", agentId: "dsh", optional: false, reason: "Provides the extension host." }], // Dependencies.
  conflicts: [{ id: "dsh.legacy-theme", versionRange: "<2.0.0", versionScheme: "semver", agentId: "dsh", optional: false, reason: "Same theme slot." }], // Conflicts.
  provides: [{ id: "dsh.gui.skin", versionRange: "1.4.0", versionScheme: "semver", agentId: "dsh", optional: false, reason: "Skin capability." }], // Provided capability.
  replaces: [{ id: "dsh.example-skin-old", versionRange: "*", versionScheme: "custom", agentId: "dsh", optional: false, reason: "Successor package." }], // Replacement.
  facets: { capabilities: ["gui.modify"], effects: ["user-config.modify"], dataPractices: ["public:read"], permissions: ["filesystem.workspace-read"], runtime: ["interactive.required"], integrations: ["agent-ui"] }, // Core facets.
  customFacets: { capabilities: ["theme.preview"], effects: ["desktop.appearance.modify"], dataPractices: ["telemetry:unknown"], permissions: [], runtime: ["gpu.optional"], integrations: ["community-dashboard"], other: ["community-entered"] }, // User facets.
  lifecycle: { status: "active", reason: "Maintained upstream.", replacementId: "dsh.example-skin-next", since: "2026-09-30T16:00:00Z" }, // Lifecycle.
  createdAt: "2026-09-01T00:00:00Z", // Creation time.
  updatedAt: "2026-09-30T15:00:00Z", // Last update.
  publishedAt: "2026-09-30T16:00:00Z", // Public time.
  _meta: { "org.example/review": { importedBy: "community" } }, // Namespaced extension.
} as const;
```

Use `mcpDetails`, `skillDetails`, `generalDetails`, or `bundleDetails` only when the corresponding top-level `type` is selected. `generalDetails.toolType` describes a General tool without introducing another first-class category. Bundle members use `memberType`, expose the intersection of member Agent sets, and keep core `facetUnion` separate from `customFacetUnion`.
