#!/usr/bin/env node
"use strict";
// Shared with Pages: reads metadata only, emits a reviewable result, never applies.
const Source = require("../site/assets/source-metadata.js");
const schema = require("../package.schema.json");
async function main(argv = process.argv.slice(2)) {
  const options = { type: "plugin", schema }, allowed = new Set(["source", "type", "ref", "path", "record-path"]);
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i].replace(/^--/, "");
    if (!argv[i].startsWith("--") || !allowed.has(key) || !argv[i + 1] || argv[i + 1].startsWith("--")) throw Error("Usage: node tools/acquire_source.cjs --source URL [--type plugin] [--ref REF] [--path PATH] [--record-path sources/plugin/packages/...json]");
    options[key] = argv[++i];
  }
  if (!options.source || !["mcp", "plugin", "skill", "general", "bundle"].includes(options.type)) throw Error("Source and a supported package type are required");
  const recordPath = options["record-path"];
  if (recordPath && (!recordPath.startsWith("sources/" + options.type + "/packages/") || !recordPath.endsWith(".json") || recordPath.split("/").includes("..") || /[\\\x00-\x1f]/.test(recordPath))) throw Error("Unsafe canonical record path");
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 25000);
  try {
    const result = await Source.acquire(options.source, { ...options, signal: controller.signal });
    const output = recordPath ? { recordPath, metadata: result.fields, source: { url: options.source, observations: result.observations } } : result;
    console.log(JSON.stringify(output, null, recordPath ? 0 : 2));
    if (recordPath && (result.issues.length || result.candidates.length)) console.error(JSON.stringify({ issues: result.issues, candidates: result.candidates, skipped: result.skipped }));
    return result;
  } finally { clearTimeout(timeout); }
}
module.exports = { main };
if (require.main === module) main().catch(error => { console.error("acquire_source: " + error.message); process.exitCode = 1; });
