"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const Source = require("../site/assets/source-metadata.js");
const Core = require("../site/assets/submission-core.js");
const schema = require("../package.schema.json");
const sha = "a".repeat(40), ctx = { repository: "author/skin", revision: sha, path: "docs/README.md" };
function response(value, status = 200) { return { ok: status === 200, status, headers: new Headers(), text: async () => JSON.stringify(value) }; }
function file(path, text) { return { type: "file", path, encoding: "base64", size: Buffer.byteLength(text), content: Buffer.from(text).toString("base64") }; }
function githubFixture({ manifest = {}, readme = "", manifestPath = "plugin.json", missingReadme = false, base = "", rootReadmePath = "README.md", readmeStatus = 200 } = {}) {
  const calls = [];
  const listing = [{ name: manifestPath.split("/").at(-1), type: "file", path: manifestPath }, { name: "README.md", type: "file", path: base ? base + "/README.md" : "README.md" }];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    const route = new URL(url).pathname.replace("/repos/author/skin", "");
    if (!route) return response({ default_branch: "main", description: "Repo description", license: { spdx_id: "MIT" }, topics: ["skin"], homepage: "https://example.test" });
    if (route === "/commits/main" || route === "/commits/feature%2Fui") return response({ sha });
    if (route === "/contents/" + base) return response(listing);
    if (route === "/contents/" + manifestPath) return response(file(manifestPath, JSON.stringify(manifest)));
    if (route === "/readme") return response(file(rootReadmePath, readme), missingReadme ? 404 : readmeStatus);
    throw Error("Unexpected fixture URL " + url);
  };
  return { fetcher, calls };
}

