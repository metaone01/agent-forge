/* Public dashboard: one revision, declared metadata, and explicit retry/refresh. */
(function () {
  "use strict";
  window.ForgeApiDashboard = { start };
  function start({ ui, base, escapeHTML: e, formatDate, number, metric, barRows }) {
    ui.addMessages({
      "zh-CN": { "dash.loading": "正在读取公开快照…", "dash.error": "查询服务暂时不可用，已有统计保留。", "dash.invalid": "Dashboard 链接参数无效。", "dash.missing": "该 revision 中没有此 Agent。", "dash.expired": "该快照已回收，请读取最新统计。", "dash.retry": "重试", "dash.refresh": "读取最新统计", "dash.catalog": "查询目录", "dash.title": "公开目录统计", "dash.description": "统计与目录查询使用同一公开快照。", "dash.notice": "全站按包 ID 去重；各 Agent 的声明关联可以重叠，不能直接相加。", "dash.versions": "关联包的全部规范版本", "dash.bundles": "独立 Bundle 记录", "dash.unique": "按规范 ID 去重", "dash.declared": "提交者声明的 Agent 关联", "dash.facets": "规范 facet 声明", "dash.custom": "自定义 facet 声明", "dash.facetNote": "每栏最多显示 20 个标签；声明不表示后端认证。", "dash.recent": "最近规范更新", "dash.recentNote": "仅按规范更新时间排列；不是上游创建或发行时间。", "dash.compat": "兼容性按包与 Agent 的声明关联计数；unknown 不表示不兼容。", "dash.footer": "Agent Forge · 公开快照统计", "dash.empty": "该快照中暂无统计", "dash.agent": "Agent：{id}", "dash.coverage": "上游指标覆盖不完整或已过期；本页目录数量只描述此公开快照。" },
      en: { "dash.loading": "Reading the public snapshot…", "dash.error": "The query service is unavailable. Previous statistics are retained.", "dash.invalid": "Invalid dashboard link parameters.", "dash.missing": "This Agent is absent from this revision.", "dash.expired": "This snapshot was retired. Read the latest statistics.", "dash.retry": "Retry", "dash.refresh": "Read latest statistics", "dash.catalog": "Query catalog", "dash.title": "Public catalog statistics", "dash.description": "Statistics and catalog queries use the same public snapshot.", "dash.notice": "Global packages are deduplicated by ID. Agent associations can overlap and must not be added together.", "dash.versions": "All canonical versions of associated packages", "dash.bundles": "Distinct Bundle records", "dash.unique": "Distinct canonical IDs", "dash.declared": "Submitter-declared Agent associations", "dash.facets": "Canonical facet declarations", "dash.custom": "Custom facet declarations", "dash.facetNote": "At most 20 labels per column. Declarations are not backend certification.", "dash.recent": "Recent canonical updates", "dash.recentNote": "Ordered by canonical metadata update time, not upstream creation or release time.", "dash.compat": "Compatibility counts package–Agent declarations. Unknown does not mean incompatible.", "dash.footer": "Agent Forge · Public snapshot statistics", "dash.empty": "No statistics in this snapshot", "dash.agent": "Agent: {id}", "dash.coverage": "Upstream metrics have partial or stale coverage. Catalog counts describe only this public snapshot." }
    });
    const t = ui.t, isAgent = document.body.dataset.page === "api-agent-dashboard";
    const params = new URLSearchParams(location.search), allowed = isAgent ? ["id", "revision"] : ["revision"];
    const agent = isAgent ? params.get("id") : null;
    let invalid = [...params.keys()].some((key) => !allowed.includes(key) || params.getAll(key).length !== 1) || (isAgent && !agent) || (params.has("revision") && !params.get("revision"));
    const config = document.querySelector('meta[name="agent-forge-api-base"]');
    const client = new window.ForgeQuery.Client(config ? new URL(config.content, document.baseURI).href : new URL("api/v1/", base).href);
    let data = null, error = null, busy = false, generation = 0, controller = null, requestedRevision = params.get("revision");
    const node = (id) => document.getElementById(id);
    function text(id, value) { const item = node(id); if (item) { item.removeAttribute("data-i18n"); item.textContent = value; } }
    function link(route, revision, id) { const url = new URL(route, base); if (revision) url.searchParams.set("revision", revision); if (id) url.searchParams.set("id", id); return url.href; }
    function facetRows(items) { return Object.fromEntries(items.map((item) => [item.kind + " / " + item.value, item.count])); }
    function bars(id, values) { node(id).innerHTML = Object.keys(values).length ? barRows(values, 20) : '<p class="muted">' + e(t("dash.empty")) + '</p>'; }
    function draw() {
      ui.apply();
      node("dashboard-refresh").disabled = busy || invalid; node("dashboard-retry").disabled = busy;
      node("dashboard-error").hidden = !error;
      node("dashboard-retry").hidden = invalid || (error && error.code === "agent_not_found");
      text("dashboard-error-message", error ? t(["invalid_query", "invalid_revision"].includes(error.code) ? "dash.invalid" : error.code === "agent_not_found" ? "dash.missing" : ["revision_unavailable", "cursor_expired"].includes(error.code) ? "dash.expired" : "dash.error") : "");
      text("dashboard-status", busy ? t("dash.loading") : "");
      if (!data) return;
      text("dashboard-updated", data.revision + " · " + formatDate(data.generatedAt));
      text("dashboard-coverage", data.freshnessSummary && data.freshnessSummary.stale ? t("dash.coverage") : "");
      node("dashboard-catalog").href = link("catalog/", data.revision);
      if (isAgent) { node("dashboard-catalog").href += "&agent=" + encodeURIComponent(agent); node("dashboard-back").href = link("catalog/dashboard/", data.revision); text("agent-title", agent); text("agent-description", t("dash.agent", { id: agent })); }
      const totals = data.totals;
      node("global-metrics").innerHTML = (isAgent ? [metric(t("包"), totals.packages, t("dash.unique")), metric(t("版本"), totals.versions, t("dash.versions")), metric("Bundle", totals.bundles, t("dash.bundles")), metric(t("范围未知"), data.compatibility.unknown, t("dash.compat"))] : [metric("Agent", totals.agents, t("dash.declared")), metric(t("包"), totals.packages, t("dash.unique")), metric(t("版本"), totals.versions, t("dash.versions")), metric("Bundle", totals.bundles, t("dash.bundles"))]).join("");
      bars(isAgent ? "agent-type-chart" : "type-bars", data.types);
      bars(isAgent ? "agent-facet-chart" : "facet-chart", facetRows(data.facets)); bars("custom-facet-chart", facetRows(data.customFacets));
      if (!isAgent) {
        const values = Object.fromEntries(data.agents.map((item) => [item.id, item.packageCount])); bars("agent-chart", values);
        node("agent-table").innerHTML = '<table><caption>' + e(t("Agent 包数量")) + '</caption><tbody>' + data.agents.map((item) => '<tr><th>' + e(item.id) + '</th><td>' + e(number(item.packageCount)) + '</td></tr>').join("") + '</tbody></table>';
        node("agent-cards").innerHTML = data.agents.length ? data.agents.map((item) => '<a class="agent-card" href="' + e(link("catalog/dashboard/agent/", data.revision, item.id)) + '"><div class="agent-card-title">' + e(item.id) + '</div><div class="agent-card-stats"><span>' + e(t("agent.packages", { count: number(item.packageCount) })) + '</span><span>' + e(t("agent.types", { count: number(Object.values(item.types).filter(Boolean).length) })) + '</span></div></a>').join("") : '<p class="muted">' + e(t("dash.empty")) + '</p>';
      }
      const compatibility = data.compatibility, compatTotal = Math.max(1, compatibility.known + compatibility.unknown);
      node("compat-chart").innerHTML = [["已声明范围", compatibility.known], ["范围未知", compatibility.unknown]].map(([label,count]) => '<div class="compat-item"><span>' + e(t(label)) + '</span><div class="bar-track"><div class="bar-fill" style="width:' + count / compatTotal * 100 + '%"></div></div><span class="bar-value">' + e(number(count)) + '</span></div>').join("");
      node("recent-packages").innerHTML = data.recentPackages.length ? data.recentPackages.map((item) => {
        const href = link("catalog/", data.revision) + "#/id/" + encodeURIComponent(item.id) + "?revision=" + encodeURIComponent(data.revision);
        return '<article class="result-card"><div><a class="result-link" href="' + e(href) + '"><div class="result-title"><strong>' + e(item.displayName || item.name) + '</strong><span class="pill">' + e(item.type.toUpperCase()) + '</span></div><p class="result-summary">' + e(item.summary) + '</p></a></div><div class="result-version"><strong>' + e(item.canonicalLatest || t("未知")) + '</strong><span>' + e(formatDate(item.canonicalUpdatedAt)) + '</span></div></article>';
      }).join("") : '<p class="muted">' + e(t("dash.empty")) + '</p>';
    }
    async function load(revision) {
      const current = ++generation; if (controller) controller.abort(); controller = new AbortController();
      requestedRevision = revision; busy = true; error = null; draw();
      try {
        if (invalid) throw new window.ForgeQuery.ApiError("invalid_query");
        const response = await client.dashboard({ agent, revision, signal: controller.signal });
        if (current !== generation) return;
        data = response;
        const url = new URL(location.href); url.searchParams.set("revision", response.revision); history.replaceState(null, "", url.href);
      } catch (failure) { if (current === generation && failure.name !== "AbortError") error = failure; }
      finally { if (current === generation) { busy = false; draw(); } }
    }
    node("dashboard-retry").addEventListener("click", () => load(requestedRevision));
    node("dashboard-refresh").addEventListener("click", () => load(null));
    window.addEventListener("forge:localechange", draw);
    window.addEventListener("pagehide", () => { ++generation; if (controller) controller.abort(); });
    load(requestedRevision);
    return { client };
  }
})();
