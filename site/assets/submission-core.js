/* Supports the keywords used by the bundled package schema, not arbitrary Draft 2020-12 schemas. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ForgeSubmission = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    if (object(a) && object(b)) { const keys = Object.keys(a); return keys.length === Object.keys(b).length && keys.every((k) => own(b, k) && equal(a[k], b[k])); }
    return false;
  };
  const pointer = (path, key) => `${path}/${String(key).replace(/~/g, "~0").replace(/\//g, "~1")}`;
  const schemaKeywords = new Set(["$schema", "$id", "$ref", "$defs", "title", "description", "default", "examples", "type", "const", "enum", "required", "properties", "additionalProperties", "propertyNames", "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "format", "minimum", "maximum", "minProperties", "maxProperties", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"]);
  function assertSupported(schema) {
    function check(node) {
      if (typeof node === "boolean") return;
      if (!object(node)) throw new Error("Invalid schema node");
      for (const key of Object.keys(node)) if (!schemaKeywords.has(key)) throw new Error(`Unsupported schema keyword: ${key}`);
      if (node.$ref && !node.$ref.startsWith("#/")) throw new Error("External schema references are not supported");
      if (node.format && !["uri", "date-time", "email"].includes(node.format)) throw new Error(`Unsupported schema format: ${node.format}`);
      for (const key of ["$defs", "properties"]) for (const sub of Object.values(node[key] || {})) check(sub);
      for (const key of ["items", "additionalProperties", "propertyNames", "not", "if", "then", "else"]) if (own(node, key)) check(node[key]);
      for (const key of ["allOf", "anyOf", "oneOf"]) for (const sub of node[key] || []) check(sub);
    }
    check(schema);
  }
  function resolve(schema, rootSchema) {
    if (!schema || !schema.$ref) return schema;
    let target = rootSchema;
    for (const part of schema.$ref.slice(2).split("/")) { const key = part.replace(/~1/g, "/").replace(/~0/g, "~"); if (!object(target) || !own(target, key)) throw new Error(`Missing schema reference: ${schema.$ref}`); target = target[key]; }
    return { ...resolve(target, rootSchema), ...Object.fromEntries(Object.entries(schema).filter(([k]) => k !== "$ref")) };
  }
  function formatValid(value, format) {
    if (format === "uri") return /^[a-z][a-z0-9+.-]*:[^\s]*$/i.test(value) && !/[<>"{}|\\^`]/.test(value) && !/%(?![0-9a-f]{2})/i.test(value);
    if (format === "email") return /^[^@\s]+@[^@\s]+$/.test(value);
    if (format === "date-time") {
      const m = value.match(/^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|[+-](\d{2}):(\d{2}))$/);
      if (!m) return false;
      const [, y, mo, d, h, mi, s, oh, om] = m;
      const days = new Date(Date.UTC(Number(y), Number(mo), 0)).getUTCDate();
      return Number(y) >= 1 && Number(mo) >= 1 && Number(mo) <= 12 && Number(d) >= 1 && Number(d) <= days && Number(h) <= 23 && Number(mi) <= 59 && Number(s) <= 59 && (!oh || (Number(oh) <= 23 && Number(om) <= 59));
    }
    return true;
  }
  function validate(value, rootSchema) {
    assertSupported(rootSchema);
    function walk(data, raw, path) {
      if (raw === true) return [];
      if (raw === false) return [{ path, keyword: "false" }];
      const schema = resolve(raw, rootSchema), errors = [];
      const add = (keyword, extra = {}) => errors.push({ path, keyword, ...extra });
      if (own(schema, "const") && !equal(data, schema.const)) add("const", { expected: schema.const });
      if (schema.enum && !schema.enum.some((v) => equal(v, data))) add("enum", { expected: schema.enum.join(", ") });
      if (schema.type) {
        const choices = Array.isArray(schema.type) ? schema.type : [schema.type];
        const matches = choices.some((t) => t === "null" ? data === null : t === "array" ? Array.isArray(data) : t === "object" ? object(data) : t === "integer" ? Number.isInteger(data) : typeof data === t);
        if (!matches) { add("type", { expected: choices.join(" / ") }); return errors; }
      }
      if (typeof data === "string") {
        const len = [...data].length;
        if (schema.minLength !== undefined && len < schema.minLength) add("minLength", { expected: schema.minLength });
        if (schema.maxLength !== undefined && len > schema.maxLength) add("maxLength", { expected: schema.maxLength });
        if (schema.pattern && !new RegExp(schema.pattern, "u").test(data)) add("pattern");
        if (schema.format && !formatValid(data, schema.format)) add("format", { expected: schema.format });
      }
      if (typeof data === "number") {
        if (schema.minimum !== undefined && data < schema.minimum) add("minimum", { expected: schema.minimum });
        if (schema.maximum !== undefined && data > schema.maximum) add("maximum", { expected: schema.maximum });
      }
      if (Array.isArray(data)) {
        if (schema.minItems !== undefined && data.length < schema.minItems) add("minItems", { expected: schema.minItems });
        if (schema.maxItems !== undefined && data.length > schema.maxItems) add("maxItems", { expected: schema.maxItems });
        if (schema.uniqueItems && data.some((v, i) => data.slice(0, i).some((other) => equal(v, other)))) add("uniqueItems");
        if (schema.items) data.forEach((v, i) => errors.push(...walk(v, schema.items, pointer(path, i))));
      }
      if (object(data)) {
        if (schema.minProperties !== undefined && Object.keys(data).length < schema.minProperties) add("minProperties", { expected: schema.minProperties });
        if (schema.maxProperties !== undefined && Object.keys(data).length > schema.maxProperties) add("maxProperties", { expected: schema.maxProperties });
        for (const key of schema.required || []) if (!own(data, key)) errors.push({ path: pointer(path, key), keyword: "required" });
        for (const key of Object.keys(data)) {
          if (schema.propertyNames) errors.push(...walk(key, schema.propertyNames, pointer(path, key)));
          if (own(schema.properties || {}, key)) errors.push(...walk(data[key], schema.properties[key], pointer(path, key)));
          else if (schema.additionalProperties === false) errors.push({ path: pointer(path, key), keyword: "additionalProperties" });
          else if (schema.additionalProperties && typeof schema.additionalProperties === "object") errors.push(...walk(data[key], schema.additionalProperties, pointer(path, key)));
        }
      }
      for (const sub of schema.allOf || []) errors.push(...walk(data, sub, path));
      for (const kind of ["anyOf", "oneOf"]) if (schema[kind]) {
        const results = schema[kind].map((sub) => walk(data, sub, path)), valid = results.filter((r) => !r.length).length;
        if (kind === "anyOf" ? !valid : valid !== 1) { add(kind); if (!valid) errors.push(...results.reduce((a, b) => a.length <= b.length ? a : b)); }
      }
      if (schema.not && !walk(data, schema.not, path).length) add("not");
      if (schema.if) { const branch = !walk(data, schema.if, path).length ? schema.then : schema.else; if (branch) errors.push(...walk(data, branch, path)); }
      return errors;
    }
    return walk(value, rootSchema, "").filter((item, i, all) => all.findIndex((other) => other.path === item.path && other.keyword === item.keyword) === i);
  }
  function parseRecord(text) {
    text = String(text).replace(/^\uFEFF/, "");
    const value = JSON.parse(text);
    const frames = [];
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (char === '"') {
        const start = i++;
        for (; i < text.length; i++) {
          if (text[i] === "\\") i++;
          else if (text[i] === '"') break;
        }
        let next = i + 1;
        while (/\s/.test(text[next] || "") && next < text.length) next++;
        const frame = frames.at(-1);
        if (frame && frame.object && text[next] === ":") {
          const key = JSON.parse(text.slice(start, i + 1));
          if (frame.keys.has(key)) throw new Error(`Duplicate JSON key: ${key}`);
          frame.keys.add(key);
        }
      } else if (char === "{" || char === "[") frames.push({ object: char === "{", keys: new Set() });
      else if (char === "}" || char === "]") frames.pop();
    }
    return value;
  }
  function initialRecord(type = "mcp", createdAt = new Date().toISOString()) {
    const record = { schemaVersion: 2, createdAt, name: "", id: "", version: "", type, description: "", license: "", targets: [{ agentId: "", compatibilityStatus: "unknown", agentVersionRange: null, compatibilityNote: "" }] };
    if (type !== "bundle") record.distributions = [{ id: "", type: "github-repo", url: "" }];
    const details = { mcp: { registryType: "npm", identifier: "" }, plugin: { manifestPath: "" }, skill: { skillPath: "" }, general: { toolType: "", agentUse: "" }, bundle: { members: [{ memberType: "package", memberId: "" }] } };
    record[`${type}Details`] = details[type];
    return record;
  }
  function issueURL(record, notes = "", limit = 7500) {
    const url = new URL("https://github.com/metaone01/agent-forge/issues/new");
    url.searchParams.set("template", "package-submission.yml");
    url.searchParams.set("title", `[package] ${record.name} ${record.version}`.slice(0, 240));
    url.searchParams.set("labels", "package-submission");
    url.searchParams.set("package_json", JSON.stringify(record, null, 2));
    if (notes.trim()) url.searchParams.set("rationale", notes.trim());
    return { href: url.href, tooLong: url.href.length > limit, length: url.href.length };
  }
  function issueBody(record, notes = "") { return `### Canonical package JSON\n\n${JSON.stringify(record, null, 2)}${notes.trim() ? `\n\n### Submission notes\n\n${notes.trim()}` : ""}\n`; }
  return { validate, resolve, assertSupported, clone, equal, parseRecord, initialRecord, issueURL, issueBody };
});
