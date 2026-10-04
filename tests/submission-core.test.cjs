const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Core = require("../site/assets/submission-core.js");
const schema = require("../package.schema.json");
const sample = require("../examples/package-mcp.json");

test("bundled package schema and all five examples are supported", () => {
  Core.assertSupported(schema);
  for (const file of fs.readdirSync(path.join(__dirname, "../examples"))) if (/^package-.*\.json$/.test(file)) {
    const record = JSON.parse(fs.readFileSync(path.join(__dirname, "../examples", file), "utf8"));
    assert.deepEqual(Core.validate(record, schema), [], file);
  }
});
test("optional notes are omitted and identity/Unicode survive URL encoding", () => {
  const record = { ...sample, id: "plugin.author/中文", name: "作者/中文 工具", version: "1+中文" };
  const { href, tooLong } = Core.issueURL(record);
  assert.equal(tooLong, false);
  const url = new URL(href);
  assert.equal(url.searchParams.get("template"), "package-submission.yml");
  assert.equal(url.searchParams.get("labels"), "package-submission");
  assert.deepEqual(JSON.parse(url.searchParams.get("package_json")), record);
  assert.equal(url.searchParams.has("rationale"), false);
  assert.equal(new URL(Core.issueURL(record, " 中文\nnotes ").href).searchParams.get("rationale"), "中文\nnotes");
  assert.equal(Core.issueBody(record).includes("### Submission notes"), false);
});
test("long URL handoff is flagged without truncating the record", () => {
  const record = { ...sample, _meta: { "org.example/data": { large: "中文".repeat(5000) } } };
  const result = Core.issueURL(record);
  assert.equal(result.tooLong, true);
  assert.deepEqual(JSON.parse(new URL(result.href).searchParams.get("package_json")), record);
});
test("conditional type, compatibility, duplicates and unknown keys are checked", () => {
  for (const [modify, keyword] of [
    [(r) => { delete r.mcpDetails; }, "required"],
    [(r) => { r.generalDetails = { toolType: "x", agentUse: "x" }; }, "not"],
    [(r) => { r.targets[0] = { agentId: "dsh", compatibilityStatus: "unknown", agentVersionRange: "*" }; }, "type"],
    [(r) => { r.targets[0] = { agentId: "dsh", compatibilityStatus: "known" }; }, "required"],
    [(r) => { r.keywords = ["same", "same"]; }, "uniqueItems"],
    [(r) => { r.distributions[0].checksum = {}; }, "minProperties"],
    [(r) => { r.distributions[0].priority = -1; }, "minimum"],
    [(r) => { r.invented = 1; }, "additionalProperties"],
    [(r) => { r.links.homepage = "relative-path"; }, "format"],
    [(r) => { r.publishedAt = "2026-02-30T00:00:00Z"; }, "format"],
  ]) { const record = Core.clone(sample); modify(record); assert.ok(Core.validate(record, schema).some((e) => e.keyword === keyword), keyword); }
});
test("extensions, array license and empty optional facets remain unchanged", () => {
  const record = Core.clone(sample);
  record.license = ["MIT", "Apache-2.0"];
  record.facets = {};
  record.customFacets = { other: ["用户标签"] };
  record._meta = { "org.example/data": JSON.parse('{"__proto__":{"hidden":true},"number":3,"nil":null,"array":[true,4]}') };
  const before = JSON.stringify(record);
  assert.deepEqual(Core.validate(record, schema), []);
  assert.equal(JSON.stringify(record), before);
  assert.equal({}.hidden, undefined);
});
test("object duplicates compare structurally and unsupported schemas fail closed", () => {
  assert.equal(Core.equal({ a: 1, b: 2 }, { b: 2, a: 1 }), true);
  assert.throws(() => Core.assertSupported({ type: "string", unknownKeyword: true }), /Unsupported/);
  assert.throws(() => Core.assertSupported({ $ref: "https://evil.invalid/schema.json" }), /External/);
});
test("initial form structures cover each type without invented upstream data", () => {
  for (const type of ["mcp", "plugin", "skill", "general", "bundle"]) {
    const value = Core.initialRecord(type);
    assert.equal(value.type, type);
    assert.ok(value[type + "Details"]);
    assert.equal(own(value, "distributions"), type !== "bundle");
    assert.equal(value.publishedAt, undefined);
    assert.ok(Number.isFinite(Date.parse(value.createdAt)));
    assert.equal(value.targets[0].agentVersionRange, null);
    assert.equal(value.targets[0].compatibilityStatus, "unknown");
  }
});
function own(value, key) { return Object.prototype.hasOwnProperty.call(value, key); }

test("JSON imports reject duplicate keys rather than silently losing fields", () => {
  assert.throws(() => Core.parseRecord('{"name":"first","name":"second"}'), /Duplicate JSON key/);
  assert.throws(() => Core.parseRecord('{"name":"first","na\\u006de":"second"}'), /Duplicate JSON key/);
  assert.throws(() => Core.parseRecord('{"extension":{"key":1,"key":2}}'), /Duplicate JSON key/);
  assert.deepEqual(Core.parseRecord('\uFEFF{"a":{"key":1},"b":{"key":2},"string":"### Canonical package JSON\\n```"}'), { a: { key: 1 }, b: { key: 2 }, string: "### Canonical package JSON\n```" });
});

test("media submission preserves icon, ordered previews and provenance; rejects unsafe image shapes", () => {
  const record = Core.clone(sample);
  record.media = { icon: { url: "https://images.example/icon.png", alt: "Icon" }, previews: [{ url: "https://images.example/light.png", alt: "Light preview", theme: "light" }, { url: "https://images.example/dark.png", alt: "Dark preview", theme: "dark" }] };
  record._meta = { "org.agentforge/media-provenance": { sources: [{ url: record.media.icon.url, source: { repository: "author/skin", revision: "a".repeat(40) } }] } };
  assert.deepEqual(Core.validate(record, schema), []);
  assert.deepEqual(Core.parseRecord(JSON.stringify(record)), record);
  assert.deepEqual(JSON.parse(new URL(Core.issueURL(record).href).searchParams.get("package_json")), record);
  for (const media of [{}, { previews: [] }, { icon: { url: "https://images.example/icon.png" } }, { previews: Array.from({ length: 13 }, (_, i) => ({ url: "https://images.example/" + i + ".png", alt: "Preview" })) }]) {
    assert.ok(Core.validate({ ...record, media }, schema).length);
  }
  for (const url of ["http://images.example/x", "javascript:alert(1)", "data:image/png;base64,x", "file:///x", "//images.example/x", "https://user:password@images.example/x", "https://images.example/\\evil"]) {
    assert.ok(Core.validate({ ...record, media: { icon: { url, alt: "Icon" } } }, schema).length, url);
  }
});
