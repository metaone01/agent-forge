/* The Pages app reads generated JSON only. No write token or runtime service is used. */
(function () {
  "use strict";

  const page = document.body.dataset.page || "catalog";
  const base = new URL(page === "dashboard" ? "../" : page === "agent-dashboard" ? "../../" : "./", document.baseURI);
  const dataUrl = (path) => new URL(`data/${path}`.replace(/^data\/data\//, "data/"), base).href;
  const types = ["mcp", "plugin", "skill", "general", "bundle"];
  const colors = { mcp: "#0e766e", plugin: "#4876a7", skill: "#c87927", general: "#8667a9", bundle: "#53656a" };
  const state = { manifest: null, entries: [], detailCache: new Map(), filtered: [] };

  async function getJSON(url, optional) {
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`${response.status} ${url}`);
      return await response.json();
    } catch (error) {
      if (!optional) console.warn("Agent Forge data unavailable", error);
      return null;
    }
  }

  function escapeHTML(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }
  function formatDate(value) {
    if (!value) return "未知时间";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
  }
  function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
  function pathFor(path) { return /^https?:\/\//i.test(path) ? path : new URL(path.replace(/^\//, ""), base).href; }
  function safeHref(value) {
    try {
      const parsed = new URL(String(value || ""), base);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
    } catch (_) { return "#"; }
  }

  async function loadManifest() {
    state.manifest = await getJSON(dataUrl("manifest.json"), true);
    if (!state.manifest) state.manifest = await getJSON(dataUrl("latest.json"), true);
    return state.manifest || {};
  }

  function indexCandidates(manifest) {
    if (Array.isArray(manifest.indexes)) return manifest.indexes.map((item) => typeof item === "string" ? item : item.url || item.path).filter(Boolean);
    if (Array.isArray(manifest.sources) && manifest.sources.length) return manifest.sources.map((item) => typeof item === "string" ? item : item.url || item.path).filter(Boolean);
    const candidates = [];
    const agents = Array.isArray(manifest.agents) && manifest.agents.length ? manifest.agents : [{ id: "dsh" }];
    for (const agent of agents) {
      const agentId = typeof agent === "string" ? agent : agent.id;
      const agentTypes = typeof agent === "object" && Array.isArray(agent.types) ? agent.types : types;
      for (const type of agentTypes) {
        const typePath = typeof type === "string" ? type : type.type;
        const explicit = typeof type === "object" && (type.index || type.url);
        candidates.push(explicit || `data/${agentId}/${typePath}/index.json`);
      }
    }
    // Keep compatibility with the repository's pre-projection source layout.
    if (!manifest.agents && !manifest.indexes) types.forEach((type) => candidates.push(`../sources/${type}/index.json`));
    return [...new Set(candidates)];
  }

  function normalizeIndex(index, indexPath) {
    if (!index || typeof index !== "object") return [];
    const items = Array.isArray(index.packages) ? index.packages.map((record) => [record.name || record.id, record]) : Object.entries(index.packages || {});
    return items.filter(([name]) => name).map(([name, record]) => {
      const sourceBase = indexPath.replace(/index\.json(?:\?.*)?$/, "");
      const path = record.path || `${name}.json`;
      return {
        id: `${index.agentId || "unknown"}:${index.type || "general"}:${name}`,
        name, agentId: index.agentId || record.agentId || "unknown", type: index.type || record.type || "general",
        subtype: record.subtype || null, latest: record.latest || record.version || "unknown", versions: record.versions || [],
        path: pathFor(path.startsWith("http") ? path : sourceBase + path), recordRevision: record.recordRevision || index.revision,
        summary: record.summary || record.description || "", keywords: record.keywords || [], facets: record.facets || {}, indexPath,
        updatedAt: record.updatedAt || index.updatedAt || index.generatedAt, searchText: record.searchText || ""
      };
    });
  }

  async function loadEntries() {
    const manifest = await loadManifest();
    const paths = indexCandidates(manifest);
    const loaded = await Promise.all(paths.map(async (path) => {
      const absolute = /^https?:\/\//i.test(path) ? path : pathFor(path);
      return normalizeIndex(await getJSON(absolute, true), absolute);
    }));
    state.entries = loaded.flat();
    // A stale or empty manifest should not leave the page looking broken: it is a valid empty catalog.
    const stamp = document.getElementById("revision-stamp");
    if (stamp) stamp.textContent = `${manifest.revision || "未发布 revision"} · ${formatDate(manifest.generatedAt)}`;
    return state.entries;
  }

  function valueText(value) {
    if (Array.isArray(value)) return value.join(" ");
    if (value && typeof value === "object") return Object.values(value).flatMap(valueText).join(" ");
    return value == null ? "" : String(value);
  }
  async function hydrate(entry) {
    if (state.detailCache.has(entry.id)) return state.detailCache.get(entry.id);
    const record = await getJSON(entry.path, true);
    const full = record && typeof record === "object" ? { ...entry, ...record } : entry;
    full.name = full.name || full.packageId || entry.name;
    full.searchText = [full.name, full.displayName, full.description, full.summary, full.keywords, full.facets, full.customFacets, full.subtype, valueText(full.details)].join(" ").toLowerCase();
    state.detailCache.set(entry.id, full);
    return full;
  }

  async function filterEntries() {
    const query = document.getElementById("query").value.trim().toLowerCase();
    const agent = document.getElementById("agent-filter").value;
    const type = document.getElementById("type-filter").value;
    const subtype = document.getElementById("subtype-filter").value;
    let entries = state.entries.filter((entry) => (!agent || entry.agentId === agent) && (!type || entry.type === type) && (!subtype || entry.subtype === subtype));
    if (query) {
      await Promise.all(entries.slice(0, 100).map(hydrate));
      entries = entries.filter((entry) => ((state.detailCache.get(entry.id) || entry).searchText || [entry.name, entry.summary, entry.keywords].join(" ")).toLowerCase().includes(query));
    }
    const sort = document.getElementById("sort-select").value;
    entries.sort((a, b) => sort === "name" ? a.name.localeCompare(b.name) : sort === "updated" ? String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")) : a.name.localeCompare(b.name));
    state.filtered = entries;
    renderResults();
  }

  function renderResults() {
    const results = document.getElementById("results");
    const empty = document.getElementById("empty-state");
    const count = document.getElementById("result-count");
    const status = document.getElementById("catalog-status");
    if (!results) return;
    count.textContent = number(state.filtered.length);
    status.textContent = state.entries.length ? `· ${state.entries.length} 条静态索引记录` : "· 当前没有已发布记录";
    results.innerHTML = state.filtered.map((entry) => {
      const typeLabel = entry.type.toUpperCase();
      const record = state.detailCache.get(entry.id) || entry;
      const summary = record.description || record.summary || "暂无描述，打开详情查看来源与安装候选。";
      return `<article class="result-card"><div><a class="result-link" href="#/package/${encodeURIComponent(entry.agentId)}/${encodeURIComponent(entry.type)}/${encodeURIComponent(entry.name)}"><div class="result-title"><strong>${escapeHTML(record.displayName || entry.name)}</strong><span class="pill">${escapeHTML(typeLabel)}</span>${entry.subtype ? `<span class="pill pill-neutral">${escapeHTML(entry.subtype)}</span>` : ""}</div><p class="result-summary">${escapeHTML(summary)}</p></a><div class="result-meta"><span>${escapeHTML(entry.agentId)}</span><span>${entry.versions && entry.versions.length ? `${entry.versions.length} 个版本` : "版本 ${escapeHTML(entry.latest)}"}</span>${record.facets ? `<span>${Object.keys(record.facets).length} 个 facet 组</span>` : ""}</div></div><div class="result-version">最新版本<strong>${escapeHTML(entry.latest)}</strong>${entry.updatedAt ? `<span>${escapeHTML(formatDate(entry.updatedAt))}</span>` : ""}</div></article>`;
    }).join("");
    empty.hidden = state.filtered.length !== 0;
  }

  function setupFilters() {
    const agents = [...new Set(state.entries.map((entry) => entry.agentId).filter(Boolean))].sort();
    const subtypes = [...new Set(state.entries.map((entry) => entry.subtype).filter(Boolean))].sort();
    document.getElementById("agent-filter").insertAdjacentHTML("beforeend", agents.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    document.getElementById("subtype-filter").insertAdjacentHTML("beforeend", subtypes.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    ["query", "agent-filter", "type-filter", "subtype-filter", "sort-select"].forEach((id) => document.getElementById(id).addEventListener(id === "query" ? "input" : "change", filterEntries));
    document.getElementById("clear-filters").addEventListener("click", () => { ["query", "agent-filter", "type-filter", "subtype-filter"].forEach((id) => { document.getElementById(id).value = ""; }); filterEntries(); });
    window.addEventListener("keydown", (event) => { if (event.key === "/" && document.activeElement.tagName !== "INPUT") { event.preventDefault(); document.getElementById("query").focus(); } });
  }

  async function renderDetail(agent, type, encodedName) {
    const name = decodeURIComponent(encodedName);
    const entry = state.entries.find((item) => item.agentId === agent && item.type === type && item.name === name);
    const main = document.querySelector("main");
    if (!entry || !main) return;
    const record = await hydrate(entry);
    const details = record.details || record[`${type}Details`] || {};
    const links = record.links || {};
    const distributions = record.distributions || [];
    main.innerHTML = `<a class="back-link" href="./">← 返回目录</a><section class="detail-heading"><div><p class="eyebrow">${escapeHTML(type.toUpperCase())} · ${escapeHTML(agent)}</p><h1>${escapeHTML(record.displayName || record.name || name)}</h1><p class="lede">${escapeHTML(record.description || record.summary || "暂无描述")}</p></div><span class="pill">${record.compatibilityStatus === "unknown" ? "兼容范围未知" : "元数据记录"}</span></section><div class="detail-layout"><article class="panel detail-main"><div class="detail-section"><h2>包信息</h2><dl class="facts"><div><dt>Package ID</dt><dd>${escapeHTML(record.packageId || name)}</dd></div><div><dt>Agent</dt><dd>${escapeHTML(agent)}</dd></div><div><dt>最新版本</dt><dd>${escapeHTML(record.version || entry.latest)}</dd></div><div><dt>Subtype</dt><dd>${escapeHTML(record.subtype || "未指定")}</dd></div><div><dt>Agent 版本范围</dt><dd>${escapeHTML(record.agentVersionRange || "未提供")}</dd></div></dl></div><div class="detail-section"><h2>Facets</h2><div class="tag-list">${renderTags(record.facets)}${renderTags(record.customFacets, "custom") || "<span class=\"muted\">未声明 facet</span>"}</div></div><div class="detail-section"><h2>类型详情</h2><pre class="code-block">${escapeHTML(JSON.stringify(details, null, 2))}</pre></div></article><aside class="panel detail-side"><div class="detail-section"><h2>安装候选</h2>${distributions.length ? distributions.map((item) => { const href = safeHref(item.url || item.href); return `<a class="distribution" href="${escapeHTML(href)}" target="_blank" rel="noreferrer"><strong>${escapeHTML(item.name || item.type || "发行来源")}</strong><span>${escapeHTML(item.url || item.href || "未提供地址")}</span></a>`; }).join("") : "<p class=\"muted\">暂无发行来源。Agent Forge 不托管插件文件。</p>"}</div><div class="detail-section"><h2>文档链接</h2>${Object.entries(links).filter(([, value]) => value).map(([key, value]) => `<a class="external-link" href="${escapeHTML(safeHref(value))}" target="_blank" rel="noreferrer">${escapeHTML(key)} ↗</a>`).join("") || "<p class=\"muted\">暂无链接</p>"}</div></aside></div>`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderTags(facets, prefix) {
    if (!facets || typeof facets !== "object") return "";
    return Object.entries(facets).flatMap(([group, values]) => (Array.isArray(values) ? values : [values]).map((value) => `<span class="tag ${prefix ? "tag-custom" : ""}">${escapeHTML(group)}: ${escapeHTML(value)}</span>`)).join("");
  }

  async function startCatalog() {
    await loadEntries();
    setupFilters();
    const initialAgent = new URLSearchParams(location.search).get("agent");
    if (initialAgent) { document.getElementById("agent-filter").value = initialAgent; }
    if (location.hash.startsWith("#/package/")) { const parts = location.hash.slice("#/package/".length).split("/"); return renderDetail(decodeURIComponent(parts[0]), decodeURIComponent(parts[1]), parts.slice(2).join("/")); }
    await filterEntries();
    window.addEventListener("hashchange", () => { if (location.hash.startsWith("#/package/")) { const parts = location.hash.slice("#/package/".length).split("/"); renderDetail(decodeURIComponent(parts[0]), decodeURIComponent(parts[1]), parts.slice(2).join("/")); } else location.reload(); });
  }

  function metric(label, value, detail) { return `<div class="metric"><div class="metric-label">${escapeHTML(label)}</div><div class="metric-value">${escapeHTML(number(value))}</div>${detail ? `<div class="metric-detail">${escapeHTML(detail)}</div>` : ""}</div>`; }
  function listCount(map, key) { return map && typeof map === "object" ? Number(map[key] || 0) : 0; }
  function getTypes(agent) { return agent.types || agent.typeCounts || {}; }

  function aggregateFromEntries() {
    const agents = {};
    const facets = {};
    const typesCount = Object.fromEntries(types.map((type) => [type, 0]));
    state.entries.forEach((entry) => { const agent = agents[entry.agentId] ||= { id: entry.agentId, name: entry.agentId, packageCount: 0, versionCount: 0, types: {}, facets: {} }; agent.packageCount++; agent.versionCount += (entry.versions || []).length || 1; agent.types[entry.type] = (agent.types[entry.type] || 0) + 1; typesCount[entry.type]++; (entry.subtype ? (agent.subtypes ||= {})[entry.subtype] = ((agent.subtypes || {})[entry.subtype] || 0) + 1 : null); });
    return { revision: state.manifest && state.manifest.revision, generatedAt: state.manifest && state.manifest.generatedAt, totals: { agents: Object.keys(agents).length, packages: state.entries.length, versions: state.entries.reduce((sum, item) => sum + ((item.versions || []).length || 1), 0), bundles: typesCount.bundle }, agents: Object.values(agents), types: typesCount, facets, compatibility: { known: 0, unknown: 0 } };
  }

  function normalizeDashboard(raw) {
    if (!raw || typeof raw !== "object") return aggregateFromEntries();
    const result = { ...raw };
    const counts = raw.counts || {};
    result.totals = { ...(raw.totals || raw.summary || {}), agents: (raw.totals || {}).agents || raw.agentCount, packages: (raw.totals || {}).packages || counts.packages || counts.records, versions: (raw.totals || {}).versions || counts.versions, bundles: (raw.totals || {}).bundles || counts.bundles };
    result.agents = Array.isArray(raw.agents) ? raw.agents : Object.entries(raw.agents || {}).map(([id, value]) => ({ id, ...value }));
    result.types = raw.types || raw.typeCounts || raw.byType || {};
    result.facets = raw.facets || raw.facetCounts || {};
    result.compatibility = raw.compatibility || { known: raw.knownCompatibility || counts.knownCompatibilityTargets || 0, unknown: raw.unknownCompatibility || counts.unknownCompatibilityTargets || 0 };
    return result;
  }
  function barRows(values, limit, valueLabel) {
    const entries = Object.entries(values || {}).map(([label, value]) => [label, Number(value) || 0]).sort((a, b) => b[1] - a[1]).slice(0, limit);
    const max = Math.max(1, ...entries.map(([, value]) => value));
    return entries.map(([label, value]) => `<div class="bar-row"><span>${escapeHTML(label)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(1, value / max * 100)}%"></div></div><span class="bar-value">${escapeHTML(valueLabel ? valueLabel(value) : number(value))}</span></div>`).join("");
  }
  function renderDashboard(dashboard) {
    const totals = dashboard.totals || {};
    document.getElementById("dashboard-updated").textContent = `${dashboard.revision || "未发布 revision"} · ${formatDate(dashboard.generatedAt)}`;
    document.getElementById("global-metrics").innerHTML = [metric("Agent", totals.agents || dashboard.agents.length, "已生成公开投影"), metric("包", totals.packages || totals.packageCount, "跨所有类型"), metric("版本", totals.versions || totals.versionCount, "元数据版本记录"), metric("Bundle", totals.bundles || totals.bundleCount, "包含嵌套统计")].join("");
    const agentValues = Object.fromEntries(dashboard.agents.map((agent) => [agent.name || agent.id, agent.packageCount || agent.packages || agent.count || 0]));
    document.getElementById("agent-chart").innerHTML = Object.keys(agentValues).length ? barRows(agentValues, 12) : `<p class="muted">暂无 Agent 统计</p>`;
    document.getElementById("agent-table").innerHTML = `<table><caption>Agent 包数量</caption><tbody>${Object.entries(agentValues).map(([label, value]) => `<tr><th>${escapeHTML(label)}</th><td>${number(value)}</td></tr>`).join("")}</tbody></table>`;
    const typeValues = Object.fromEntries(types.map((type) => [type, Number(dashboard.types[type] || 0)]));
    const typeTotal = Math.max(1, Object.values(typeValues).reduce((a, b) => a + b, 0));
    let cursor = 0; const stops = types.map((type) => { const start = cursor / typeTotal * 100; cursor += typeValues[type]; return `${colors[type]} ${start}% ${cursor / typeTotal * 100}%`; }).join(", ");
    document.getElementById("type-chart").innerHTML = `<div class="donut" style="background:conic-gradient(${stops})"></div><div class="legend">${types.map((type) => `<div class="legend-item"><i class="legend-swatch" style="background:${colors[type]}"></i><span>${type.toUpperCase()} · ${number(typeValues[type])}</span></div>`).join("")}</div>`;
    document.getElementById("type-table").innerHTML = `<table><caption>类型数量</caption><tbody>${types.map((type) => `<tr><th>${type}</th><td>${number(typeValues[type])}</td></tr>`).join("")}</tbody></table>`;
    document.getElementById("facet-chart").innerHTML = Object.keys(dashboard.facets).length ? barRows(dashboard.facets, 10) : `<p class="muted">暂无 facet 统计</p>`;
    const compatibility = dashboard.compatibility || {}; const known = Number(compatibility.known || 0); const unknown = Number(compatibility.unknown || 0); const total = Math.max(1, known + unknown);
    document.getElementById("compat-chart").innerHTML = `<div class="compat-item known"><span>已声明范围</span><div class="bar-track"><div class="bar-fill" style="width:${known / total * 100}%"></div></div><span class="bar-value">${number(known)}</span></div><div class="compat-item"><span>范围未知</span><div class="bar-track"><div class="bar-fill" style="width:${unknown / total * 100}%"></div></div><span class="bar-value">${number(unknown)}</span></div>`;
    document.getElementById("agent-cards").innerHTML = dashboard.agents.length ? dashboard.agents.map((agent) => { const id = agent.id || agent.name; const count = agent.packageCount || agent.packages || agent.count || 0; const typeCount = Object.keys(getTypes(agent)).length; return `<a class="agent-card" href="agent/?id=${encodeURIComponent(id)}"><div class="agent-card-title"><span>${escapeHTML(agent.name || id)}</span><span class="pill">${escapeHTML(id)}</span></div><div class="agent-card-stats"><span>${number(count)} 个包</span><span>${number(typeCount)} 类</span></div></a>`; }).join("") : `<p class="muted">暂无 Agent 统计</p>`;
  }

  async function startDashboard() {
    const raw = await getJSON(dataUrl("dashboard.json"), true);
    if (!raw || !Array.isArray(raw.agents) || !raw.agents.length) await loadEntries();
    const dashboard = normalizeDashboard(raw);
    if (!dashboard.agents.length && state.entries.length) {
      const fallback = aggregateFromEntries();
      dashboard.agents = fallback.agents;
      dashboard.totals.agents = fallback.totals.agents;
    }
    renderDashboard(dashboard);
  }
  async function startAgentDashboard() {
    const id = new URLSearchParams(location.search).get("id") || location.pathname.split("/").filter(Boolean).pop() || "dsh";
    const global = normalizeDashboard(await getJSON(dataUrl("dashboard.json"), true));
    const raw = await getJSON(dataUrl(`agents/${encodeURIComponent(id)}/dashboard.json`), true);
    const agent = raw ? normalizeDashboard(raw) : global.agents.find((item) => (item.id || item.name) === id) || { id, name: id, types: {}, facets: {}, packages: 0, versions: 0 };
    const totals = agent.totals || agent.summary || agent;
    document.getElementById("agent-title").textContent = agent.name || id;
    document.getElementById("agent-description").textContent = agent.description || `Agent ${id} 的静态元数据投影。`;
    document.getElementById("dashboard-updated").textContent = `${agent.revision || global.revision || "未发布 revision"} · ${formatDate(agent.generatedAt || global.generatedAt)}`;
    document.getElementById("global-metrics").innerHTML = [metric("包", totals.packages || totals.packageCount || agent.packageCount, "该 Agent 投影"), metric("版本", totals.versions || totals.versionCount || agent.versionCount, "元数据版本记录"), metric("Bundle", totals.bundles || totals.bundleCount, "包含嵌套统计"), metric("范围未知", totals.unknownCompatibility || agent.unknownCompatibility, "未提供 Agent 版本范围")].join("");
    const typeValues = agent.types || agent.typeCounts || {};
    document.getElementById("agent-type-chart").innerHTML = Object.keys(typeValues).length ? barRows(typeValues, 10) : `<p class="muted">暂无类型统计</p>`;
    const facetValues = agent.facets || agent.facetCounts || {};
    document.getElementById("agent-facet-chart").innerHTML = Object.keys(facetValues).length ? barRows(facetValues, 10) : `<p class="muted">暂无 facet 统计</p>`;
    const recent = agent.recentPackages || agent.recent || [];
    document.getElementById("recent-packages").innerHTML = recent.length ? recent.map((item) => `<article class="result-card"><div><div class="result-title"><strong>${escapeHTML(item.displayName || item.name || item.packageId)}</strong><span class="pill">${escapeHTML(item.type || "package")}</span></div><p class="result-summary">${escapeHTML(item.description || "暂无描述")}</p></div><div class="result-version"><strong>${escapeHTML(item.version || item.latest || "unknown")}</strong><span>${escapeHTML(formatDate(item.updatedAt))}</span></div></article>`).join("") : `<p class="muted">暂无最近更新记录</p>`;
  }
  if (page === "dashboard") startDashboard(); else if (page === "agent-dashboard") startAgentDashboard(); else startCatalog();
}());