test("source parser supports repos, pinned blobs, explicit refs and CORS JSON", () => {
  assert.equal(Source.parseSource("author/skin").repository, "author/skin");
  assert.equal(Source.parseSource("https://github.com/author/skin.git/").repository, "author/skin");
  assert.equal(Source.parseSource("https://example.test/package.json").kind, "json");
  assert.equal(Source.parseSource("https://github.com/author/skin/blob/" + sha + "/plugin.json").path, "plugin.json");
  const split = Source.parseSource("https://github.com/author/skin/tree/feature/ui/subdir", { ref: "feature/ui" });
  assert.equal(split.ref, "feature/ui"); assert.equal(split.path, "subdir");
});
for (const input of ["http://github.com/author/skin", "https://user:password@github.com/author/skin", "javascript:alert(1)", "https://github.com/author/skin?token=secret", "https://github.com/author/skin#readme", "https://example.test/readme.md", "https://github.com/author/skin/blob/main/plugin.json"]) test("unsafe/ambiguous source rejected: " + input, () => assert.throws(() => Source.parseSource(input)));
test("relative media is pinned to the asset repository and can traverse within its root", () => {
  assert.equal(Source.resolveImage("../assets/preview.png", ctx), "https://raw.githubusercontent.com/author/skin/" + sha + "/assets/preview.png");
  assert.equal(Source.resolveImage("https://example.test/HEAD/image.png", ctx), "https://example.test/HEAD/image.png");
  for (const value of ["../../escape.png", "//evil.test/a.png", "data:image/png,x", "http://example.test/a.png", "bad%ZZ.png", "../a\\b.png", "a.png?token=x"]) assert.equal(Source.resolveImage(value, ctx), "", value);
  assert.equal(Source.resolveImage("a.png", { ...ctx, revision: "HEAD" }), "");
});
test("icon is independent, explicit canonical media has precedence, preview never fills icon", () => {
  const first = Source.normalizeMedia({ preview: "https://example.test/preview.png" });
  assert.equal(first.media.icon, undefined);
  const result = Source.normalizeMedia({ name: "Skin", media: { icon: { url: "https://example.test/a.png", alt: "A" } }, icon: "https://example.test/b.png", previews: ["https://example.test/p.png", "https://example.test/p.png"] });
  assert.equal(result.media.icon.alt, "A"); assert.equal(result.media.previews.length, 1); assert.deepEqual(result.issues, ["icon-conflict"]);
});
test("unsafe media, invalid theme, blank alt and excess previews are reported", () => {
  const result = Source.normalizeMedia({ icon: { url: "https://example.test/a.png", alt: " " }, previews: Array.from({ length: 15 }, (_, i) => "https://example.test/" + i + ".png") });
  assert.equal(result.media.icon, undefined); assert.equal(result.media.previews.length, 12); assert.equal(result.issues.length, 4);
  assert.equal(Source.normalizeMedia({ icon: { url: "https://example.test/a.png", alt: "A", theme: "dark" } }).media.icon, undefined);
});
test("README recognizes inline, reference and HTML images, preserves order and never renders HTML", () => {
  const readme = '# Preview\n![Dark](../assets/dark.png "title")\n![Light][light]\n<img src="../assets/third.png" alt="A &amp; B" onerror="alert(1)">\n\n[light]: ../assets/light.png\n';
  const result = Source.readmeImages(readme, ctx);
  assert.deepEqual(result.images.map(image => image.alt), ["Dark", "Light", "A & B"]);
  assert.equal(result.images.every(image => image.confirmed), true);
  assert.equal(result.images[0].url.includes(sha), true);
});
test("README skips badges, fenced/inline code, comments and raw script contents", () => {
  const readme = '# Preview\n![badge](https://img.shields.io/badge/status-ok)\n\n~~~md\n![ignore](fake.png)\n~~~\n\n    ![indent](fake.png)\n\n<!-- ![comment](fake.png) -->\n<script>![script](fake.png)</script>\n\n' + String.fromCharCode(96) + '![inline](fake.png)' + String.fromCharCode(96) + '\n\n![Real](real.png)';
  const result = Source.readmeImages(readme, ctx);
  assert.equal(result.skipped, 1); assert.deepEqual(result.images.map(image => image.alt), ["Real"]);
});
test("README generic diagrams remain candidates, only explicitly labelled logos become icon candidates", () => {
  const result = Source.readmeImages('# Project\n![Architecture](diagram.png)\n![Logo](logo.png)\n\n## Screenshots\n![App](screen.png)', ctx);
  assert.equal(result.images[0].confirmed, false); assert.equal(result.images[0].icon, false);
  assert.equal(result.images[1].icon, true); assert.equal(result.images[2].confirmed, true);
});
test("README duplicate URLs deduplicate; unresolved URLs and excessive input fail safely", () => {
  const result = Source.readmeImages('![a](image.png)\n![b](image.png)\n![x](javascript:x)\n<img src="data:image/png,x">', ctx);
  assert.equal(result.images.length, 1); assert.equal(result.issues.length, 2);
  assert.throws(() => Source.readmeImages("x".repeat(Source.MAX_BYTES + 1), ctx), /too-large/);
});
test("merge respects existing fields, explicitly cleared edits and concurrent changes", () => {
  const baseline = Core.initialRecord("plugin", "2026-10-04T00:00:00Z");
  const current = structuredClone(baseline); current.description = "My description"; current.version = "";
  const result = Source.mergeMissing(current, { description: "Fetched", version: "1.0", name: "Fetched name", media: { previews: [{ url: "https://example.test/p.png", alt: "P" }] } }, { baseline, dirty: ["/version"] });
  assert.equal(result.record.description, "My description"); assert.equal(result.record.version, ""); assert.equal(result.record.name, "Fetched name"); assert.equal(result.record.media.icon, undefined);
  assert.ok(result.conflicts.includes("/version")); assert.deepEqual(current, { ...baseline, description: "My description" });
});
test("initial array placeholders can be populated but dirty descendants protect arrays", () => {
  const baseline = Core.initialRecord("plugin"), incoming = { distributions: [{ id: "upstream-git", type: "github-repo", url: "https://github.com/author/skin", ref: sha }] };
  assert.equal(Source.mergeMissing(baseline, incoming, { baseline }).record.distributions[0].ref, sha);
  assert.equal(Source.mergeMissing(baseline, incoming, { baseline, dirty: ["/distributions/0/id"] }).record.distributions[0].id, "");
});
test("merge blocks prototype keys and protects removed fields", () => {
  const incoming = JSON.parse('{"__proto__":{"polluted":true},"media":{"previews":[]}}');
  const result = Source.mergeMissing({}, incoming, { dirty: ["/media"] });
  assert.equal({}.polluted, undefined); assert.equal(result.record.media, undefined);
});
test("canonical JSON preserves declared fields and rejects a different publication type", () => {
  const canonical = require("../site/submit/example.json");
  const result = Source.metadataFields(canonical, schema, null, canonical.type);
  for (const key of ["id", "targets", "distributions", "pluginDetails", "createdAt"]) assert.deepEqual(result.fields[key], canonical[key]);
  assert.throws(() => Source.metadataFields(canonical, schema, null, "bundle"), /type-mismatch/);
});
test("GitHub acquisition pins requests, collects explicit icon and previews and leaves ambiguous images", async () => {
  const fixture = githubFixture({ manifest: { name: "Skin", version: "1.2.3", icon: "assets/icon.png", preview: "assets/first.png", description: "Plugin" }, readme: '# Preview\n![App](assets/second.png)\n\n# Development\n![Architecture](assets/diagram.png)\n![Status](https://img.shields.io/badge/ok-green)' });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.equal(result.fields.media.icon.url, "https://raw.githubusercontent.com/author/skin/" + sha + "/assets/icon.png");
  assert.equal(result.fields.media.previews.length, 2); assert.equal(result.candidates.length, 1); assert.equal(result.skipped, 1);
  assert.equal(result.fields.id, undefined); assert.equal(result.fields.targets, undefined);
  assert.equal(result.fields.pluginDetails.manifestPath, "plugin.json");
  assert.equal(Core.validate(result.fields.distributions, { ...schema.$defs.distribution, $defs: schema.$defs }).length > 0, true); // Array is not an individual distribution.
  assert.deepEqual(Core.validate(result.fields.distributions[0], { ...schema.$defs.distribution, $defs: schema.$defs }), []);
  assert.equal(result.fields.distributions[0].ref, sha);
  for (const call of fixture.calls) { assert.equal(call.options.credentials, "omit"); assert.equal(call.options.referrerPolicy, "no-referrer"); assert.equal(call.options.redirect, "error"); if (call.url.includes("/contents/")) assert.ok(call.url.endsWith("?ref=" + sha)); }
  assert.equal(fixture.calls.some(call => /raw\.githubusercontent|assets\/.*\.png/.test(call.url)), false);
});
test("README screenshots never become icon, explicit README logo can independently supply one", async () => {
  let fixture = githubFixture({ readme: '# Screenshots\n![App](screen.png)' });
  let result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.equal(result.fields.media.icon, undefined); assert.equal(result.fields.media.previews.length, 1);
  fixture = githubFixture({ readme: '# Project\n![Logo](logo.png)\n\n## Preview\n![App](screen.png)' });
  result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.ok(result.fields.media.icon.url.endsWith('/logo.png')); assert.ok(result.fields.media.previews[0].url.endsWith('/screen.png'));
});
test("missing README and invalid manifest produce usable partial acquisition without invented version", async () => {
  const fixture = githubFixture({ missingReadme: true, manifest: {} });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.equal(result.fields.name, "author/skin"); assert.equal(result.fields.version, undefined); assert.equal(result.fields.media, undefined);
});
test("HTTPS JSON acquisition preserves declared media without guessing relative assets", async () => {
  const result = await Source.acquire("https://example.test/package.json", { schema, type: "plugin", fetcher: async () => response({ name: "N", icon: "relative.png", preview: "https://example.test/preview.png" }) });
  assert.equal(result.fields.name, "N"); assert.equal(result.fields.media.icon, undefined); assert.equal(result.fields.media.previews.length, 1); assert.deepEqual(result.issues, ["invalid-media"]);
});
for (const status of [403, 429, 404, 500]) test("HTTP error " + status + " does not masquerade as successful empty metadata", async () => {
  await assert.rejects(Source.acquire("https://example.test/package.json", { schema, type: "plugin", fetcher: async () => response({}, status) }), new RegExp(status === 404 ? "not-found" : [403, 429].includes(status) ? "rate-limit" : "http-500"));
});
test("network/CORS and cancellation errors remain visible", async () => {
  await assert.rejects(Source.acquire("https://example.test/package.json", { schema, type: "plugin", fetcher: async () => { throw Error("CORS"); } }), /network/);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(Source.acquire("https://example.test/package.json", { schema, type: "plugin", signal: controller.signal, fetcher: async () => { throw Error("aborted"); } }), /cancelled/);
});
test("bounded streaming JSON refuses oversized body and cancels its reader", async () => {
  let cancelled = false;
  const reader = { read: async () => ({ value: new Uint8Array(Source.MAX_BYTES + 1), done: false }), cancel: async () => { cancelled = true; }, releaseLock() {} };
  await assert.rejects(Source.requestJSON("https://example.test/a.json", async () => ({ ok: true, headers: new Headers(), body: { getReader: () => reader } })), /too-large/);
  assert.equal(cancelled, true);
});

