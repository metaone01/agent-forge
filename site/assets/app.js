/* Dashboard reads precomputed statistics only; package search is temporarily disabled. */
(function () {
  "use strict";
  const page = document.body.dataset.page;
  const base = new URL(page === "dashboard" ? "../" : page === "agent-dashboard" ? "../../" : "./", document.baseURI);
  const dataUrl = (path) => new URL(`data/${path}`, base).href;
  const types = ["mcp", "plugin", "skill", "general", "bundle"];
  const colors = { mcp: "#0e766e", plugin: "#4876a7", skill: "#c87927", general: "#8667a9", bundle: "#53656a" };
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" });
  async function getJSON(url) {
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (!response.ok) return null;
      return await response.json();
    } catch (_) { return null; }
  }
  function escapeHTML(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }
  function formatDate(value) {
    if (!value) return "未知时间";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : dateFormatter.format(date);
  }
  function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
  function metric(label, value, detail) { return `<div class="metric"><div class="metric-label">${escapeHTML(label)}</div><div class="metric-value">${escapeHTML(number(value))}</div>${detail ? `<div class="metric-detail">${escapeHTML(detail)}</div>` : ""}</div>`; }
  function listCount(map, key) { return map && typeof map === "object" ? Number(map[key] || 0) : 0; }
  function getTypes(agent) { return agent.types || agent.typeCounts || {}; }

  function normalizeDashboard(raw) {
    if (!raw || typeof raw !== "object") raw = {};
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
    if (!raw) {
      document.getElementById("dashboard-updated").textContent = "统计数据暂时不可用，请稍后重试。";
      return;
    }
    const dashboard = normalizeDashboard(raw);
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
  if (page === "dashboard") startDashboard(); else if (page === "agent-dashboard") startAgentDashboard(); else location.replace(new URL("dashboard/", base).href);
}());
