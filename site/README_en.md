# GitHub Pages Static Frontend

`site/` is the zero-dependency Agent Forge GitHub Pages application. It only reads generated static JSON; it stores no GitHub token and exposes no write API.

## Data contract

The Pages build places these files under `data/`:

- `data/manifest.json`: current revision, generation time, Agent list, and optional index URLs.
- `data/<agent>/<type>/index.json`: a lightweight index for one Agent/type. `packages` may be an object or an array.
- `data/<agent>/<type>/packages/**/*.json`: package details; an index `path` is relative to its index directory.
- `data/dashboard.json`: global Dashboard statistics.
- `data/agents/<agent>/dashboard.json`: per-Agent Dashboard statistics.

When an index is unavailable, the UI stays in a readable empty state rather than silently mixing revisions. Links and `distributions` are displayed only; the page never downloads or executes a plugin.

## Local preview

Use a static HTTP server (opening HTML directly triggers browser same-origin restrictions for `fetch`). For example:

```sh
npx serve site
```

The production Pages deployment is performed by repository Actions; the frontend has no runtime CDN or backend dependency.
