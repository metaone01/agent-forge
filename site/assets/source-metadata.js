(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./submission-core.js"));
  else root.ForgeSource = factory(root.ForgeSubmission);
})(typeof globalThis === "object" ? globalThis : this, function (Core) {
  "use strict";
  const MAX_BYTES = 1024 * 1024, MAX_PREVIEWS = 12;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const escapePointer = key => key.replace(/~/g, "~0").replace(/\//g, "~1");
  const imagePattern = /^https:\/\/[^\s/@?#\\\x00-\x1f\x7f]+(?:[/?#][^\s<>"{}|\\^\x60\x00-\x1f\x7f]*)?$/;
  function httpsURL(value) {
    if (typeof value !== "string" || value.length > 4096 || !imagePattern.test(value) || /%(?![a-f\d]{2})/i.test(value)) return "";
    try { const url = new URL(value); return url.hostname && !url.username && !url.password ? value : ""; } catch (_) { return ""; }
  }
  function safePath(value) {
    if (typeof value !== "string" || value.length > 1000 || value.startsWith("/") || /[\\?#\x00-\x1f\x7f]/.test(value) || value.split("/").some(part => part === ".." || part === ".")) throw Error("path");
    return value;
  }
  function resolveImage(value, context) {
    if (httpsURL(value)) return value;
    if (typeof value !== "string" || !context || !/^[a-f\d]{40}(?:[a-f\d]{24})?$/i.test(context.revision || "") || !/^[\w.-]+\/[\w.-]+$/.test(context.repository || "")) return "";
    let relative;
    try { relative = decodeURIComponent(value); safePath(context.path); } catch (_) { return ""; }
    if (!relative || /^[\/]/.test(relative) || /^[a-z][\w+.-]*:/i.test(relative) || /[\s<>"{}|\\^\x60?#\x00-\x1f\x7f]/.test(relative)) return "";
    const parts = context.path.split("/").slice(0, -1);
    for (const part of relative.split("/")) { if (part === "..") { if (!parts.length) return ""; parts.pop(); } else if (part && part !== ".") parts.push(part); }
    if (!parts.length || context.repository.split("/").some(part => part === "." || part === "..")) return "";
    return httpsURL("https://raw.githubusercontent.com/" + context.repository + "/" + context.revision + "/" + parts.map(encodeURIComponent).join("/"));
  }
  function normalizeMedia(metadata, context) {
    const media = {}, issues = [], label = String(metadata.displayName || metadata.name || "Package").slice(0, 200);
    const canonical = object(metadata.media) ? metadata.media : {};
    function asset(value, preview) {
      const supplied = typeof value === "string" ? { url: value } : value;
      const url = supplied && resolveImage(supplied.url, context);
      const alt = supplied && (supplied.alt === undefined ? label + (preview ? " preview" : " icon") : supplied.alt);
      if (!url || typeof alt !== "string" || !alt.trim() || alt.length > 500 || (supplied.theme !== undefined && (!preview || !["light", "dark", "system"].includes(supplied.theme)))) { issues.push("invalid-media"); return null; }
      return { url, alt, ...(supplied.theme !== undefined ? { theme: supplied.theme } : {}) };
    }
    for (const value of [canonical.icon, metadata.icon, metadata.iconUrl]) if (value !== undefined) {
      const image = asset(value, false); if (!image) continue;
      if (!media.icon) media.icon = image; else if (JSON.stringify(media.icon) !== JSON.stringify(image)) issues.push("icon-conflict");
    }
    const previews = [];
    for (const value of [canonical.previews, metadata.preview, metadata.previews, metadata.screenshots]) if (value !== undefined) {
      for (const item of Array.isArray(value) ? value : [value]) { const image = asset(item, true); if (!image || previews.some(old => old.url === image.url)) continue; if (previews.length < MAX_PREVIEWS) previews.push(image); else issues.push("preview-limit"); }
    }
    if (previews.length) media.previews = previews;
    return { media, issues };
  }
  function decodeEntities(text) {
    return text.replace(/&(?:amp|lt|gt|quot|apos|#\d+|#x[a-f\d]+);/gi, token => {
      const named = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'" };
      if (named[token.toLowerCase()]) return named[token.toLowerCase()];
      const n = token.toLowerCase().startsWith("&#x") ? parseInt(token.slice(3, -1), 16) : parseInt(token.slice(2, -1), 10);
      return n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : "";
    });
  }
  function readmeImages(markdown, context) {
    if (typeof markdown !== "string" || new TextEncoder().encode(markdown).length > MAX_BYTES) throw Error("too-large");
    // Parse references only. Never insert upstream HTML into a DOM or load images.
    let fence = null, heading = "", paragraph = [], blocks = [];
    const flush = () => { if (paragraph.length) blocks.push({ text: paragraph.join("\n"), heading }); paragraph = []; };
    for (const line of markdown.replace(/<!--[\s\S]*?-->|<(script|style|pre|code)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "").split(/\r?\n/)) {
      const marker = line.match(/^\s{0,3}(\x60{3,}|~{3,})/);
      if (marker) { flush(); if (!fence) fence = marker[1]; else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null; continue; }
      if (fence || /^ {4}|^\t/.test(line)) continue;
      const title = line.match(/^\s{0,3}#{1,6}\s+(.+)/);
      if (title) { flush(); heading = title[1]; continue; }
      if (!line.trim()) flush(); else paragraph.push(line);
    }
    flush();
    const references = new Map();
    for (const block of blocks) for (const match of block.text.matchAll(/^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))/gm)) references.set(match[1].trim().toLowerCase(), match[2] || match[3]);
    const images = [], issues = []; let skipped = 0;
    for (const block of blocks) {
      const text = block.text.replace(/\x60+[^\x60\n]*\x60+/g, "");
      const found = [];
      for (const match of text.matchAll(/!\[((?:\\.|[^\]\\])*)\]\(\s*(?:<([^>]+)>|((?:[^\s()]+|\([^()]*\))+))(?:\s+["'][^\n]*?["'])?\s*\)|!\[([^\]]*)\]\[([^\]]*)\]|!\[([^\]]+)\](?![([])/g)) {
        const alt = match[1] ?? match[4] ?? match[6] ?? "";
        const url = match[2] || match[3] || references.get((match[5] || alt).trim().toLowerCase());
        found.push({ at: match.index, url, alt });
      }
      for (const match of text.matchAll(/<img\b([^>]*?)>/gi)) {
        const attrs = {};
        for (const attr of match[1].matchAll(/\b(src|alt)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>\x60]+))/gi)) attrs[attr[1].toLowerCase()] = decodeEntities(attr[2] ?? attr[3] ?? attr[4]);
        found.push({ at: match.index, url: attrs.src, alt: attrs.alt || "" });
      }
      for (const image of found.sort((a, b) => a.at - b.at)) {
        const url = resolveImage(image.url, context), alt = decodeEntities(image.alt.replace(/\\([\[\]\\])/g, "$1")).trim().slice(0, 500);
        if (!url) { issues.push("unresolved-image"); continue; }
        const host = new URL(url).hostname;
        if (/(^|\.)(shields\.io|badgen\.net|img\.shields\.io)$/.test(host) || /(?:^|[/_.-])(badge|badges)(?:[/_.-]|$)/i.test(url) || /^https:\/\/github\.com\/[^/]+\/[^/]+\/actions\/workflows\/.*\/badge\.svg/i.test(url)) { skipped++; continue; }
        if (images.some(old => old.url === url)) continue;
        if (images.length >= 100) { issues.push("candidate-limit"); continue; }
        const confirmed = /(?:preview|screenshot|screen\s*shot|截图|预览)/i.test(block.heading + " " + alt + " " + String(image.url));
        const icon = /^(?:icon|logo|图标|标志)$/i.test(alt) || /^(?:icon|logo|图标|标志)\s*#*$/i.test(block.heading);
        images.push({ url, alt: alt || "README image", heading: block.heading.slice(0, 200), confirmed, icon });
      }
    }
    return { images, skipped, issues };
  }
  function parseSource(input, { ref = "", path = "" } = {}) {
    const value = String(input || "").trim();
    const raw = /^[\w.-]+\/[\w.-]+$/.test(value) ? "https://github.com/" + value : value;
    if (!httpsURL(raw)) throw Error("source-url");
    const url = new URL(raw); if (url.search || url.hash) throw Error("source-query");
    safePath(path);
    if (ref && (ref.length > 200 || /[\s\\?#\x00-\x1f]/.test(ref))) throw Error("ref");
    if (url.hostname !== "github.com") {
      if (ref || path || !/\.json$/i.test(url.pathname)) throw Error("source-json");
      return { kind: "json", url: raw };
    }
    const parts = url.pathname.replace(/\/$/, "").split("/").slice(1).map(decodeURIComponent);
    const repository = parts.slice(0, 2).join("/").replace(/\.git$/, "");
    if (!/^[\w.-]+\/[\w.-]+$/.test(repository) || repository.split("/").some(part => [".", ".."].includes(part))) throw Error("repository");
    if (parts.length > 2) {
      if (!["tree", "blob"].includes(parts[2]) || parts.length < 4) throw Error("repository-path");
      const suffix = parts.slice(3).join("/");
      if (ref && suffix.startsWith(ref + "/")) path = path || suffix.slice(ref.length + 1);
      else if (!ref && /^[a-f\d]{40}(?:[a-f\d]{24})?$/i.test(parts[3])) { ref = parts[3]; path = path || parts.slice(4).join("/"); }
      else if (ref && suffix === ref) path = path || "";
      else throw Error("ambiguous-ref");
      safePath(path);
    }
    if ((/\.md$/i.test(path) && !/(?:^|\/)SKILL\.md$/.test(path)) || /^readme(?:\.[^/]+)?$/i.test(path.split("/").at(-1))) throw Error("source-path");
    return { kind: "github", repository, ref, path, url: "https://github.com/" + repository };
  }
  async function requestJSON(url, fetcher, signal, optional = false) {
    let response;
    try { response = await fetcher(url, { signal, credentials: "omit", referrerPolicy: "no-referrer", redirect: "error", headers: { Accept: "application/vnd.github+json" } }); }
    catch (error) { if (signal?.aborted) throw Error("cancelled"); throw Error("network"); }
    if (!response.ok) { if (optional && response.status === 404) return null; if ([403, 429].includes(response.status)) throw Error("rate-limit"); if (response.status === 404) throw Error("not-found"); throw Error("http-" + response.status); }
    if (Number(response.headers?.get("content-length")) > MAX_BYTES) throw Error("too-large");
    let text;
    if (response.body?.getReader) {
      const reader = response.body.getReader(), decoder = new TextDecoder(); let size = 0; text = "";
      try { for (;;) { const { done, value } = await reader.read(); if (done) break; size += value.byteLength; if (size > MAX_BYTES) { await reader.cancel(); throw Error("too-large"); } text += decoder.decode(value, { stream: true }); } text += decoder.decode(); } finally { reader.releaseLock(); }
    } else { text = await response.text(); if (new TextEncoder().encode(text).length > MAX_BYTES) throw Error("too-large"); }
    try { return Core.parseRecord(text); } catch (_) { throw Error("json"); }
  }
  function decodeFile(file) {
    if (file?.path) safePath(file.path);
    if (!file || file.type !== "file" || file.encoding !== "base64" || typeof file.content !== "string" || file.size > MAX_BYTES) throw Error("file-format");
    const binary = atob(file.content.replace(/\s/g, ""));
    if (binary.length > MAX_BYTES) throw Error("too-large");
    return new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(binary, c => c.charCodeAt(0)));
  }
  function metadataFields(metadata, schema, context, type) {
    if (!object(metadata)) throw Error("metadata-object");
    const fields = {}, canonical = metadata.schemaVersion === 2;
    if (canonical) {
      if (metadata.type !== type) throw Error("type-mismatch");
      // Identity and timestamps are explicit source claims, not inferred compatibility.
      for (const key of Object.keys(schema.properties)) if (own(metadata, key) && key !== "media") Object.defineProperty(fields, key, { value: clone(metadata[key]), writable: true, configurable: true, enumerable: true });
    } else {
      const limits = { name: 200, displayName: 200, version: 200, description: 4000, releaseNotes: 10000, license: 200 };
      for (const [key, max] of Object.entries(limits)) if (typeof metadata[key] === "string" && metadata[key].trim()) fields[key] = metadata[key].slice(0, max);
      if (Array.isArray(metadata.keywords)) fields.keywords = [...new Set(metadata.keywords.filter(v => typeof v === "string" && v.trim()).map(v => v.slice(0, 100)))].slice(0, 100);
      const links = {};
      const repo = typeof metadata.repository === "string" ? metadata.repository : metadata.repository?.url;
      for (const [key, value] of Object.entries({ repository: repo, homepage: metadata.homepage, ...(object(metadata.links) ? metadata.links : {}) })) if (schema.$defs.links.properties[key] && httpsURL(value)) links[key] = value;
      if (Object.keys(links).length) fields.links = links;
      const detailsKey = type + "Details";
      for (const key of ["subtype", "platform", "maintainers", "facets", "customFacets", detailsKey]) if (own(metadata, key) && !Core.validate(metadata[key], { ...schema.properties[key], $defs: schema.$defs }).length) fields[key] = clone(metadata[key]);
      if (!fields.maintainers && object(metadata.author) && typeof metadata.author.name === "string" && metadata.author.name.trim()) {
        const author = { name: metadata.author.name.slice(0, 200) };
        if (httpsURL(metadata.author.url)) author.url = metadata.author.url;
        if (typeof metadata.author.email === "string") author.email = metadata.author.email;
        if (!Core.validate(author, { ...schema.$defs.maintainer, $defs: schema.$defs }).length) fields.maintainers = [author];
      }
    }
    const normalized = normalizeMedia(metadata, context);
    if (Object.keys(normalized.media).length) fields.media = normalized.media;
    return { fields, issues: normalized.issues };
  }
  async function acquire(input, options) {
    const { schema, type, signal, fetcher = fetch } = options;
    const source = parseSource(input, options), observations = [], issues = [], candidates = [];
    let fields = {};
    function observe(metadata, locator) {
      const extracted = metadataFields(metadata, schema, locator, type);
      const merged = mergeMissing(fields, extracted.fields); fields = merged.record;
      issues.push(...merged.conflicts.map(path => "metadata-conflict:" + path));
      issues.push(...extracted.issues); observations.push(locator);
    }
    if (source.kind === "json") {
      observe(await requestJSON(source.url, fetcher, signal), { url: source.url });
      return { fields, observations, issues, candidates, skipped: 0 };
    }
    const endpoint = "https://api.github.com/repos/" + source.repository;
    const repo = await requestJSON(endpoint, fetcher, signal);
    const commit = await requestJSON(endpoint + "/commits/" + encodeURIComponent(source.ref || repo.default_branch), fetcher, signal);
    if (!/^[a-f\d]{40}(?:[a-f\d]{24})?$/i.test(commit?.sha || "")) throw Error("commit");
    const revision = commit.sha, context = path => ({ repository: source.repository, revision, path });
    const contents = async (path, optional = false) => requestJSON(endpoint + "/contents/" + path.split("/").map(encodeURIComponent).join("/") + "?ref=" + revision, fetcher, signal, optional);
    let base = source.path, explicitFile = /\.json$/i.test(base) || /(?:^|\/)SKILL\.md$/.test(base), manifests = [], readmeFile = null;
    if (explicitFile) {
      const file = await contents(base);
      if (/\.json$/i.test(base)) manifests = [file];
      base = base.split("/").slice(0, -1).join("/");
      if (/\/(?:\.claude-plugin|\.dsh)$/.test("/" + base)) base = base.split("/").slice(0, -1).join("/");
    } else {
      const listing = await contents(base);
      if (!Array.isArray(listing)) throw Error("directory");
      const expected = type === "mcp" ? ["server.json", "package.json"] : ["plugin.json", "dsh-plugin.json", "manifest.json", "package.json"];
      const matches = expected.map(name => listing.find(file => file.type === "file" && file.name === name)).filter(Boolean);
      const directories = listing.filter(file => file.type === "dir" && [".claude-plugin", ".dsh"].includes(file.name));
      for (const dir of directories) { const file = await contents(dir.path + "/plugin.json", true); if (file) manifests.push(file); }
      for (const file of matches.slice(0, 4)) manifests.push(await contents(file.path));
      if (manifests.filter(file => !/(?:^|\/)package\.json$/.test(file.path)).length > 1) throw Error("multiple-manifests");
    }
    for (const file of manifests) {
      try {
        const metadata = Core.parseRecord(decodeFile(file)); observe(metadata, context(file.path));
        if (type === "plugin" && /(?:^|\/)(?:plugin|dsh-plugin|manifest)\.json$/.test(file.path)) fields.pluginDetails ||= { manifestPath: file.path, sourceType: "github-repo" };
        if (type === "plugin" && fields.pluginDetails) {
          const declared = {}; for (const key of ["entrypoint", "permissions", "marketplaceUrl"]) if (own(metadata, key) && !Core.validate(metadata[key], { ...schema.$defs.pluginDetails.properties[key], $defs: schema.$defs }).length) declared[key] = clone(metadata[key]);
          fields.pluginDetails = mergeMissing(fields.pluginDetails, declared).record;
        }
      } catch (error) { if (error.message === "type-mismatch") throw error; issues.push("manifest-invalid:" + file.path); }
    }
    // Package paths locate manifests only; README images always come from the repository root.
    try {
      readmeFile = await requestJSON(endpoint + "/readme?ref=" + revision, fetcher, signal, true);
      if (!readmeFile) issues.push("readme-missing");
      if (readmeFile && (typeof readmeFile.path !== "string" || !/^readme(?:\.[^/]+)?$/i.test(readmeFile.path))) { readmeFile = null; issues.push("readme-unavailable:root-only"); }
    } catch (error) { if (signal?.aborted) throw error; issues.push("readme-unavailable:" + error.message); }
    if (readmeFile) {
      try {
        const extracted = readmeImages(decodeFile(readmeFile), context(readmeFile.path));
        const existing = fields.media?.previews || []; let icon = fields.media?.icon;
        for (const image of extracted.images) {
          if (icon?.url === image.url || existing.some(old => old.url === image.url)) continue;
          if (image.icon && !icon) { icon = { url: image.url, alt: image.alt }; fields.media = { ...(fields.media || {}), icon }; }
          else if (image.confirmed && existing.length < MAX_PREVIEWS) existing.push({ url: image.url, alt: image.alt });
          else candidates.push(image);
        }
        if (existing.length) fields.media = { ...(fields.media || {}), previews: existing };
        issues.push(...extracted.issues); observations.push(context(readmeFile.path));
        fields.links = { ...(fields.links || {}), readme: "https://github.com/" + source.repository + "/blob/" + revision + "/" + readmeFile.path.split("/").map(encodeURIComponent).join("/") };
        source.skipped = extracted.skipped;
      } catch (_) { issues.push("readme-invalid"); }
    }
    fields.name ||= source.repository + (base ? "/" + base : "");
    fields.description ||= typeof repo.description === "string" ? repo.description.slice(0, 4000) : "";
    if (!fields.description) delete fields.description;
    if (!fields.license && repo.license?.spdx_id && repo.license.spdx_id !== "NOASSERTION") fields.license = repo.license.spdx_id;
    fields.links = { ...(fields.links || {}), repository: source.url };
    if (!fields.links.homepage && httpsURL(repo.homepage)) fields.links.homepage = repo.homepage;
    if (!fields.keywords && Array.isArray(repo.topics) && repo.topics.length) fields.keywords = repo.topics.filter(v => typeof v === "string").slice(0, 100);
    if (type === "skill" && /(?:^|\/)SKILL\.md$/.test(source.path)) fields.skillDetails ||= { skillPath: source.path };
    if (type !== "bundle" && !fields.distributions) fields.distributions = [{ id: "upstream-git", type: "github-repo", url: source.url + (base ? "/tree/" + revision + "/" + base.split("/").map(encodeURIComponent).join("/") : ""), ref: revision }];
    // Do not invent version, Agent targets, permissions, ID, or compatibility.
    return { fields, observations, issues, candidates, skipped: source.skipped || 0 };
  }
  function mergeMissing(current, incoming, { baseline = {}, dirty = [] } = {}) {
    const protectedPaths = new Set(dirty), filled = [], conflicts = [];
    function merge(now, next, before, pointer) {
      if ([...protectedPaths].some(p => p === pointer || pointer.startsWith(p + "/"))) { conflicts.push(pointer); return clone(now); }
      if (object(next) && object(now)) {
        const result = clone(now);
        for (const key of Object.keys(next)) if (!["__proto__", "constructor", "prototype"].includes(key)) {
          const p = pointer + "/" + escapePointer(key);
          const v = merge(result[key], next[key], object(before) ? before[key] : undefined, p);
          if (v !== undefined) Object.defineProperty(result, key, { value: v, writable: true, enumerable: true, configurable: true });
        }
        return result;
      }
      if ([...protectedPaths].some(p => p.startsWith(pointer + "/"))) { conflicts.push(pointer); return clone(now); }
      if (now === undefined || now === null || now === "" || Array.isArray(now) && !now.length || before !== undefined && JSON.stringify(now) === JSON.stringify(before)) { filled.push(pointer); return clone(next); }
      if (JSON.stringify(now) !== JSON.stringify(next)) conflicts.push(pointer);
      return clone(now);
    }
    return { record: merge(current, incoming, baseline, ""), filled, conflicts };
  }
  function mergeAcquired(current, incoming, { baseline = {}, dirty = [], previous = {} } = {}) {
    const next = clone(current), segments = pointer => pointer.slice(1).split("/").map(part => part.replace(/~1/g, "/").replace(/~0/g, "~"));
    const protectedPath = pointer => dirty.some(path => path === pointer || path.startsWith(pointer + "/") || pointer.startsWith(path + "/"));
    const valueAt = (value, parts) => { for (const part of parts) { if (!object(value) && !Array.isArray(value) || !own(value, part)) return undefined; value = value[part]; } return value; };
    for (const [pointer, value] of Object.entries(object(previous) ? previous : {})) {
      if (!pointer.startsWith("/") || protectedPath(pointer)) continue;
      const parts = segments(pointer);
      if (parts.some(part => ["__proto__", "constructor", "prototype"].includes(part)) || JSON.stringify(valueAt(next, parts)) !== JSON.stringify(value)) continue;
      const parents = [next];
      for (const part of parts.slice(0, -1)) parents.push(parents.at(-1)[part]);
      const parent = parents.at(-1), key = parts.at(-1), initial = valueAt(baseline, parts);
      if (initial !== undefined) Object.defineProperty(parent, key, { value: clone(initial), writable: true, enumerable: true, configurable: true });
      else {
        delete parent[key];
        for (let i = parents.length - 1; i > 0 && object(parents[i]) && !Object.keys(parents[i]).length; i--) delete parents[i - 1][parts[i - 1]];
      }
    }
    const merged = mergeMissing(next, incoming, { baseline, dirty }), managed = {};
    // Track only fields this acquisition filled, never pre-existing or manually edited values.
    function track(pointer, value) {
      if (object(value) && Object.keys(value).length && pointer !== "/media/icon") { for (const [key, child] of Object.entries(value)) track(pointer + "/" + escapePointer(key), child); }
      else Object.defineProperty(managed, pointer, { value: clone(value), enumerable: true });
    }
    for (const pointer of merged.filled) track(pointer, valueAt(merged.record, segments(pointer)));
    return { ...merged, managed };
  }
  return { MAX_BYTES, MAX_PREVIEWS, httpsURL, resolveImage, normalizeMedia, readmeImages, parseSource, requestJSON, metadataFields, acquire, mergeMissing, mergeAcquired };
});
