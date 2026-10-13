/* Pages app. Reads the read-only query API (/api/) served next to the static
   shell. No write token, no runtime service beyond that API is used. */
(function () {
  "use strict";

  const page = document.body.dataset.page || "catalog";
  const ui = window.ForgeUI;
  ui.addMessages({
    "zh-CN": { "media.load": "加载外链图片", "media.hide": "隐藏外链图片", "media.notice": "图片来自第三方，未核验。加载后会向图片站点发送请求；本页不发送 Referer。", "media.preview": "静态预览", "media.blocked": "尚未加载", "media.failed": "图片加载失败", "catalog.loading": "正在检索…", "catalog.error": "数据服务暂不可用，请稍后重试。", "detail.loading": "正在读取包详情…" },
    en: { "media.load": "Load external images", "media.hide": "Hide external images", "media.notice": "Unverified third-party images. Loading sends requests to image hosts, without a Referer from this page.", "media.preview": "Static previews", "media.blocked": "Not loaded", "media.failed": "Image unavailable", "catalog.loading": "Searching…", "catalog.error": "The data service is unavailable. Try again later.", "detail.loading": "Loading package detail…" }
  });
  const t = ui.t;
  const base = ui.siteBase;
  const apiBase = new URL("/api/", base);
  const DEFAULT_MEDIA = new URL("assets/img/mc2_crate.png", base).href;
  const types = ["mcp", "plugin", "skill", "general", "bundle"];
  const colors = { mcp: "#5f9d33", plugin: "#4876a7", skill: "#c87927", general: "#8667a9", bundle: "#8a7358" };
  const pageSize = 50;
  const state = {
    manifest: null, filtered: [], total: 0, page: 0, sort: "relevance",
    ready: false, catalogNodes: null, detailRoute: 0, mediaEnabled: true,
    dashboard: null, agentDashboard: null, abort: null, searchSig: "",
  };

  async function apiJSON(path, params, signal) {
    const url = new URL(path.replace(/^\//, ""), apiBase);
    if (params) for (const [key, value] of Object.entries(params)) { if (value !== "" && value != null) url.searchParams.set(key, value); }
    const response = await fetch(url.href, { headers: { Accept: "application/json" }, signal });
    if (!response.ok) throw new Error(`${response.status} ${url.pathname}`);
    return await response.json();
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
  function setText(id, value) {
    const node = document.getElementById(id);
    if (!node) return;
    node.removeAttribute("data-i18n");
    node.textContent = value;
  }
  function safeHref(value) {
    try {
      const parsed = new URL(String(value || ""), base);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
    } catch (_) { return "#"; }
  }

  /* ---------- media ---------- */
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
    const useReal = Boolean(url && state.mediaEnabled);
    const src = useReal ? url : DEFAULT_MEDIA;
    const cls = useReal ? "" : "media-default";
    const title = url && !state.mediaEnabled ? ' title="' + escapeHTML(t("media.blocked")) + '"' : '';
    return '<span class="media-slot media-' + kind + '"' + title + '><img class="' + cls + '" src="' + escapeHTML(src) + '" alt="' + escapeHTML(alt) + '" loading="lazy" decoding="async" referrerpolicy="no-referrer"></span>';
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
      const failed = () => {
        if (image.dataset.failed) return;
        image.dataset.failed = "1";
        image.src = DEFAULT_MEDIA;
        image.classList.add("media-default");
      };
      image.addEventListener('error', failed);
      if (image.complete && image.naturalWidth === 0) failed();
    });
  }

  /* ---------- tags ---------- */
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

  /* ---------- catalog ---------- */
  function currentFilters() {
    return {
      q: (document.getElementById("query") || {}).value?.trim() || "",
      agent: (document.getElementById("agent-filter") || {}).value || "",
      type: (document.getElementById("type-filter") || {}).value || "",
      subtype: (document.getElementById("subtype-filter") || {}).value || "",
      sort: (document.getElementById("sort-select") || {}).value || "relevance",
    };
  }
  async function runSearch(pageIndex) {
    const results = document.getElementById("results");
    if (!results) return;
    // Cancel any in-flight query so a slow earlier response cannot overwrite a
    // newer one (and so fast typing does not pile up requests).
    if (state.abort) state.abort.abort();
    const controller = new AbortController();
    state.abort = controller;
    const filters = currentFilters();
    state.page = Math.max(0, pageIndex || 0);
    state.sort = filters.sort;
    results.setAttribute("aria-busy", "true");
    setText("catalog-status", t("catalog.loading"));
    let payload;
    try {
      payload = await apiJSON("search", { ...filters, page: state.page, pageSize }, controller.signal);
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (controller !== state.abort) return;
      results.setAttribute("aria-busy", "false");
      results.innerHTML = '<p class="muted">' + escapeHTML(t("catalog.error")) + '</p>';
      setText("catalog-status", "");
      return;
    }
    if (controller !== state.abort) return;
    results.setAttribute("aria-busy", "false");
    state.filtered = payload.entries || [];
    state.total = payload.total || 0;
    renderResults();
  }
  function renderResults() {
    const results = document.getElementById("results");
    if (!results) return;
    const total = state.total;
    const start = state.page * pageSize;
    setText("result-count", number(total));
    setText("catalog-status", total ? t("catalog.records", { count: number(total) }) : t("catalog.empty"));
    if (state.manifest) setText("revision-stamp", (state.manifest.revision || t("未发布 revision")) + " · " + formatDate(state.manifest.generatedAt));
    const hasMedia = state.filtered.some((entry) => entry.media && (entry.media.icon || (Array.isArray(entry.media.previews) && entry.media.previews.length)));
    results.innerHTML = (hasMedia ? mediaControls() : '') + state.filtered.map((entry) => {
      const href = "#/package/" + [entry.agentId, entry.type, entry.routeName].map(encodeURIComponent).join("/");
      const media = entry.media || {};
      const firstPreview = Array.isArray(media.previews) && media.previews[0] && safeImageURL(media.previews[0].url) ? media.previews[0] : null;
      // Use the first preview as the card cover; otherwise the MC crate default.
      const cover = firstPreview ? imageSlot(firstPreview, "thumbnail", t("media.preview")) : '';
      const icon = firstPreview ? '' : imageSlot(media.icon, "icon", entry.type.toUpperCase().slice(0, 1));
      const title = '<div class="result-title">' + icon + '<strong>' + escapeHTML(entry.displayName || entry.name) + '</strong><span class="pill">' + escapeHTML(entry.type.toUpperCase()) + '</span>' + (entry.subtype ? '<span class="pill pill-neutral">' + escapeHTML(entry.subtype) + '</span>' : '') + '</div>';
      const summary = entry.description || entry.summary || t("暂无描述，打开详情查看来源与安装候选。");
      const versions = entry.versions.length ? t("catalog.versions", { count: number(entry.versions.length) }) : t("catalog.version", { version: entry.latest });
      return '<article class="result-card"><div><a class="result-link" href="' + escapeHTML(href) + '">' + cover + title + '<p class="result-summary">' + escapeHTML(summary) + '</p></a><div class="result-meta"><span>' + escapeHTML(entry.agentId) + '</span><span>' + escapeHTML(versions) + '</span></div>' + renderPackageTags(entry, 8) + '</div><div class="result-version"><strong>' + escapeHTML(entry.latest) + '</strong><span>' + escapeHTML(formatDate(entry.updatedAt)) + '</span></div></article>';
    }).join("");
    bindMedia(results);
    document.getElementById("empty-state").hidden = total > 0;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    document.getElementById("pagination").hidden = pages <= 1;
    document.getElementById("page-number").max = pages;
    document.getElementById("page-number").value = state.page + 1;
    setText("page-total", t("catalog.pages", { count: number(pages) }));
    setText("page-range", total ? number(start + 1) + "–" + number(Math.min(start + pageSize, total)) + " / " + number(total) : "");
    document.getElementById("previous-page").disabled = state.page === 0;
    document.getElementById("next-page").disabled = state.page + 1 >= pages;
  }

  /* ---------- detail ---------- */
  async function renderDetail(agent, type, name) {
    const route = ++state.detailRoute;
    const main = document.querySelector("main");
    if (!main) return;
    main.innerHTML = '<a class="back-link" href="#">' + escapeHTML(t("← 返回目录")) + '</a><p role="status">' + escapeHTML(t("detail.loading")) + '</p>';
    let record;
    try {
      record = await apiJSON("package", { agent, type, name });
    } catch (error) {
      record = null;
    }
    if (route !== state.detailRoute) return;
    if (!record || record.error) {
      main.innerHTML = '<a class="back-link" href="#">' + escapeHTML(t("← 返回目录")) + '</a><p role="status">' + escapeHTML(t("detail.missing")) + '</p>';
      return;
    }
    const details = record.details || record[type + "Details"] || {};
    const distributions = Array.isArray(record.distributions) ? record.distributions : [];
    const fact = (label, value) => '<div><dt>' + escapeHTML(t(label)) + '</dt><dd>' + escapeHTML(value) + '</dd></div>';
    const tags = renderPackageTags(record);
    const sources = distributions.map((item) => '<a class="distribution" href="' + escapeHTML(safeHref(item.url || item.href)) + '" target="_blank" rel="noreferrer"><strong>' + escapeHTML(item.name || item.type || t("发行来源")) + '</strong><span>' + escapeHTML(item.url || item.href || t("未提供地址")) + '</span></a>').join("") || '<p class="muted">' + escapeHTML(t("暂无发行来源。Agent Forge 不托管插件文件。")) + '</p>';
    const links = Object.entries(record.links || {}).filter(([, value]) => value).map(([key, value]) => '<a class="external-link" href="' + escapeHTML(safeHref(value)) + '" target="_blank" rel="noreferrer">' + escapeHTML(key) + ' ↗</a>').join("") || '<p class="muted">' + escapeHTML(t("暂无链接")) + '</p>';
    main.innerHTML = '<a class="back-link" href="#">' + escapeHTML(t("← 返回目录")) + '</a><section class="detail-heading"><div><p class="eyebrow">' + escapeHTML(type.toUpperCase()) + ' · ' + escapeHTML(agent) + '</p><h1>' + escapeHTML(record.displayName || record.name || name) + '</h1><p class="lede">' + escapeHTML(record.description || record.summary || t("暂无描述")) + '</p></div><span class="pill">' + escapeHTML(t(record.compatibilityStatus === "unknown" ? "兼容范围未知" : "元数据记录")) + '</span></section><div class="detail-layout"><article class="panel detail-main"><div class="detail-section"><h2>' + escapeHTML(t("包信息")) + '</h2><dl class="facts">' + fact("包名", record.name || name) + fact("Agent", agent) + fact("最新版本", record.version || record.latest) + fact("Subtype", record.subtype || t("未指定")) + fact("Agent 版本范围", record.agentVersionRange || t("未提供")) + '</dl></div>' + (tags ? '<div class="detail-section"><h2>' + escapeHTML(t("标签")) + '</h2>' + tags + '</div>' : '') + renderGallery(record) + '<div class="detail-section"><h2>' + escapeHTML(t("类型详情")) + '</h2><pre class="code-block">' + escapeHTML(JSON.stringify(details, null, 2)) + '</pre></div></article><aside class="panel detail-side"><div class="detail-section"><h2>' + escapeHTML(t("安装候选")) + '</h2>' + sources + '</div><div class="detail-section"><h2>' + escapeHTML(t("文档链接")) + '</h2>' + links + '</div></aside></div>';
    bindMedia(main);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function renderRoute() {
    const main = document.querySelector("main");
    if (location.hash.startsWith("#/package/")) {
      try {
        const parts = location.hash.slice("#/package/".length).split("/");
        return renderDetail(decodeURIComponent(parts[0]), decodeURIComponent(parts[1]), decodeURIComponent(parts.slice(2).join("/")));
      } catch (_) {
        return renderDetail("", "", "");
      }
    }
    ++state.detailRoute;
    if (state.catalogNodes && !document.getElementById("query")) main.replaceChildren(...state.catalogNodes);
    ui.apply(main);
    renderResults();
  }

  function setupFilters() {
    const agents = (state.manifest && state.manifest.agents) || [];
    const subtypes = (state.manifest && state.manifest.subtypes) || [];
    const agentSelect = document.getElementById("agent-filter");
    const subtypeSelect = document.getElementById("subtype-filter");
    if (agentSelect && agentSelect.options.length <= 1) agentSelect.insertAdjacentHTML("beforeend", agents.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    if (subtypeSelect && subtypeSelect.options.length <= 1) subtypeSelect.insertAdjacentHTML("beforeend", subtypes.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join(""));
    let searchTimer;
    const scheduleSearch = (delay) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        const signature = JSON.stringify(currentFilters());
        if (signature === state.searchSig) return;
        state.searchSig = signature;
        runSearch(0);
      }, delay);
    };
    document.getElementById("query").addEventListener("input", () => scheduleSearch(300));
    ["agent-filter", "type-filter", "subtype-filter", "sort-select"].forEach((id) => document.getElementById(id).addEventListener("change", () => scheduleSearch(0)));
    document.getElementById("clear-filters").addEventListener("click", () => { ["query", "agent-filter", "type-filter", "subtype-filter"].forEach((id) => { document.getElementById(id).value = ""; }); scheduleSearch(0); });
    const changePage = (pageNumber) => {
      if (!Number.isFinite(pageNumber)) pageNumber = 0;
      const pages = Math.max(1, Math.ceil(state.total / pageSize));
      state.page = Math.max(0, Math.min(pages - 1, Math.floor(pageNumber)));
      runSearch(state.page);
      document.querySelector(".catalog-toolbar").scrollIntoView({ block: "start" });
    };
    document.getElementById("previous-page").addEventListener("click", () => changePage(state.page - 1));
    document.getElementById("next-page").addEventListener("click", () => changePage(state.page + 1));
    document.getElementById("page-number").addEventListener("change", (event) => changePage(Number(event.target.value) - 1));
    window.addEventListener("keydown", (event) => { if (event.key === "/" && !event.ctrlKey && !event.altKey && !event.metaKey && !["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(document.activeElement.tagName) && !document.activeElement.isContentEditable) { event.preventDefault(); document.getElementById("query").focus(); } });
  }

  async function startCatalog() {
    try {
      state.manifest = await apiJSON("manifest");
    } catch (_) {
      state.manifest = {};
    }
    setupFilters();
    const params = new URLSearchParams(location.search);
    [["query", "q"], ["agent-filter", "agent"], ["type-filter", "type"], ["subtype-filter", "subtype"], ["sort-select", "sort"]]
      .forEach(([id, key]) => { const value = params.get(key); const el = document.getElementById(id); if (value != null && el) el.value = value; });
    setupDone();
    state.catalogNodes = Array.from(document.querySelector("main").childNodes);
    state.ready = true;
    window.addEventListener("hashchange", () => renderRoute());
    await runSearch(0);
  }
  function setupDone() {
    if (state.manifest && state.manifest.revision) setText("revision-stamp", state.manifest.revision + " · " + formatDate(state.manifest.generatedAt));
  }

  /* ---------- dashboard ---------- */
  function metric(label, value, detail) { return `<div class="metric"><div class="metric-label">${escapeHTML(label)}</div><div class="metric-value">${escapeHTML(number(value))}</div>${detail ? `<div class="metric-detail">${escapeHTML(detail)}</div>` : ""}</div>`; }
  function getTypes(agent) { return agent.types || agent.typeCounts || {}; }
  function normalizeDashboard(raw) {
    if (!raw || typeof raw !== "object") return { totals: {}, agents: [], types: {}, facets: {}, compatibility: { known: 0, unknown: 0 } };
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
    let raw = null;
    try { raw = await apiJSON("dashboard"); } catch (_) { raw = null; }
    const dashboard = normalizeDashboard(raw);
    state.dashboard = dashboard;
    state.ready = true;
    renderDashboard(dashboard);
  }
  async function startAgentDashboard() {
    const id = new URLSearchParams(location.search).get("id") || "dsh";
    let global = {}, raw = null;
    try { global = await apiJSON("dashboard"); } catch (_) { global = {}; }
    try { raw = await apiJSON("agent-dashboard", { id }); } catch (_) { raw = null; }
    const globalDashboard = normalizeDashboard(global);
    const agent = raw ? normalizeDashboard(raw) : globalDashboard.agents.find((item) => (item.id || item.name) === id) || { id, name: id, types: {}, facets: {}, packages: 0, versions: 0 };
    state.agentDashboard = { agent, global: globalDashboard, id };
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
    else if (document.getElementById("results")) runSearch(state.page);
    else renderRoute();
  });
  ui.apply();
  if (page === "dashboard") startDashboard();
  else if (page === "agent-dashboard") startAgentDashboard();
  else startCatalog();
}());
