/* The Pages app reads generated JSON only. No write token or runtime service is used. */
(function () {
  "use strict";

  const page = document.body.dataset.page || "catalog";
  const ui = window.ForgeUI;
  ui.addMessages({
    "zh-CN": { "media.load": "加载外链图片", "media.hide": "隐藏外链图片", "media.notice": "图片来自第三方，未核验。加载后会向图片站点发送请求；本页不发送 Referer。", "media.preview": "静态预览", "media.blocked": "尚未加载", "media.failed": "图片加载失败" },
    en: { "media.load": "Load external images", "media.hide": "Hide external images", "media.notice": "Unverified third-party images. Loading sends requests to image hosts, without a Referer from this page.", "media.preview": "Static previews", "media.blocked": "Not loaded", "media.failed": "Image unavailable" }
  });
  const t = ui.t;
  const base = ui.siteBase;
  const dataBase = new URL("data/", base);
  const dataUrl = (path) => new URL(`data/${path}`.replace(/^data\/data\//, "data/"), base).href;
  const types = ["mcp", "plugin", "skill", "general", "bundle"];
  const colors = { mcp: "#0e766e", plugin: "#4876a7", skill: "#c87927", general: "#8667a9", bundle: "#53656a" };
  const pageSize = 50;
  const state = { manifest: null, entries: [], detailCache: new Map(), detailPending: new Map(), filtered: [], resultPage: 0, sort: "", ready: false, catalogNodes: null, detailRoute: 0, mediaEnabled: false, dashboard: null, agentDashboard: null };

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
    if (!value) return t("未知时间");
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(ui.locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }
  function number(value) { return Number(value || 0).toLocaleString(ui.locale); }
  function pathFor(path) { return /^https?:\/\//i.test(path) ? path : new URL(path.replace(/^\//, ""), base).href; }
  // Manifest source paths are relative to the generated data directory, not the page root.
  function dataPathFor(path) {
    if (/^https?:\/\//i.test(path)) return path;
    const normalized = String(path).replace(/^\/+/, "");
    return new URL(normalized, normalized.startsWith("data/") ? base : dataBase).href;
  }
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
    const items = Array.isArray(index.packages) ? index.packages.map((record) => [record.name || record.packageId || record.id, record]) : Object.entries(index.packages || {});
    return items.filter(([name]) => name).map(([name, record]) => {
      const sourceBase = indexPath.replace(/index\.json(?:\?.*)?$/, "");
      const path = record.path || `${name}.json`;
      return {
        key: `${index.agentId || record.agentId || "unknown"}:${index.type || record.type || "general"}:${name}`,
        id: record.packageId || record.id || "", packageId: record.packageId || record.id || "", routeName: name,
        name: record.name || name, displayName: record.displayName || "", agentId: index.agentId || record.agentId || "unknown", type: index.type || record.type || "general",
        subtype: record.subtype || null, latest: record.latest || record.version || "unknown", versions: record.versions || [],
        path: pathFor(path.startsWith("http") ? path : sourceBase + path), recordRevision: record.recordRevision || index.revision,
        summary: record.summary || record.description || "", keywords: record.keywords || [], facets: record.facets || {}, customFacets: record.customFacets || {}, indexPath,
        media: record.media || null,
        updatedAt: record.updatedAt || index.updatedAt || index.generatedAt,
        searchText: (record.searchText || [name, record.name, record.id, record.packageId, record.displayName, record.summary, record.description, valueText(record.keywords), valueText(record.facets), valueText(record.customFacets), record.subtype].join(" ")).toLowerCase()
      };
    });
  }

  async function loadEntries() {
    const manifest = await loadManifest();
    const paths = indexCandidates(manifest);
    const loaded = await Promise.all(paths.map(async (path) => {
      const absolute = dataPathFor(path);
      return normalizeIndex(await getJSON(absolute, true), absolute);
    }));
    state.entries = loaded.flat();
    // A stale or empty manifest should not leave the page looking broken: it is a valid empty catalog.
    const stamp = document.getElementById("revision-stamp");
    if (stamp) setText("revision-stamp", `${manifest.revision || t("未发布 revision")} · ${formatDate(manifest.generatedAt)}`);
    return state.entries;
  }

  function valueText(value) {
    if (Array.isArray(value)) return value.join(" ");
    if (value && typeof value === "object") return Object.values(value).flatMap(valueText).join(" ");
    return value == null ? "" : String(value);
  }
  async function hydrate(entry) {
    if (state.detailCache.has(entry.key)) return state.detailCache.get(entry.key);
    if (state.detailPending.has(entry.key)) return state.detailPending.get(entry.key);
    const pending = (async () => {
      const record = await getJSON(entry.path, true);
      const full = record && typeof record === "object" ? { ...entry, ...record } : { ...entry };
      full.name = record && record.name || entry.name;
      full.packageId = record && (record.packageId || record.id) || entry.packageId;
      state.detailCache.set(entry.key, full);
      if (state.detailCache.size > 20) state.detailCache.delete(state.detailCache.keys().next().value);
      return full;
    })();
    state.detailPending.set(entry.key, pending);
    try { return await pending; } finally { state.detailPending.delete(entry.key); }
  }

  function filterEntries() {
    if (!document.getElementById("query")) return;
    const query = document.getElementById("query").value.trim().toLowerCase();
    const agent = document.getElementById("agent-filter").value;
    const type = document.getElementById("type-filter").value;
    const subtype = document.getElementById("subtype-filter").value;
    const sort = document.getElementById("sort-select").value;
    if (sort !== state.sort) {
      state.entries.sort((a, b) => sort === "updated" ? String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")) : a.name.localeCompare(b.name));
      state.sort = sort;
    }
    state.filtered = state.entries.filter((entry) => (!agent || entry.agentId === agent) && (!type || entry.type === type) && (!subtype || entry.subtype === subtype) && (!query || entry.searchText.includes(query)));
    state.resultPage = 0;
    renderResults();
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (!node) return;
    node.removeAttribute("data-i18n");
    node.textContent = value;
  }
  function safeImageURL(value) {
    if (typeof value !== "string" || value.length > 4096 || !value.startsWith("https://") || /[\s<>"{}|\\^`\x00-\x1f\x7f]/.test(value)) return "";
    try {
      const url = new URL(value);
      return url.protocol === "https:" && url.hostname && !url.username && !url.password ? url.href : "";
    } catch (_) { return ""; }
  }
  function imageSlot(asset, kind, fallback) {
    const url = asset && safeImageURL(asset.url);
    const alt = asset && typeof asset.alt === "string" ? asset.alt : fallback;
    const placeholder = '<span class="media-placeholder">' + escapeHTML(fallback) + '</span>';
    const image = url && state.mediaEnabled ? '<img src="' + escapeHTML(url) + '" alt="' + escapeHTML(alt) + '" loading="lazy" decoding="async" referrerpolicy="no-referrer">' : '';
    return '<span class="media-slot media-' + kind + '"' + (url && !state.mediaEnabled ? ' title="' + escapeHTML(t("media.blocked")) + '"' : '') + '>' + placeholder + image + '</span>';
  }
  function mediaControls() {
    return '<div class="media-controls"><button type="button" data-media-consent aria-pressed="' + state.mediaEnabled + '">' + escapeHTML(t(state.mediaEnabled ? "media.hide" : "media.load")) + '</button><p>' + escapeHTML(t("media.notice")) + '</p></div>';
  }
  function renderGallery(record) {
    const media = record.media || {};
    const previews = Array.isArray(media.previews) ? media.previews.slice(0, 12).filter((asset) => asset && safeImageURL(asset.url)) : [];
    if (!previews.length && !(media.icon && safeImageURL(media.icon.url))) return '';
    return '<section class="detail-section media-section"><h2>' + escapeHTML(t("media.preview")) + '</h2>' + mediaControls() + (media.icon ? imageSlot(media.icon, "icon", record.type.toUpperCase()) : '') + '<div class="preview-gallery">' + previews.map((asset) => '<figure>' + imageSlot(asset, "preview", t("media.preview")) + '<figcaption>' + escapeHTML(asset.alt || '') + (asset.theme ? ' · ' + escapeHTML(asset.theme) : '') + '</figcaption></figure>').join('') + '</div></section>';
  }
  function bindMedia(root) {
    root.querySelectorAll('[data-media-consent]').forEach((button) => button.addEventListener('click', () => {
      state.mediaEnabled = !state.mediaEnabled;
      renderRoute(false);
    }));
    root.querySelectorAll('.media-slot img').forEach((image) => {
      const failed = () => { image.hidden = true; image.removeAttribute('src'); image.parentElement.querySelector('.media-placeholder').textContent = t('media.failed'); };
      image.addEventListener('error', failed, { once: true });
      if (image.complete && image.naturalWidth === 0) failed();
    });
  }

  function renderResults() {
    const results = document.getElementById("results");
    if (!results) return;
    setText("result-count", number(state.filtered.length));
    setText("catalog-status", state.entries.length ? t("catalog.records", { count: number(state.entries.length) }) : t("catalog.empty"));
    if (state.manifest) setText("revision-stamp", (state.manifest.revision || t("未发布 revision")) + " · " + formatDate(state.manifest.generatedAt));
    const start = state.resultPage * pageSize;
    const visible = state.filtered.slice(start, start + pageSize);
    const hasMedia = visible.some((entry) => { const media = (state.detailCache.get(entry.key) || entry).media; return media && (media.icon || (Array.isArray(media.previews) && media.previews.length)); });
    results.innerHTML = (hasMedia ? mediaControls() : '') + visible.map((entry) => {
      const record = state.detailCache.get(entry.key) || entry;
      const href = "#/package/" + [entry.agentId, entry.type, entry.routeName].map(encodeURIComponent).join("/");
      const media = record.media || {};
      const icon = imageSlot(media.icon, "icon", entry.type.toUpperCase().slice(0, 1));
      const preview = entry.subtype === "skin" && Array.isArray(media.previews) && media.previews[0] ? imageSlot(media.previews[0], "thumbnail", t("media.preview")) : '';
      const title = '<div class="result-title">' + icon + '<strong>' + escapeHTML(record.displayName || record.name || entry.name) + '</strong><span class="pill">' + escapeHTML(entry.type.toUpperCase()) + '</span>' + (entry.subtype ? '<span class="pill pill-neutral">' + escapeHTML(entry.subtype) + '</span>' : '') + '</div>';
      const summary = record.description || record.summary || t("暂无描述，打开详情查看来源与安装候选。");
      const versions = entry.versions.length ? t("catalog.versions", { count: number(entry.versions.length) }) : t("catalog.version", { version: entry.latest });
      return '<article class="result-card"><div><a class="result-link" href="' + escapeHTML(href) + '">' + preview + title + '<p class="result-summary">' + escapeHTML(summary) + '</p></a><div class="result-meta"><span>' + escapeHTML(entry.agentId) + '</span><span>' + escapeHTML(versions) + '</span></div>' + renderPackageTags(record, 8) + '</div><div class="result-version"><strong>' + escapeHTML(entry.latest) + '</strong><span>' + escapeHTML(formatDate(entry.updatedAt)) + '</span></div></article>';
    }).join("");
    bindMedia(results);
    document.getElementById("empty-state").hidden = state.filtered.length > 0;
    const pages = Math.max(1, Math.ceil(state.filtered.length / pageSize));
    document.getElementById("pagination").hidden = pages <= 1;
    document.getElementById("page-number").max = pages;
    document.getElementById("page-number").value = state.resultPage + 1;
    setText("page-total", t("catalog.pages", { count: number(pages) }));
    setText("page-range", state.filtered.length ? number(start + 1) + "–" + number(Math.min(start + pageSize, state.filtered.length)) + " / " + number(state.filtered.length) : "");
    document.getElementById("previous-page").disabled = state.resultPage === 0;
    document.getElementById("next-page").disabled = state.resultPage + 1 >= pages;
  }

  function setupFilters() {
    const agents = [...new Set(state.entries.map((entry) => entry.agentId).filter(Boolean))].sort();
    const subtypes = [...new Set(state.entries.map((entry) => entry.subtype).filter(Boolean))].sort();
    document.getElementById("agent-filter").insertAdjacentHTML("beforeend", agents.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    document.getElementById("subtype-filter").insertAdjacentHTML("beforeend", subtypes.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    let searchTimer;
    const applyFilters = () => { clearTimeout(searchTimer); filterEntries(); };
    document.getElementById("query").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(filterEntries, 150); });
    ["agent-filter", "type-filter", "subtype-filter", "sort-select"].forEach((id) => document.getElementById(id).addEventListener("change", applyFilters));
    document.getElementById("clear-filters").addEventListener("click", () => { ["query", "agent-filter", "type-filter", "subtype-filter"].forEach((id) => { document.getElementById(id).value = ""; }); applyFilters(); });
    const changePage = (pageNumber) => {
      if (!Number.isFinite(pageNumber)) pageNumber = 0;
      state.resultPage = Math.max(0, Math.min(Math.ceil(state.filtered.length / pageSize) - 1, Math.floor(pageNumber)));
      renderResults();
      document.querySelector(".catalog-toolbar").scrollIntoView({ block: "start" });
    };
    document.getElementById("previous-page").addEventListener("click", () => changePage(state.resultPage - 1));
    document.getElementById("next-page").addEventListener("click", () => changePage(state.resultPage + 1));
    document.getElementById("page-number").addEventListener("change", (event) => changePage(Number(event.target.value) - 1));
    window.addEventListener("keydown", (event) => { if (event.key === "/" && !event.ctrlKey && !event.altKey && !event.metaKey && !["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(document.activeElement.tagName) && !document.activeElement.isContentEditable) { event.preventDefault(); document.getElementById("query").focus(); } });
  }

  function tagItems(record) {
    const items = [];
    for (const [kind, facets] of [["controlled", record.facets], ["custom", record.customFacets]]) {
      if (!facets || typeof facets !== "object" || Array.isArray(facets)) continue;
      for (const [group, values] of Object.entries(facets)) {
        for (const value of Array.isArray(values) ? values : [values]) {
          if (value == null || String(value).trim() === "") continue;
          items.push({ kind, group, text: group + ": " + String(value) });
        }
      }
    }
    for (const value of Array.isArray(record.keywords) ? record.keywords : []) {
      if (value != null && String(value).trim()) items.push({ kind: "keywords", text: String(value) });
    }
    return items;
  }
  function renderPackageTags(record, limit = Infinity) {
    const items = tagItems(record);
    if (!items.length) return "";
    const tags = items.slice(0, limit).map((item) => '<span class="tag tag-' + item.kind + (item.kind === "controlled" ? " facet-color-" + facetColor(item.group) : "") + '" title="' + escapeHTML(t("tags." + item.kind)) + '">' + escapeHTML(item.text) + '</span>').join("");
    const more = items.length > limit ? '<span class="tag tag-more">' + escapeHTML(t("tags.more", { count: number(items.length - limit) })) + '</span>' : '';
    return '<div class="tag-list" aria-label="' + escapeHTML(t("标签")) + '">' + tags + more + '</div>';
  }
  function facetColor(group) {
    let hash = 0;
    for (const char of group) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    return hash % 5;
  }
  async function renderDetail(agent, type, name, scroll = true) {
    const route = ++state.detailRoute;
    const entry = state.entries.find((item) => item.agentId === agent && item.type === type && item.routeName === name);
    const main = document.querySelector("main");
    if (!main) return;
    if (!entry) {
      main.innerHTML = '<a class="back-link" href="#">' + escapeHTML(t("← 返回目录")) + '</a><p role="status">' + escapeHTML(t("detail.missing")) + '</p>';
      return;
    }
    const record = await hydrate(entry);
    // A late fetch must not replace the catalog or a more recently selected detail.
    if (route !== state.detailRoute) return;
    const details = record.details || record[type + "Details"] || {};
    const distributions = Array.isArray(record.distributions) ? record.distributions : [];
    const fact = (label, value) => '<div><dt>' + escapeHTML(t(label)) + '</dt><dd>' + escapeHTML(value) + '</dd></div>';
    const tags = renderPackageTags(record);
    const sources = distributions.map((item) => '<a class="distribution" href="' + escapeHTML(safeHref(item.url || item.href)) + '" target="_blank" rel="noreferrer"><strong>' + escapeHTML(item.name || item.type || t("发行来源")) + '</strong><span>' + escapeHTML(item.url || item.href || t("未提供地址")) + '</span></a>').join("") || '<p class="muted">' + escapeHTML(t("暂无发行来源。Agent Forge 不托管插件文件。")) + '</p>';
    const links = Object.entries(record.links || {}).filter(([, value]) => value).map(([key, value]) => '<a class="external-link" href="' + escapeHTML(safeHref(value)) + '" target="_blank" rel="noreferrer">' + escapeHTML(key) + ' ↗</a>').join("") || '<p class="muted">' + escapeHTML(t("暂无链接")) + '</p>';
    main.innerHTML = '<a class="back-link" href="#">' + escapeHTML(t("← 返回目录")) + '</a><section class="detail-heading"><div><p class="eyebrow">' + escapeHTML(type.toUpperCase()) + ' · ' + escapeHTML(agent) + '</p><h1>' + escapeHTML(record.displayName || record.name || name) + '</h1><p class="lede">' + escapeHTML(record.description || record.summary || t("暂无描述")) + '</p></div><span class="pill">' + escapeHTML(t(record.compatibilityStatus === "unknown" ? "兼容范围未知" : "元数据记录")) + '</span></section><div class="detail-layout"><article class="panel detail-main"><div class="detail-section"><h2>' + escapeHTML(t("包信息")) + '</h2><dl class="facts">' + fact("Package ID", record.packageId || t("未提供")) + fact("Agent", agent) + fact("最新版本", record.version || entry.latest) + fact("Subtype", record.subtype || t("未指定")) + fact("Agent 版本范围", record.agentVersionRange || t("未提供")) + '</dl></div>' + (tags ? '<div class="detail-section"><h2>' + escapeHTML(t("标签")) + '</h2>' + tags + '</div>' : '') + renderGallery(record) + '<div class="detail-section"><h2>' + escapeHTML(t("类型详情")) + '</h2><pre class="code-block">' + escapeHTML(JSON.stringify(details, null, 2)) + '</pre></div></article><aside class="panel detail-side"><div class="detail-section"><h2>' + escapeHTML(t("安装候选")) + '</h2>' + sources + '</div><div class="detail-section"><h2>' + escapeHTML(t("文档链接")) + '</h2>' + links + '</div></aside></div>';
    bindMedia(main);
    if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function renderRoute(scroll = true) {
    const main = document.querySelector("main");
    if (location.hash.startsWith("#/package/")) {
      try {
        const parts = location.hash.slice("#/package/".length).split("/");
        return renderDetail(decodeURIComponent(parts[0]), decodeURIComponent(parts[1]), decodeURIComponent(parts.slice(2).join("/")), scroll);
      } catch (_) {
        return renderDetail("", "", "", scroll);
      }
    }
    ++state.detailRoute;
    if (state.catalogNodes && !document.getElementById("query")) main.replaceChildren(...state.catalogNodes);
    ui.apply(main);
    renderResults();
  }
  async function startCatalog() {
    await loadEntries();
    setupFilters();
    const initialAgent = new URLSearchParams(location.search).get("agent");
    if (initialAgent) document.getElementById("agent-filter").value = initialAgent;
    filterEntries();
    state.catalogNodes = Array.from(document.querySelector("main").childNodes);
    state.ready = true;
    window.addEventListener("hashchange", () => renderRoute());
    await renderRoute();
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
    setText("dashboard-updated", (dashboard.revision || t("未发布 revision")) + " · " + formatDate(dashboard.generatedAt));
    document.getElementById("global-metrics").innerHTML = [metric("Agent", totals.agents || dashboard.agents.length, t("已生成公开投影")), metric(t("包"), totals.packages || totals.packageCount, t("跨所有类型")), metric(t("版本"), totals.versions || totals.versionCount, t("元数据版本记录")), metric(t("Bundle"), totals.bundles || totals.bundleCount, t("包含嵌套统计"))].join("");
    const agentValues = Object.fromEntries(dashboard.agents.map((agent) => [agent.name || agent.id, agent.packageCount || agent.packages || agent.count || 0]));
    document.getElementById("agent-chart").innerHTML = Object.keys(agentValues).length ? barRows(agentValues, 12) : '<p class="muted">' + escapeHTML(t("暂无 Agent 统计")) + '</p>';
    document.getElementById("agent-table").innerHTML = '<table><caption>' + escapeHTML(t("Agent 包数量")) + '</caption><tbody>' + Object.entries(agentValues).map(([label, value]) => '<tr><th>' + escapeHTML(label) + '</th><td>' + number(value) + '</td></tr>').join("") + '</tbody></table>';
    const typeValues = Object.fromEntries(types.map((type) => [type, Number(dashboard.types[type] || 0)]));
    const typeTotal = Math.max(1, Object.values(typeValues).reduce((a, b) => a + b, 0));
    let cursor = 0;
    const stops = types.map((type) => {
      const start = cursor / typeTotal * 100;
      cursor += typeValues[type];
      return colors[type] + " " + start + "% " + cursor / typeTotal * 100 + "%";
    }).join(", ");
    document.getElementById("type-chart").innerHTML = '<div class="donut" style="background:conic-gradient(' + stops + ')"></div><div class="legend">' + types.map((type) => '<div class="legend-item"><i class="legend-swatch" style="background:' + colors[type] + '"></i><span>' + escapeHTML(type.toUpperCase()) + ' · ' + number(typeValues[type]) + '</span></div>').join("") + '</div>';
    document.getElementById("type-table").innerHTML = '<table><caption>' + escapeHTML(t("类型数量")) + '</caption><tbody>' + types.map((type) => '<tr><th>' + escapeHTML(type) + '</th><td>' + number(typeValues[type]) + '</td></tr>').join("") + '</tbody></table>';
    document.getElementById("facet-chart").innerHTML = Object.keys(dashboard.facets).length ? barRows(dashboard.facets, 10) : '<p class="muted">' + escapeHTML(t("暂无 facet 统计")) + '</p>';
    const compatibility = dashboard.compatibility || {};
    const known = Number(compatibility.known || 0), unknown = Number(compatibility.unknown || 0);
    const total = Math.max(1, known + unknown);
    document.getElementById("compat-chart").innerHTML = '<div class="compat-item known"><span>' + escapeHTML(t("已声明范围")) + '</span><div class="bar-track"><div class="bar-fill" style="width:' + known / total * 100 + '%"></div></div><span class="bar-value">' + number(known) + '</span></div><div class="compat-item"><span>' + escapeHTML(t("范围未知")) + '</span><div class="bar-track"><div class="bar-fill" style="width:' + unknown / total * 100 + '%"></div></div><span class="bar-value">' + number(unknown) + '</span></div>';
    document.getElementById("agent-cards").innerHTML = dashboard.agents.length ? dashboard.agents.map((agent) => {
      const id = agent.id || agent.name;
      const count = agent.packageCount || agent.packages || agent.count || 0;
      const href = new URL("dashboard/agent/?id=" + encodeURIComponent(id), base).href;
      return '<a class="agent-card" href="' + escapeHTML(href) + '"><div class="agent-card-title"><span>' + escapeHTML(agent.name || id) + '</span><span class="pill">' + escapeHTML(id) + '</span></div><div class="agent-card-stats"><span>' + escapeHTML(t("agent.packages", { count: number(count) })) + '</span><span>' + escapeHTML(t("agent.types", { count: number(Object.keys(getTypes(agent)).length) })) + '</span></div></a>';
    }).join("") : '<p class="muted">' + escapeHTML(t("暂无 Agent 统计")) + '</p>';
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
    state.dashboard = dashboard;
    state.ready = true;
    renderDashboard(dashboard);
  }
  async function startAgentDashboard() {
    const id = new URLSearchParams(location.search).get("id") || "dsh";
    const global = normalizeDashboard(await getJSON(dataUrl("dashboard.json"), true));
    const raw = await getJSON(dataUrl(`agents/${encodeURIComponent(id)}/dashboard.json`), true);
    const agent = raw ? normalizeDashboard(raw) : global.agents.find((item) => (item.id || item.name) === id) || { id, name: id, types: {}, facets: {}, packages: 0, versions: 0 };
    state.agentDashboard = { agent, global, id };
    state.ready = true;
    renderAgentDashboard(state.agentDashboard);
  }
  function renderAgentDashboard({ agent, global, id }) {
    const totals = agent.totals || agent.summary || agent;
    setText("agent-title", agent.name || id);
    setText("agent-description", agent.description || t("agent.description", { id }));
    setText("dashboard-updated", `${agent.revision || global.revision || t("未发布 revision")} · ${formatDate(agent.generatedAt || global.generatedAt)}`);
    document.getElementById("global-metrics").innerHTML = [metric(t("包"), totals.packages || totals.packageCount || agent.packageCount, t("该 Agent 投影")), metric(t("版本"), totals.versions || totals.versionCount || agent.versionCount, t("元数据版本记录")), metric(t("Bundle"), totals.bundles || totals.bundleCount, t("包含嵌套统计")), metric(t("范围未知"), totals.unknownCompatibility || agent.unknownCompatibility, t("未提供 Agent 版本范围"))].join("");
    const typeValues = agent.types || agent.typeCounts || {};
    document.getElementById("agent-type-chart").innerHTML = Object.keys(typeValues).length ? barRows(typeValues, 10) : `<p class="muted">${escapeHTML(t("暂无类型统计"))}</p>`;
    const facetValues = agent.facets || agent.facetCounts || {};
    document.getElementById("agent-facet-chart").innerHTML = Object.keys(facetValues).length ? barRows(facetValues, 10) : `<p class="muted">${escapeHTML(t("暂无 facet 统计"))}</p>`;
    const recent = agent.recentPackages || agent.recent || [];
    document.getElementById("recent-packages").innerHTML = recent.length ? recent.map((item) => `<article class="result-card"><div><div class="result-title"><strong>${escapeHTML(item.displayName || item.name || item.packageId || item.id)}</strong><span class="pill">${escapeHTML(item.type || "package")}</span></div><p class="result-summary">${escapeHTML(item.description || t("暂无描述"))}</p>${renderPackageTags(item, 8)}</div><div class="result-version"><strong>${escapeHTML(item.version || item.latest || "unknown")}</strong><span>${escapeHTML(formatDate(item.updatedAt))}</span></div></article>`).join("") : `<p class="muted">${escapeHTML(t("暂无最近更新记录"))}</p>`;
  }
  window.addEventListener("forge:localechange", () => {
    if (!state.ready) return;
    if (state.dashboard) renderDashboard(state.dashboard);
    else if (state.agentDashboard) renderAgentDashboard(state.agentDashboard);
    else renderRoute(false);
  });
  ui.apply();
  if (page === "dashboard") startDashboard(); else if (page === "agent-dashboard") startAgentDashboard(); else startCatalog();
}());
