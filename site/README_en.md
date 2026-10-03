# GitHub Pages Static Frontend

`site/` is a static application with no external runtime dependencies, GitHub token storage, or write API. Documentation is generated using Python's standard library; the browser reads static JSON.

## Pages and preferences

Catalog, package details, both dashboards, documentation and submission share preferences. The default is Chinese (`zh-CN`) and system theme; users can choose English, Light or Dark. Explicit choices persist when storage is available. Language changes preserve filters, pagination and draft data.

`docs/` renders the root project READMEs and Schema references with highlighting, headings and code copying. `submit/` offers a visual Schema-driven form and an independent Agent / JSON entry. Submission Notes are optional. Cards show package name above canonical ID, followed by controlled facets, custom tags and keyword tags; empty collections are omitted.

## Static data contract

- `data/manifest.json`: revision, times and sources. Source paths are relative to `data/`, not the site root.
- `data/<agent>/<type>/index.json`: lightweight entries keyed by name, with canonical `id`. Optional `facets`, `customFacets`, `keywords` and entry `updatedAt` support rendering and sorting without per-card detail requests.
- `data/<agent>/<type>/packages/**/*.json`: detail records; index paths are relative to their index directory.
- `data/dashboard.json` and `data/agents/<agent>/dashboard.json`: statistics.
- Four root `*.schema.json` files: real machine-readable contracts for documentation and submission.
- `submit/contract.json` and `submit/example.json`: machine submission instructions and a complete example.

A missing index produces a readable empty state. Distribution links are displayed, never downloaded or executed.

## Submission boundaries

The visual form generates JSON and opens a prefilled GitHub Issue Form for the user's final confirmation. Agents may provide JSON directly, using the same record and stable Canonical package JSON heading. No backend or browser write credential is introduced.

New visual drafts use their actual metadata `createdAt`, never an invented upstream `publishedAt`. If an Agent record omits all lifecycle timestamps, the main Issue pipeline adds its actual Issue creation time as metadata `createdAt`, preserving declared fields while avoiding permanently excluded undated records.

The no-dependency browser checker supports the keywords used by the bundled Schema, not arbitrary Draft 2020-12 schemas. Unsupported keywords fail closed. Imports preserve extensions and reject duplicate keys. CI remains authoritative.

A conservative 7,500-character URL budget is an application policy, not a guaranteed GitHub limit. Long records use an explicit copy-to-GitHub fallback and are never truncated. Notes are separate from the JSON. Drafts stay in the browser; do not enter secrets.

New submissions use `tools/validate.py <record.json> --submission-formats` for deterministic URI/date-time checks. Existing full-catalog validation behavior is preserved; legacy URI-template debt is not silently rewritten in this work.

## Build and preview

Run from the repository root:

```sh
uv run tools/project.py --output data --base-url http://localhost:8000/data
uv run python tools/build_site.py --output pages-staging --data data
uv run python -m http.server 8000 --bind 127.0.0.1 --directory pages-staging
```

Visit `/`, `/docs/` and `/submit/`. Opening HTML directly or serving only `site/` omits generated documents, root schemas and data.

The additive builder never recursively deletes directories and rejects unsafe source/output overlaps and linked destinations. Data input and staging output must be separate. Supported Markdown is documented in `--help`; Mermaid stays as copyable code, and raw HTML is not executed.

Main-only workflow changes are delivered separately in `docs/pages-control-plane.patch`; see `docs/PAGES-CONTROL-PLANE-HANDOFF.zh-CN.md`. No control workflow is added to the data branch. Live Issue/PR creation and deployment require separate approval and acceptance.
