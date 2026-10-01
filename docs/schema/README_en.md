# Agent Forge Schema Documentation

This directory is the human-facing entry point for the four public JSON Schemas. The machine-readable contracts remain the `*.schema.json` files at the repository root; these documents explain field meanings, boundaries, and complete TypeScript-style examples.

## Files

| Schema | Purpose | Notes |
| --- | --- | --- |
| [package.schema_en.md](package.schema_en.md) | Package-version record | Agents, types, compatibility, facets, Bundles, and distributions |
| [source.schema_en.md](source.schema_en.md) | Metadata-source manifest | One Agent/type catalog and its metadata mirrors |
| [index.schema_en.md](index.schema_en.md) | Package index | Low-bandwidth discovery and version lookup |
| [advisory.schema_en.md](advisory.schema_en.md) | Advisory snapshot | Integrated third-party notices, not a security detection result |

Chinese documents omit the `_en` suffix, for example `package.schema.md`.

## Two-layer address model

`source.schema.json` and `index.schema.json` describe Agent Forge metadata catalogs. `sourceMirrors`, `mirrorOf`, `revision`, and `priority` let consumers switch between GitHub Pages, regional mirrors, and other read-only endpoints.

`package.schema.json` uses `distributions` for candidates that contain or provide the actual tool, such as a Git repository, Release, Archive, Registry, or OCI location. These are not Agent Forge catalog sources. Installers may choose a candidate, but Agent Forge does not download, proxy, verify, or guarantee it.

## Guidance for contributors

The machine-readable Schema is authoritative; the Schema README is the best field reference for humans; and the commented TypeScript example is the fastest way to understand object shape and entry. Together they reduce incorrect submissions, but examples never replace the validator or cross-file rules.

All fields describe metadata integration only. They do not mean that a source, checksum, signature, facet, compatibility range, or advisory has been security-verified.