test("duplicate JSON keys fail closed instead of losing declarations", async () => {
  await assert.rejects(Source.acquire("https://example.test/package.json", { schema, type: "plugin", fetcher: async () => ({ ok: true, headers: new Headers(), text: async () => '{"icon":"https://example.test/a.png","icon":"https://example.test/b.png"}' }) }), /json/);
});

test("ordinary manifest maps only valid explicit maintainers, labels and plugin declarations", async () => {
  const fixture = githubFixture({ manifest: { name: "P", author: { name: "Author", url: "https://example.test" }, pluginDetails: { manifestPath: "plugin.json" }, entrypoint: "src/main.js", permissions: ["filesystem.read"], subtype: "skin", platform: { os: ["windows"] }, script: "rm -rf /" } });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.equal(result.fields.maintainers[0].name, "Author"); assert.equal(result.fields.pluginDetails.entrypoint, "src/main.js");
  assert.deepEqual(result.fields.pluginDetails.permissions, ["filesystem.read"]); assert.equal(result.fields.subtype, "skin");
  assert.deepEqual(result.fields.platform.os, ["windows"]); assert.equal(result.fields.script, undefined);
});

for (const path of ["", "skin-gallery", "skin-gallery/plugin.json", "skin-gallery/.claude-plugin/plugin.json"]) test("only root README is read for package path: " + (path || "root"), async () => {
  const base = path.endsWith(".json") ? path.split("/").slice(0, -1).join("/") : path;
  const manifestPath = path.endsWith(".json") ? path : (base ? base + "/" : "") + "plugin.json";
  const fixture = githubFixture({ base, manifestPath, manifest: { name: "Child skin", version: "0.1.0", icon: "icon.png" }, readme: "# Preview\n![Root screenshot](assets/root.png)" });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", path, fetcher: fixture.fetcher });
  assert.equal(result.fields.name, "Child skin"); assert.equal(result.fields.version, "0.1.0");
  assert.equal(result.fields.media.previews[0].url, "https://raw.githubusercontent.com/author/skin/" + sha + "/assets/root.png");
  assert.equal(result.fields.media.icon.url, "https://raw.githubusercontent.com/author/skin/" + sha + "/" + (base ? base + "/" : "") + "icon.png");
  assert.ok(result.fields.links.readme.endsWith("/" + sha + "/README.md"));
  assert.equal(fixture.calls.filter(call => call.url.endsWith("/readme?ref=" + sha)).length, 1);
  assert.equal(fixture.calls.some(call => /\/contents\/.*readme/i.test(call.url)), false);
});
test("missing root README never falls back to a listed child README", async () => {
  const fixture = githubFixture({ base: "skin-gallery", manifestPath: "skin-gallery/plugin.json", manifest: { name: "Child skin", version: "0.1.0" }, missingReadme: true });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", path: "skin-gallery", fetcher: fixture.fetcher });
  assert.equal(result.fields.version, "0.1.0"); assert.equal(result.fields.media, undefined);
  assert.deepEqual(result.candidates, []); assert.ok(result.issues.includes("readme-missing"));
  assert.equal(fixture.calls.some(call => /\/contents\/.*readme/i.test(call.url)), false);
});
test("extensionless root README is supported and nested API README is rejected", async () => {
  for (const rootReadmePath of ["README", "docs/README.md"]) {
    const fixture = githubFixture({ rootReadmePath, readme: "# Preview\n![App](screen.png)" });
    const result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
    if (rootReadmePath === "README") {
      assert.ok(result.fields.media.previews[0].url.endsWith("/" + sha + "/screen.png"));
      assert.ok(result.observations.some(item => item.path === "README"));
    } else { assert.equal(result.fields.media, undefined); assert.ok(result.issues.includes("readme-unavailable:root-only")); }
  }
});
test("root README failure preserves manifest metadata", async () => {
  const fixture = githubFixture({ manifest: { name: "Skin", version: "1.0" }, readmeStatus: 403 });
  const result = await Source.acquire("author/skin", { schema, type: "plugin", fetcher: fixture.fetcher });
  assert.equal(result.fields.version, "1.0"); assert.ok(result.issues.includes("readme-unavailable:rate-limit"));
});
test("README paths are rejected before making requests", async () => {
  for (const path of ["README", "README.md", "skin-gallery/README.md", "docs/guide.md"]) {
    let calls = 0;
    await assert.rejects(Source.acquire("author/skin", { schema, type: "plugin", path, fetcher: async () => { calls++; throw Error("unexpected"); } }), /source-path/);
    assert.equal(calls, 0);
  }
  assert.throws(() => Source.parseSource("https://github.com/author/skin/blob/" + sha + "/docs/README.md"), /source-path/);
});

