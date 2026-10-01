# Agent Forge Architecture

This document describes the current Agent Forge v2 contract and the GitHub-native
first release. Agent Forge is a metadata index for Agent tools; it is not a
binary registry, security scanner, compatibility authority, or installer.

## 1. Boundaries

The repository records publisher-supplied and community-supplied metadata. It
does not download, execute, inspect, scan, certify, or otherwise verify upstream
content. URLs, checksums, signatures, compatibility ranges, facets, licenses,
and advisories remain claims or references. Consumers must show the public
disclaimer before treating them as useful input.

Canonical records live in Git. Pull Requests are the write path. GitHub Actions
validate records, generate read-only Agent projections, publish GitHub Pages,
and create timestamped Release assets. A future backend may automate writes, but
it must preserve this schema and projection behavior.

## 2. Repository layout

```text
.
├── package.schema.json       # Package/version record contract
├── source.schema.json        # Catalog source manifest contract
├── index.schema.json         # One Agent/type index contract
├── advisory.schema.json      # Advisory snapshot contract
├── sources/<type>/           # Canonical records grouped by type
│   ├── source.json
│   ├── index.json
│   └── packages/**/*.json
├── tools/validate.py         # Schema and cross-file validation
├── tools/project.py          # Agent/type projection generator
├── tools/snapshot.py         # Optional SQLite FTS5 snapshot generator
├── site/                     # Static Pages UI and dashboard
├── tests/                    # Contract, validation, and projection tests
└── .github/workflows/        # PR checks, projection, Pages, and Releases
```

The canonical source tree is authoritative. Generated `data/`, `snapshots/`,
and Pages staging directories are build outputs and are not committed.

## 3. Public types and records

The only first-class package types are:

| `type` | Details object | Meaning |
| --- | --- | --- |
| `mcp` | `mcpDetails` | MCP server or MCP-compatible distribution |
| `plugin` | `pluginDetails` | Agent plugin, including skins via `subtype=skin` |
| `skill` | `skillDetails` | Agent skill document or skill package |
| `general` | `generalDetails` | Agent tool outside the three categories; uses `toolType` and `agentUse` |
| `bundle` | `bundleDetails` | A metadata-only collection of package or nested Bundle members |

`subtype` is one optional administrator-defined value beside `type`. A user
selects it from the registry when creating a package; users cannot define or
override vocabulary. The same subtype is copied to every compatible Agent
projection. There is no `kind`, `memberKind`, `versionKind`, `typeRef`, or
`typeRef.type` in v2.

Every non-Bundle record has at least one `distributions[]` entry. A distribution
is a possible place to obtain the tool (`git`, archive, registry, release, OCI,
or another declared form). Agent Forge does not proxy or verify it. README and
LICENSE are ordinary links at `links.readme` and `links.license`; content is not
copied into the index.

## 4. Agent projections

`targets[]` declares which Agents can use a record and the publisher's claimed
version range. The target contains:

- `agentId`;
- `agentVersionRange`, which may be `null` when no range is available;
- `versionScheme` when a comparison convention is known; and
- `compatibilityStatus` (`known`, `unknown`, or `declared`).

An unknown range is listed but cannot participate in automatic compatibility
filtering or Bundle range calculation unless a Bundle-local override supplies a
range. The generator clones one record into every `data/<agent>/<type>/`
projection and retains only that Agent's target. This makes queries naturally
Agent-scoped without duplicating canonical authoring records.

Publication uses a ten-minute cutoff. A record without a usable timestamp is
excluded from strict public projections unless the build explicitly opts into
undated records. Scheduled publication runs at UTC 00:00, 08:00, and 16:00;
unchanged points do not create a new Release.

## 5. Facets and extensions

`facets` is the administrator vocabulary for capabilities, effects,
dataPractices, permissions, runtime, and integrations. `customFacets` is a
separate user vocabulary and is always displayed as user-defined and
unverified. Neither property is a safety score or a guarantee.

`_meta` is the namespaced extension point. Keys must use a reverse-DNS namespace
such as `org.example/tool`; consumers preserve unknown values without
interpreting them. The validator warns when serialized `_meta` exceeds 4096
UTF-8 bytes.

## 6. Bundles

Bundle members use `memberType: package|bundle` and a stable `memberId`. There is
no required/optional distinction: every listed member is a member. Nested
Bundles are allowed, but cycles are rejected by semantic validation. All package
types share `dependencies`, `conflicts`, `provides`, and `replaces` relations.

The generated Bundle projection uses the intersection of the member Agent sets.
Its core facet set is the union of member capabilities, effects, data practices,
permissions, runtime, and integrations. A Bundle may provide an `override` for
a member's overly narrow or unknown compatibility claim; the override is local
to that Bundle and never edits the member record. Installers decide how to
install a Bundle for a selected Agent; Agent Forge only exposes metadata.

## 7. Sources, mirrors, indexes, and revisions

Each `sources/<type>/` manifest is an independently consumable catalog for one
Agent projection. It contains `sourceId`, `agentId`, `type`, `baseUrl`, `index`,
and an immutable `revision`. `sourceMirrors[]` and `relatedSources[]` describe
alternate metadata endpoints and must not be confused with package
`distributions[]`. A mirror must advertise the same revision as its canonical
source.

Each generated index maps a package name to its available `versions`, a
deterministic `latest`, a relative detail `path`, a record revision, subtype,
and SHA-256 checksum. `manifest.json` and `catalog.json` provide one entry point
for Pages clients. All generated files for a release share the same revision.

## 8. Search and database snapshots

The Pages UI searches lightweight JSON indexes and loads package details on
demand. This works without a server or database and is cacheable through any
static mirror. `tools/snapshot.py` optionally builds one SQLite FTS5 database per
Agent/type. Releases compress these files as immutable assets such as
`dsh-plugin-20261001T080000Z.db.zst`.

SQLite is an offline/high-frequency cache, not the online source of truth. The
per-type split keeps downloads small and lets clients update only the catalog
they use. Consumers must verify the revision and checksum before replacing a
local snapshot.

## 9. Validation and workflows

Run locally with the project Python environment:

```text
uv run --with jsonschema --with referencing python -m unittest discover -s tests -v
uv run tools/validate.py --all
uv run tools/project.py --clean --revision 20261001T080000Z
uv run tools/snapshot.py --data data --output snapshots
```

The validator checks Draft 2020-12 schemas, source/index identity, Agent/type
boundaries, indexed paths and versions, mirror revisions, and `_meta` size. PR
and Merge Queue checks run the same validation before a merge. The release
workflow validates canonical data, builds projections and optional snapshots,
uploads the Pages artifact, and creates a timestamped GitHub Release only when
canonical sources changed.

## 10. Known limits and future work

The current phase has no dynamic API, OAuth callback, PostgreSQL, or long-running
worker. GitHub account identity, review, branch protection, and Merge Queue are
the initial governance layer. A later backend may add authenticated editing,
rate-limit enforcement, audit APIs, and faster server-side search, but must
reuse the same canonical schema, ten-minute publication rule, and projection
semantics. External object-storage mirrors are also later work; GitHub Pages and
Releases are the first deployment targets.