test("SKILL path remains a locator, never a README image source", async () => {
  const fixture = githubFixture({ readme: "# Preview\n![Root](root.png)" });
  const fetcher = async (url, options) => new URL(url).pathname.endsWith("/contents/skills/demo/SKILL.md")
    ? response(file("skills/demo/SKILL.md", "# Preview\n![Child](child.png)")) : fixture.fetcher(url, options);
  const result = await Source.acquire("author/skin", { schema, type: "skill", path: "skills/demo/SKILL.md", fetcher });
  assert.equal(result.fields.skillDetails.skillPath, "skills/demo/SKILL.md");
  assert.equal(result.fields.media.previews.length, 1);
  assert.ok(result.fields.media.previews[0].url.endsWith("/" + sha + "/root.png"));
  assert.equal(result.observations.some(item => item.path === "skills/demo/SKILL.md"), false);
});

test("acquired fields refresh, stale auto values disappear and manual fields survive", () => {
  const baseline = Core.initialRecord("plugin");
  const first = Source.mergeAcquired(baseline, { name: "Source A", version: "1.0", description: "Auto description", media: { icon: { url: "https://example.test/a-icon.png", alt: "A" }, previews: [{ url: "https://example.test/a.png", alt: "A" }] } }, { baseline });
  const refreshed = Source.mergeAcquired(first.record, { name: "Source A", version: "2.0" }, { baseline, previous: first.managed });
  assert.equal(refreshed.record.version, "2.0"); assert.equal(refreshed.record.media, undefined); assert.equal(refreshed.record.description, baseline.description);
  const manual = structuredClone(first.record); manual.description = "Manual"; manual.media.icon.alt = "My icon";
  const switched = Source.mergeAcquired(manual, { name: "Source B", version: "3.0", media: { previews: [{ url: "https://example.test/b.png", alt: "B" }] } }, { baseline, previous: first.managed, dirty: ["/description", "/media/icon/alt"] });
  assert.equal(switched.record.name, "Source B"); assert.equal(switched.record.version, "3.0"); assert.equal(switched.record.description, "Manual");
  assert.equal(switched.record.media.icon.alt, "My icon"); assert.equal(switched.record.media.icon.url, "https://example.test/a-icon.png");
  assert.ok(switched.record.media.previews[0].url.endsWith("/b.png"));
  assert.equal(switched.managed["/description"], undefined); assert.equal(switched.managed["/media/icon/alt"], undefined);
});
test("pre-existing matching values are never mistaken for acquisition-owned fields", () => {
  const baseline = Core.initialRecord("plugin"), existing = { ...baseline, name: "Source A", media: { icon: { url: "https://example.test/manual.png", alt: "Manual" } } };
  const first = Source.mergeAcquired(existing, { name: "Source A", version: "1.0", media: { icon: { url: "https://example.test/manual.png", alt: "Manual" }, previews: [{ url: "https://example.test/a.png", alt: "A" }] } }, { baseline });
  const next = Source.mergeAcquired(first.record, { name: "Source B", version: "2.0" }, { baseline, previous: first.managed });
  assert.equal(next.record.name, "Source A"); assert.equal(next.record.media.icon.url, "https://example.test/manual.png"); assert.equal(next.record.media.previews, undefined);
});
test("manual array edits, removals, cleared values and concurrent changes stay protected", () => {
  const baseline = Core.initialRecord("plugin");
  const first = Source.mergeAcquired(baseline, { name: "A", version: "1", media: { previews: [{ url: "https://example.test/a.png", alt: "A" }] } }, { baseline });
  const current = structuredClone(first.record); current.version = ""; delete current.name; current.media.previews[0].alt = "Manual";
  const next = Source.mergeAcquired(current, { name: "B", version: "2", media: { previews: [{ url: "https://example.test/b.png", alt: "B" }] } }, { baseline, previous: first.managed, dirty: ["/version", "/name", "/media/previews/0/alt"] });
  assert.equal(next.record.version, ""); assert.equal(next.record.name, undefined); assert.equal(next.record.media.previews[0].alt, "Manual");
  assert.deepEqual(next.managed, {}); assert.deepEqual(first.record.version, "1");
});
test("reconciliation refuses prototype paths and respects externally changed values", () => {
  const baseline = Core.initialRecord("plugin"), current = { ...baseline, version: "Manual" };
  const result = Source.mergeAcquired(current, { version: "New" }, { baseline, previous: { "/version": "Old", "/__proto__/polluted": "x" } });
  assert.equal(result.record.version, "Manual"); assert.equal({}.polluted, undefined);
});
test("ambiguous manifests require an explicit choice before any fields are returned", async () => {
  const fixture = githubFixture({ manifest: { name: "Package A" } });
  const fetcher = async (url, options) => {
    const route = new URL(url).pathname;
    if (route.endsWith("/contents/")) return response([{ type: "file", name: "plugin.json", path: "plugin.json" }, { type: "file", name: "manifest.json", path: "manifest.json" }]);
    if (route.endsWith("/contents/manifest.json")) return response(file("manifest.json", JSON.stringify({ name: "Package B" })));
    return fixture.fetcher(url, options);
  };
  await assert.rejects(Source.acquire("author/skin", { schema, type: "plugin", fetcher }), /multiple-manifests/);
  const chosen = await Source.acquire("author/skin", { schema, type: "plugin", fetcher, path: "manifest.json" });
  assert.equal(chosen.fields.name, "Package B"); assert.equal(chosen.fields.pluginDetails.manifestPath, "manifest.json");
});
