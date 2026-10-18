/* The API catalog shares the existing cards, tag rendering, media consent and preferences. */
(function () {
  "use strict";
  window.ForgeApiCatalog = { start };
  function start(shared) {
    const { ui, base, escapeHTML: e, formatDate, number, imageSlot, coverSlot, mediaControls, renderPackageTags, renderGallery, bindMedia, safeHref } = shared;
    ui.addMessages({
      "zh-CN": { "api.loading": "正在查询…", "api.ready": "已读取这一页", "api.partial": "部分来源未采集或已过期", "api.page": "第 {page} 页", "api.loaded": "本页 {count} 个", "api.retry": "重试", "api.refresh": "读取最新目录", "api.error": "查询失败，保留上次结果。", "api.expired": "这一版目录已回收，请读取最新目录。", "api.unknown": "未知", "api.tags": "标签筛选（可多选）", "api.controlled": "规范标签", "api.custom": "自定义标签", "api.source": "指标来源与口径", "api.stars": "所属仓库 Star", "api.starRate": "仓库 Star 净增 / 小时", "api.downloads": "npm 近 30 日下载次数", "api.downloadRate": "npm 完整日下载 / 小时", "api.coverage": "下载次数不是人数；仓库 Star 可能由多个插件共享。", "api.interval": "实际区间", "api.selected": "当前展示版本", "api.latest": "规范最新版本", "api.versions": "规范版本", "api.more": "加载更多版本", "api.noCanonical": "这一版没有可明确选择的规范记录。", "api.detailError": "详情暂不可用", "api.relevance": "相关性排序需要搜索词。", "api.filterError": "筛选条件无效，请减少选项或修改搜索词。", "api.sourceStatus": "来源状态", "api.fresh": "已采集", "api.stale": "缺失或过期", "api.noVersions": "没有已公开的规范版本", "api.loadedLabel": "本页记录", "api.sort.canonical": "最近规范更新", "api.sort.discovered": "最近收录", "api.sort.stars": "所属仓库 Star", "api.sort.starGrowth": "Star 增长热门", "api.sort.downloads": "npm 下载最多", "api.sort.downloadGrowth": "npm 下载热门", "api.sort.pushed": "最近代码推送", "api.description": "按 Agent、类型、标签和来源指标查询公开元数据。包提供安装地址，目录不代理插件文件。", "api.metricVersion": "指标按规范最新版本 {version} 的来源关联展示，不代表所选历史版本的独立指标。", "api.starPolicy": "Star 热门按实际采样间隔的小时净增长排序，符合窗口的记录优先。", "api.downloadPolicy": "npm 下载热门采用最后一个完整 UTC 日的小时平均下载次数；不是滚动 30 日总量的差值。", "api.qualified": "满足该快照的榜单窗口", "api.unqualified": "未知或不满足该快照榜单窗口" },
      en: { "api.loading": "Searching…", "api.ready": "Page loaded", "api.partial": "Some sources are missing or stale", "api.page": "Page {page}", "api.loaded": "{count} on this page", "api.retry": "Retry", "api.refresh": "Read latest catalog", "api.error": "Query failed. Previous results are retained.", "api.expired": "This revision was retired. Read the latest catalog.", "api.unknown": "Unknown", "api.tags": "Filter tags (multiple selection)", "api.controlled": "Declared facets", "api.custom": "Custom facets", "api.source": "Metric source and scope", "api.stars": "Repository stars", "api.starRate": "Repository star net growth / hour", "api.downloads": "npm downloads in 30 days", "api.downloadRate": "npm complete-day downloads / hour", "api.coverage": "Downloads are not people. Repository stars may be shared by several plugins.", "api.interval": "Actual interval", "api.selected": "Displayed version", "api.latest": "Canonical latest", "api.versions": "Canonical versions", "api.more": "Load more versions", "api.noCanonical": "No canonical record can be selected for this revision.", "api.detailError": "Details are unavailable", "api.relevance": "Relevance sorting requires a search term.", "api.filterError": "Invalid filters. Select fewer tags or revise the search.", "api.sourceStatus": "Source status", "api.fresh": "Collected", "api.stale": "Missing or stale", "api.noVersions": "No published canonical versions", "api.loadedLabel": "on this page", "api.sort.canonical": "Canonical update", "api.sort.discovered": "Recently indexed", "api.sort.stars": "Repository stars", "api.sort.starGrowth": "Star growth", "api.sort.downloads": "Most npm downloads", "api.sort.downloadGrowth": "npm download rate", "api.sort.pushed": "Code pushed", "api.description": "Search public metadata by Agent, type, facets and source metrics. Packages provide installation URLs; the catalog does not proxy plugin files.", "api.metricVersion": "Metrics follow the sources associated with canonical latest {version}; they are not independent metrics for the selected historical version.", "api.starPolicy": "Star growth uses net change per actual sampling hour. Qualified windows rank first.", "api.downloadPolicy": "npm download rate is the hourly average of the last complete UTC day, not a difference between rolling 30-day totals.", "api.qualified": "Qualified in this snapshot", "api.unqualified": "Unknown or outside this snapshot window" }
    });
    ui.addMessages({"zh-CN": {"api.scope": "查询范围", "api.scope.catalog": "已收录目录", "api.scope.discovery": "发现候选", "api.candidateNotice": "候选尚未正式收录。仓库 Topics 不代表插件类型或 Agent 兼容性；声明合格仍需审核。", "api.candidateCoverage": "发现范围可能不完整；首次发现时间不是上游创建时间。", "api.repositoryCandidate": "候选仓库", "api.manifestCandidate": "声明候选", "api.review.unreviewed": "仅发现仓库", "api.review.needs_metadata": "待补元数据", "api.review.identity_conflict": "身份冲突，待核对", "api.review.review_pending": "待审核", "api.firstSeen": "首次发现", "api.lastSeen": "最后观察", "api.sort.firstSeen": "最近发现", "api.declaredVersion": "声明版本", "api.declaredLicense": "声明许可证", "api.declaredName": "声明名称", "api.declaredDescription": "声明描述", "api.agentAssociation": "声明检查的 Agent 关联", "api.noCompatibility": "尚未核验兼容性", "api.publicRepository": "公开仓库", "api.sourceEvidence": "来源证据", "api.sourceCount": "已展示 {shown} / 共 {total} 个来源", "api.reviewStatus": "候选状态", "api.declaration": "声明信息", "api.issues": "待补信息与校验问题", "api.noIssues": "声明字段已通过当前检查，仍需审核。", "api.timeline": "发现记录", "api.metadata_found": "已找到声明", "api.normalized": "已整理声明字段", "api.schema_valid": "声明格式合格", "api.discovered": "已发现", "api.repositoryStars": "仓库 Star（由仓库共享）", "api.filterOptionsError": "筛选选项暂不可用，已读取的结果保留。", "api.discoveryUnavailable": "这一版没有候选数据，可以切回已收录目录。", "api.issue.missing_or_invalid_declaration": "缺少或无效的声明字段", "api.issue.canonical_identity_conflict": "与已收录身份冲突", "api.issue.upstream_profile_invalid": "上游声明格式不符合要求", "api.issue.skill_directory_name_mismatch": "Skill 名称与目录不一致", "api.issue.schema_invalid": "声明格式校验问题", "api.issue.identity_invalid": "身份字段无效"}, "en": {"api.scope": "Search scope", "api.scope.catalog": "Indexed catalog", "api.scope.discovery": "Discovered candidates", "api.candidateNotice": "Candidates have not been admitted to the catalog. Repository topics do not establish package type or Agent compatibility. Valid declarations still need review.", "api.candidateCoverage": "Discovery coverage may be incomplete. First seen is not the upstream creation time.", "api.repositoryCandidate": "Repository candidate", "api.manifestCandidate": "Declaration candidate", "api.review.unreviewed": "Repository discovered", "api.review.needs_metadata": "Metadata needed", "api.review.identity_conflict": "Identity conflict", "api.review.review_pending": "Review pending", "api.firstSeen": "First seen", "api.lastSeen": "Last observed", "api.sort.firstSeen": "Recently discovered", "api.declaredVersion": "Declared version", "api.declaredLicense": "Declared license", "api.declaredName": "Declared name", "api.declaredDescription": "Declared description", "api.agentAssociation": "Agent associated with the declaration check", "api.noCompatibility": "Compatibility has not been verified", "api.publicRepository": "Public repository", "api.sourceEvidence": "Source evidence", "api.sourceCount": "Showing {shown} of {total} sources", "api.reviewStatus": "Candidate status", "api.declaration": "Declared metadata", "api.issues": "Missing metadata and validation issues", "api.noIssues": "Declared fields passed the current checks. Review is still required.", "api.timeline": "Discovery history", "api.metadata_found": "Declaration found", "api.normalized": "Declared fields normalized", "api.schema_valid": "Declaration format valid", "api.discovered": "Discovered", "api.repositoryStars": "Repository stars (shared)", "api.filterOptionsError": "Filter options are unavailable. Loaded results are retained.", "api.discoveryUnavailable": "This revision has no candidate data. You can switch to the indexed catalog.", "api.issue.missing_or_invalid_declaration": "Missing or invalid declared field", "api.issue.canonical_identity_conflict": "Conflicts with an indexed identity", "api.issue.upstream_profile_invalid": "Unsupported declaration format", "api.issue.skill_directory_name_mismatch": "Skill name differs from its directory", "api.issue.schema_invalid": "Declaration validation issue", "api.issue.identity_invalid": "Invalid identity fields"}});
    ui.apply();
    const t = ui.t;
    ui.addMessages({
      "zh-CN": {"sourceFilter.label":"精选来源关联","sourceFilter.all":"不按来源筛选","sourceFilter.more":"加载更多来源","sourceFilter.loading":"正在读取此版本的精选来源…","sourceFilter.error":"来源选项读取失败，可独立重试；目录结果保留。","sourceFilter.unavailable":"此版本尚不支持按精选来源筛选。","sourceFilter.unknownOption":"尚未加载的指定来源","sourceFilter.notice":"来源：{source}。仅筛选规范最新版本的精确声明链接与仍在来源列表中的成员关联；不推断插件背书或质量。原始排名未知，排序使用当前明确选项。","sourceFilter.stale":"来源检查已过期，保留最后完整事实。"},
      en: {"sourceFilter.label":"Curated source association","sourceFilter.all":"No source filter","sourceFilter.more":"Load more sources","sourceFilter.loading":"Loading curated sources for this revision…","sourceFilter.error":"Source options failed to load. Retry independently; catalog results are retained.","sourceFilter.unavailable":"This revision has no curated-source filtering feature.","sourceFilter.unknownOption":"Specified source not loaded yet","sourceFilter.notice":"Source: {source}. Matches only canonical latest versions with exact declared links to active list members; no inferred package endorsement or quality. Upstream ranks are unknown; the selected sort applies.","sourceFilter.stale":"Source checks are stale; the last complete facts are retained."}
    });
    ui.addMessages({
      "zh-CN":{"review.title":"元数据分拣记录","review.filter":"元数据分拣状态","review.all":"全部记录","review.not_recorded":"未记录","review.rejected":"运行者拒绝收录","review.unsupported":"当前目录不支持","review.needs_metadata":"运行者要求补充元数据","review.pending":"重新待审","review.superseded":"元数据已变化，旧记录待重审","review.notice":"这是本地运行者记录的元数据收录意见，不代表质量、安全或兼容核验，也没有批准正式收录。","review.fingerprint":"当前元数据视图指纹","review.history":"操作记录","review.matches":"适用于当前元数据视图","review.changed":"旧记录不适用于当前元数据视图","review.truncated":"展示最近 {shown} / 共 {total} 条记录","review.unavailable":"此版本未提供元数据分拣记录，不能据此判断尚无人审核。","review.reason.outside_catalog_scope":"不在当前目录收录范围","review.reason.unsupported_catalog_metadata":"当前目录不支持该元数据","review.reason.additional_metadata_required":"需要补充元数据","review.reason.reconsider_metadata":"重新考虑元数据收录意见"},
      en:{"review.title":"Metadata triage records","review.filter":"Metadata triage status","review.all":"All records","review.not_recorded":"No record","review.rejected":"Operator declined catalog admission","review.unsupported":"Unsupported by this catalog","review.needs_metadata":"Operator requested more metadata","review.pending":"Reopened for triage","review.superseded":"Metadata changed; prior record needs reconsideration","review.notice":"These are local operator records about metadata admission. They do not verify quality, safety or compatibility, and do not authorize canonical adoption.","review.fingerprint":"Current metadata view fingerprint","review.history":"Recorded actions","review.matches":"Applies to the current metadata view","review.changed":"The prior record does not apply to the current metadata view","review.truncated":"Showing the latest {shown} of {total} records","review.unavailable":"This revision has no metadata triage projection. It does not establish that no review has happened.","review.reason.outside_catalog_scope":"Outside the current catalog scope","review.reason.unsupported_catalog_metadata":"Metadata unsupported by this catalog","review.reason.additional_metadata_required":"Additional metadata required","review.reason.reconsider_metadata":"Reconsidered metadata admission"}
    });
    const config = document.querySelector('meta[name="agent-forge-api-base"]');
    const client = new window.ForgeQuery.Client(config ? new URL(config.content, document.baseURI).href : new URL("api/v1/", base).href);
    const main = document.querySelector("main");
    const collectionControls = window.ForgeSourceControls.create({client,ui,escapeHTML:e,formatDate,host:document.getElementById("source-control-panel")});
    ui.addMessages({"zh-CN":{
      "api.sort.release":"最新声明发行","api.releasePolicy":"按当前规范版本明确声明关联的发行时间排序；预发行参与，未来时间排除，未知在后。多条关联取最新时间，不代表完整发行覆盖或安装验证。",
      "api.releaseDate":"声明发行时间（规范版本 {version}）","api.releasePre":"预发行","api.releaseUnknown":"未知：未找到明确的有效发行关联","api.releaseFuture":"未知：上游发行时间晚于快照","api.releaseUnavailable":"这一版不支持声明发行排序，可选择其他排序或读取最新目录。"
    },"en":{
      "api.sort.release":"Latest declared release","api.releasePolicy":"Orders explicit release associations of the canonical latest version by publication time. Prereleases are included, future times excluded, unknowns last. Uses the latest of multiple associations; coverage and installation are not verified.",
      "api.releaseDate":"Declared release time (canonical {version})","api.releasePre":"Prerelease","api.releaseUnknown":"Unknown: no explicit eligible release association","api.releaseFuture":"Unknown: source publication time is after this snapshot","api.releaseUnavailable":"This revision cannot sort declared releases. Choose another sort or load the latest catalog."
    }});
    const nodes = Array.from(main.childNodes);
    const releases=window.ForgeReleases.create({client,ui,escapeHTML:e,formatDate});
    const recommendations = window.ForgeRecommendations.create({client,ui,escapeHTML:e,formatDate,main});
    const controls = {};
    for (const id of ["query", "scope-filter", "agent-filter", "type-filter", "subtype-filter", "sort-select", "facet-filter", "recommendation-source-filter", "review-disposition-filter"]) controls[id] = document.getElementById(id);
    let timer, detailGeneration = 0, detailAbort = null, detail = null, versionRows = [], versionCursor = null, versionError = null, versionBusy = false;
    let metadata = null, metadataGeneration = 0, metadataAbort = null, metadataRequest = null, metadataError = null, formGeneration = 0;
    const sourceControls = {status:document.getElementById("source-filter-status"),retry:document.getElementById("retry-source-options"),more:document.getElementById("more-source-options"),actions:document.querySelector(".source-filter-actions")};
    let reviewsAvailable = false, releaseSortAvailable = false;
    let sourceOptions = null, sourceGeneration = 0, sourceAbort = null;
    const session = new window.ForgeQuery.CatalogSession(client, { onChange: drawList });
    function text(id, value) { const node = document.getElementById(id); if (node) { node.removeAttribute("data-i18n"); node.textContent = value; } }
    function errorText(error) {
      if (error && ["cursor_expired", "revision_unavailable"].includes(error.code)) return t("api.expired");
      if (error && error.code === "sort_unavailable" && controls["sort-select"].value === "latest_release") return t("api.releaseUnavailable");
      if (error && error.code === "reviews_unavailable") return t("review.unavailable");
      if (error && error.code === "recommendations_unavailable") return t("sourceFilter.unavailable");
      if (error && error.code === "discovery_unavailable") return t("api.discoveryUnavailable");
      if (error && error.code === "relevance_requires_query") return t("api.relevance");
      if (error && ["invalid_query", "invalid_revision"].includes(error.code)) return t("api.filterError");
      return t("api.error");
    }
    function drawMetadataStatus() {
      text("filter-status", metadataError ? t("api.filterOptionsError") : "");
      const retry = document.getElementById("retry-filter-options"); if (retry) retry.hidden = !metadataError;
    }
    function filters() {
      const value = { scope: controls["scope-filter"].value, q: controls.query.value, agent: controls["agent-filter"].value, type: controls["type-filter"].value,
        subtype: controls["subtype-filter"].value, recommendationSource: controls["recommendation-source-filter"].value || undefined, reviewDisposition:controls["review-disposition-filter"].value||undefined, sort: controls["sort-select"].value, facets: Object.create(null), customFacets: Object.create(null) };
      for (const option of controls["facet-filter"].selectedOptions) {
        const [custom, group, facet] = JSON.parse(option.value), target = custom ? value.customFacets : value.facets;
        (target[group] || (target[group] = [])).push(facet);
      }
      return value;
    }
    function isDetailRoute() { return location.hash.startsWith("#/id/") || location.hash.startsWith("#/discovery/") || location.hash.startsWith("#/recommendations"); }
    function link(id, revision, version, scope = "catalog") {
      const params = new URLSearchParams();
      if (revision) params.set("revision", revision);
      if (version) params.set("version", version);
      return (scope === "discovery" ? "#/discovery/" : "#/id/") + encodeURIComponent(id) + (params.size ? "?" + params : "");
    }
    function routeInfo() {
      if (!isDetailRoute()) return null;
      if (location.hash.startsWith("#/recommendations")) {
        const raw=location.hash.slice("#/recommendations".length),index=raw.indexOf("?"),path=index<0?raw:raw.slice(0,index);
        const params=new URLSearchParams(index<0?"":raw.slice(index+1));
        if (path && !path.startsWith("/") || [...params.keys()].some(key=>!["revision","status"].includes(key)||params.getAll(key).length!==1||!params.get(key))) throw new window.ForgeQuery.ApiError("invalid_query");
        return {scope:"recommendations",source:path?decodeURIComponent(path.slice(1)):null,revision:params.get("revision"),status:params.get("status")||"all"};
      }
      const scope = location.hash.startsWith("#/discovery/") ? "discovery" : "catalog";
      const raw = location.hash.slice(scope === "discovery" ? "#/discovery/".length : "#/id/".length), index = raw.indexOf("?");
      const params = new URLSearchParams(index < 0 ? "" : raw.slice(index + 1));
      if ([...params.keys()].some((key) => !["revision", "version"].includes(key) || params.getAll(key).length !== 1 || !params.get(key))) throw new window.ForgeQuery.ApiError("invalid_query");
      if (scope === "discovery" && params.has("version")) throw new window.ForgeQuery.ApiError("invalid_query");
      return { scope, id: decodeURIComponent(index < 0 ? raw : raw.slice(0, index)), revision: params.get("revision"), version: params.get("version") };
    }
    function applyScope(resetSort = false) {
      const discovery = controls["scope-filter"].value === "discovery";
      for (const option of controls["sort-select"].options) {
        option.disabled = discovery && !["first_seen", "name", "relevance"].includes(option.value) || option.value === "latest_release" && !releaseSortAvailable;
        option.hidden = discovery && option.disabled;
        if (option.value === "first_seen") { option.dataset.i18n = discovery ? "api.sort.firstSeen" : "api.sort.discovered"; option.textContent = t(option.dataset.i18n); }
      }
      if (resetSort) controls["sort-select"].value = discovery ? "first_seen" : "canonical_updated";
      for (const option of controls["type-filter"].options) { option.disabled = discovery && !["", "plugin", "skill"].includes(option.value); option.hidden = option.disabled; }
      controls["review-disposition-filter"].closest("label").hidden = !discovery;
      controls["review-disposition-filter"].disabled = !reviewsAvailable;
      text("review-filter-status",discovery&&!reviewsAvailable?t("review.unavailable"):"");
      controls["recommendation-source-filter"].closest("label").hidden = discovery;
      sourceControls.actions.hidden = discovery;
      controls["subtype-filter"].closest("label").hidden = discovery;
      controls["facet-filter"].closest("label").hidden = discovery;
      controls["subtype-filter"].disabled = discovery; controls["facet-filter"].disabled = discovery;
      const notice = document.getElementById("scope-notice");
      if (notice) { notice.hidden = !discovery; notice.textContent = t("api.candidateNotice"); }
      const metric = document.getElementById("metric-notice"); if (metric) metric.hidden = discovery;
    }
    function discoveryTags(item) {
      return (Array.isArray(item.keywords) ? item.keywords : []).slice(0, 8).map((tag) => '<span class="pill">' + e(tag) + '</span>').join(" ");
    }
    function discoveryCard(item, revision) {
      const status = t("api.review." + item.reviewStatus);
      const agents = item.agents.length ? item.agents.join(" · ") : t("api.unknown");
      const declaredVersion = item.declaredMetadata && item.declaredMetadata.version;
      const observed = '<span>' + e(t("api.lastSeen")) + ': ' + e(formatDate(item.lastSeenAt)) + '</span>';
      const summary = item.summary || item.repository.fullName;
      return '<article class="result-card discovery-card" data-discovery-id="' + e(item.id) + '"><div><a class="result-link" href="' + e(link(item.id, revision, null, "discovery")) + '"><div class="result-title"><strong>' + e(item.name) + '</strong><span class="pill">' + e(t(item.kind === "repository" ? "api.repositoryCandidate" : "api.manifestCandidate")) + '</span></div><p class="result-summary">' + e(summary) + '</p></a><div class="result-meta"><span>' + e(t("api.agentAssociation")) + ': ' + e(agents) + '</span>' + observed + '</div><div class="discovery-tags">' + discoveryTags(item) + '</div>' + (item.kind === "manifest" ? '<p class="muted">' + e(t("api.declaredVersion")) + ': ' + e(declaredVersion || t("api.unknown")) + '</p>' : '') + '</div><div class="result-version"><strong>' + e(status) + '</strong><span>' + e(t("api.firstSeen")) + ': ' + e(formatDate(item.firstSeenAt)) + '</span><span>' + e(t(item.freshness.stale ? "api.stale" : "api.fresh")) + '</span></div></article>';
    }
    function metadataReviewPanel(item) {
      const review=item.metadataReview;if(!review)return "";
      return '<section class="detail-section metadata-review"><h2>'+e(t("review.title"))+'</h2><p class="scope-notice">'+e(t("review.notice"))+'</p><p class="metadata-review-status">'+e(t("review."+review.status))+'</p><p>'+e(t("review.fingerprint"))+'</p><code>'+e(review.evidenceFingerprint)+'</code>'+(review.latestDecision?'<p>'+e(t(review.evidenceMatches?"review.matches":"review.changed"))+'</p>':'')+'<details><summary>'+e(t("review.history"))+' ('+review.historyCount+')</summary><p>'+e(t("review.truncated",{shown:review.history.length,total:review.historyCount}))+'</p><ol>'+review.history.map(event=>'<li>'+e(t("review.reason."+event.reasonCode))+' · '+e(formatDate(event.reviewedAt))+'<code>'+e(event.id)+'</code><code>'+e(event.evidenceFingerprint)+'</code></li>').join("")+'</ol></details></section>';
    }
    function drawDiscoveryDetail() {
      const item = detail.item, declared = item.declaredMetadata, repo = item.repository;
      const fact = (key, value) => '<div><dt>' + e(t(key)) + '</dt><dd>' + e(value == null ? t("api.unknown") : value) + '</dd></div>';
      const heading = item.kind === "repository" ? "api.repositoryCandidate" : "api.manifestCandidate";
      const facts = fact("api.reviewStatus", t("api.review." + item.reviewStatus)) + fact("api.firstSeen", formatDate(item.firstSeenAt)) + fact("api.lastSeen", formatDate(item.lastSeenAt)) + fact("api.agentAssociation", item.agents.length ? item.agents.join(" · ") : null) + fact("api.sourceStatus", t(item.freshness.stale ? "api.stale" : "api.fresh"));
      const declaration = declared ? '<section class="detail-section"><h2>' + e(t("api.declaration")) + '</h2><dl class="facts">' + fact("api.declaredName", declared.name) + fact("api.declaredDescription", declared.description) + fact("api.declaredVersion", declared.version) + fact("api.declaredLicense", declared.license) + '</dl></section>' : '';
      const issues = item.issues.map((issue) => '<li>' + e(t("api.issue." + issue.code)) + (issue.field ? ' · ' + e(issue.field) : Array.isArray(issue.path) && issue.path.length ? ' · ' + e(issue.path.join(".")) : '') + '</li>').join("");
      const issuePanel = declared ? '<section class="detail-section"><h2>' + e(t("api.issues")) + '</h2>' + (issues ? '<ul class="candidate-issues">' + issues + '</ul>' : '<p>' + e(t("api.noIssues")) + '</p>') + '</section>' : '';
      const timeline = (Array.isArray(item.lifecycle) ? item.lifecycle : []).map((event) => '<li>' + e(t(["needs_metadata", "identity_conflict", "review_pending"].includes(event.state) ? "api.review." + event.state : "api." + event.state)) + ' · ' + e(formatDate(event.observedAt)) + '</li>').join("");
      const sourceEvidence = item.sourceEvidence.map((source) => '<li><a href="' + e(safeHref(source.url)) + '" target="_blank" rel="noreferrer">' + e(source.sourceId) + '</a><p class="muted">' + e(t("api.lastSeen")) + ': ' + e(formatDate(source.checkedAt)) + '</p><code>' + e(source.evidenceHash) + '</code></li>').join("");
      const pinned = item.manifestEvidence ? '<p class="muted">' + e(item.manifestEvidence.path) + '</p><code>' + e(item.manifestEvidence.commitSha) + '</code>' : '';
      main.innerHTML = '<a class="back-link" href="#">' + e(t("← 返回目录")) + '</a><section class="detail-heading"><div><p class="eyebrow">' + e(t(heading)) + '</p><h1>' + e(item.name) + '</h1><p class="lede">' + e(item.summary || repo.fullName) + '</p></div></section><p class="scope-notice">' + e(t("api.candidateNotice")) + '</p><p class="muted">' + e(detail.revision) + ' · ' + e(formatDate(detail.generatedAt)) + '</p><div class="detail-layout discovery-detail"><article class="panel detail-main"><section class="detail-section"><h2>' + e(t("api.reviewStatus")) + '</h2><dl class="facts">' + facts + '</dl><p>' + e(t("api.noCompatibility")) + '</p></section>' + declaration + issuePanel + metadataReviewPanel(item) + '<section class="detail-section"><h2>' + e(t("api.timeline")) + '</h2><ul>' + timeline + '</ul></section><section class="detail-section"><h2>' + e(t("标签")) + '</h2>' + discoveryTags(item) + '</section></article><aside class="panel detail-side"><section class="detail-section"><h2>' + e(t("api.publicRepository")) + '</h2><a href="' + e(safeHref(repo.url)) + '" target="_blank" rel="noreferrer">' + e(repo.fullName) + '</a><dl class="facts">' + fact("api.repositoryStars", repo.stars == null ? null : number(repo.stars)) + '</dl><p class="muted">' + e(t("api.candidateCoverage")) + '</p></section><section class="detail-section source-evidence"><h2>' + e(t("api.sourceEvidence")) + '</h2><p>' + e(t("api.sourceCount", { shown: number(item.sourceEvidence.length), total: number(item.sourceCount) })) + '</p>' + pinned + '<ul>' + sourceEvidence + '</ul></section></aside></div>';
      ui.apply(main);
    }
    function signalFacts(item) {
      const repo = item.repositoryMetrics, downloads = item.downloadMetrics;
      const row = (label, value, suffix = "") => '<span>' + e(t(label)) + ': <strong>' + e(value == null ? t("api.unknown") : number(value)) + '</strong>' + e(suffix) + '</span>';
      const growth = repo && repo.starGrowth, rate = downloads && downloads.downloadGrowth;
      let html = row("api.stars", repo && repo.stars ? repo.stars.value : null) + row("api.starRate", growth ? growth.valuePerHour : null);
      html += row("api.downloads", downloads && downloads.downloads30d ? downloads.downloads30d.value : null) + row("api.downloadRate", rate ? rate.value_per_hour : null);
      if(item.latestRelease){
        const r=item.latestRelease;
        const label=t("api.releaseDate",{version:r.canonicalVersion||t("api.unknown")});
        const value=r.status==="known"?formatDate(r.publishedAt)+" · "+r.selected.tagName+(r.selected.prerelease?" · "+t("api.releasePre"):""):t(r.reason==="future_published_time"?"api.releaseFuture":"api.releaseUnknown");
        html+='<span class="release-sort-fact">'+e(label)+': <strong>'+e(value)+'</strong></span>';
      }
      return '<div class="api-signals">' + html + '</div>';
    }
    function drawList(snapshot = session.snapshot()) {
      if (isDetailRoute() || !document.getElementById("results")) return;
      const data = snapshot.data;
      if(data)collectionControls.pin(data);
      if(data?.scope==="discovery")reviewsAvailable=Boolean(data.discoveryReviewCoverage);
      if(data)releaseSortAvailable=Boolean(data.releaseSortCoverage);
      applyScope();drawMetadataStatus();drawSourceOptions();
      text("catalog-status", snapshot.busy ? t("api.loading") : snapshot.error ? errorText(snapshot.error) : t("api.ready"));
      document.getElementById("query-error").hidden = !snapshot.error;
      text("query-error-message", snapshot.error ? errorText(snapshot.error) : "");
      document.getElementById("retry-query").disabled = snapshot.busy || !session.pending;
      document.getElementById("refresh-query").disabled = snapshot.busy;
      document.getElementById("previous-page").disabled = !snapshot.canPrevious;
      document.getElementById("next-page").disabled = !snapshot.canNext;
      document.getElementById("results").setAttribute("aria-busy", String(snapshot.busy));
      if (!data) return;
      text("revision-stamp", data.revision + " · " + formatDate(data.generatedAt));
      text("result-count", number(data.items.length));
      text("result-scope", t(data.scope === "discovery" ? "api.scope.discovery" : "api.scope.catalog"));
      text("page-number", t("api.page", { page: number(snapshot.page + 1) }));
      text("page-range", t("api.loaded", { count: number(data.items.length) }));
      text("ranking-context", data.sort === "latest_release" ? t("api.releasePolicy") : data.sort === "star_growth" ? t("api.starPolicy") : data.sort === "download_growth" ? t("api.downloadPolicy") : data.scope === "discovery" ? t("api.candidateCoverage") : "");
      const association = data.recommendationFilter;
      const sourceNotice = document.getElementById("source-match-notice");sourceNotice.hidden = !association;
      sourceNotice.textContent = association ? t("sourceFilter.notice",{source:association.labels.join(" · ")})+(association.freshness.stale?" "+t("sourceFilter.stale"):"") : "";
      text("coverage-status", data.freshnessSummary && data.freshnessSummary.stale ? t("api.partial") : "");
      document.getElementById("pagination").hidden = snapshot.page === 0 && !data.nextCursor;
      document.getElementById("empty-state").hidden = data.items.length > 0 || snapshot.busy || Boolean(snapshot.error);
      const hasMedia = data.scope !== "discovery" && data.items.some((item) => item.media && (item.media.icon || coverSlot(item.media)));
      document.getElementById("results").innerHTML = (hasMedia ? mediaControls() : "") + data.items.map((item) => {
        if (data.scope === "discovery") return discoveryCard(item, data.revision);
        const media = item.media || {}, agents = Array.isArray(item.targets) ? item.targets.map((target) => target.agentId).join(" · ") : t("api.unknown");
        const title = imageSlot(media.icon, "icon", item.type.slice(0, 1).toUpperCase()) + '<strong>' + e(item.displayName || item.name) + '</strong><span class="pill">' + e(item.type.toUpperCase()) + '</span>';
        return '<article class="result-card"><div><a class="result-link" href="' + e(link(item.id, data.revision)) + '">' + coverSlot(media) + '<div class="result-title">' + title + '</div><p class="result-summary">' + e(item.summary || t("暂无描述")) + '</p></a><div class="result-meta"><span>' + e(agents) + '</span><span>' + e(item.subtype || "") + '</span></div>' + renderPackageTags(item, 8) + signalFacts(item) + '</div><div class="result-version"><strong>' + e(item.canonicalLatest || t("api.unknown")) + '</strong><span>' + e(formatDate(item.updatedAt)) + '</span></div></article>';
      }).join("");
      bindMedia(document.getElementById("results"), () => drawList());
    }
    function drawDetail() {
      if (!detail || !isDetailRoute()) return;
      if (detail.scope === "discovery") { drawDiscoveryDetail(); return; }
      const record = detail.canonicalRecord, source = record || detail;
      const agents = Array.isArray(source.targets) ? source.targets.map((target) => target.agentId).join(" · ") : t("api.unknown");
      const fact = (key, value) => '<div><dt>' + e(t(key)) + '</dt><dd>' + e(value == null ? t("api.unknown") : value) + '</dd></div>';
      const distributions = Array.isArray(source.distributions) ? source.distributions : [];
      const sources = distributions.map((item) => '<a class="distribution" href="' + e(safeHref(item.url || item.href)) + '" target="_blank" rel="noreferrer"><strong>' + e(item.type) + '</strong><span>' + e(item.url || item.href || t("未提供地址")) + '</span></a>').join("") || '<p>' + e(t("暂无发行来源。Agent Forge 不托管插件文件。")) + '</p>';
      const versions = versionRows.map((row) => '<option value="' + e(row.version) + '"' + (record && record.version === row.version ? ' selected' : '') + '>' + e(row.version) + '</option>').join("");
      const available = record && !versionRows.some((row) => row.version === record.version) ? '<option selected value="' + e(record.version) + '">' + e(record.version) + '</option>' : '';
      main.innerHTML = '<a class="back-link" href="#">' + e(t("← 返回目录")) + '</a><section class="detail-heading"><div><p class="eyebrow">' + e(detail.type.toUpperCase()) + ' · ' + e(agents) + '</p><h1>' + e(source.displayName || source.name) + '</h1><p class="lede">' + e(source.description || source.summary || t("暂无描述")) + '</p></div></section><p class="muted">' + e(detail.revision) + ' · ' + e(formatDate(detail.generatedAt)) + '</p><div class="detail-layout"><article class="panel detail-main"><div class="detail-section"><h2>' + e(t("包信息")) + '</h2><dl class="facts">' + fact("包名", source.name) + fact("Agent", agents) + fact("api.selected", record && record.version) + fact("api.latest", detail.canonicalLatest) + fact("api.sourceStatus", t(detail.freshness && !detail.freshness.stale ? "api.fresh" : "api.stale")) + '</dl>' + (!record ? '<p role="status">' + e(t("api.noCanonical")) + '</p>' : '') + '</div><div class="detail-section"><h2>' + e(t("标签")) + '</h2>' + renderPackageTags(source) + '</div>' + renderGallery(source) + '<div class="detail-section"><h2>' + e(t("api.source")) + '</h2>' + signalFacts(detail) + '<p class="muted">' + e(t("api.coverage")) + '</p>' + intervalFacts(detail) + (detail.requestedVersion && detail.requestedVersion !== detail.canonicalLatest ? '<p class="muted">' + e(t("api.metricVersion", { version: detail.canonicalLatest || t("api.unknown") })) + '</p>' : '') + '</div></article><aside class="panel detail-side"><div class="detail-section"><h2>' + e(t("安装候选")) + '</h2>' + sources + '</div><div class="detail-section"><h2>' + e(t("api.versions")) + '</h2><select id="canonical-version" aria-label="' + e(t("api.selected")) + '">' + (!record ? '<option value="">' + e(t("api.unknown")) + '</option>' : '') + available + versions + '</select><p id="version-error" role="status">' + e(versionError ? t("api.detailError") : '') + '</p><button id="more-versions" type="button" class="button button-quiet"' + (!versionCursor ? ' hidden' : '') + (versionBusy ? ' disabled' : '') + '>' + e(t("api.more")) + '</button></div></aside></div>';
      const recHost=document.createElement("section");recHost.className="panel rec-panel";main.appendChild(recHost);
      recommendations.packagePanel(detail,recHost);
      const releaseHost=document.createElement("section");releaseHost.className="panel release-panel";main.appendChild(releaseHost);releases.packagePanel(detail,releaseHost);
      bindMedia(main, drawDetail);
      document.getElementById("canonical-version").addEventListener("change", (event) => { if (event.target.value) location.hash = link(detail.id, detail.revision, event.target.value); });
      document.getElementById("more-versions").addEventListener("click", () => loadVersions(detailGeneration));
      ui.apply(main);
    }
    function intervalFacts(item) {
      const repo = item.repositoryMetrics && item.repositoryMetrics.starGrowth, download = item.downloadMetrics && item.downloadMetrics.downloadGrowth;
      return [repo, download].filter(Boolean).map((metric) => '<p class="muted">' + e(metric.source_id || (metric === repo ? "github.com" : "npmjs.org")) + ' · ' + e(t("api.interval")) + ': ' + e(formatDate(metric.window_start)) + ' → ' + e(formatDate(metric.window_end)) + ' · ' + e(metric.elapsed_hours == null ? t("api.unknown") : number(metric.elapsed_hours)) + 'h · ' + e(t(metric.qualification && metric.qualification.eligible ? "api.qualified" : "api.unqualified")) + '</p>').join("");
    }
    async function loadVersions(generation) {
      if (!detail || versionBusy) return;
      versionBusy = true; drawDetail();
      try {
        const response = await client.versions(detail.id, { revision: detail.revision, cursor: versionCursor, signal: detailAbort.signal });
        if (generation !== detailGeneration) return;
        versionRows = [...versionRows, ...response.items]; versionCursor = response.nextCursor; versionError = null;
      } catch (error) { if (generation === detailGeneration && error.name !== "AbortError") versionError = error; }
      finally { if (generation === detailGeneration) { versionBusy = false; drawDetail(); } }
    }
    async function route() {
      const generation = ++detailGeneration;
      recommendations.stop();releases.stop();
      if (detailAbort) detailAbort.abort();
      detailAbort = new AbortController(); detail = null; versionRows = []; versionCursor = null; versionError = null; versionBusy = false;
      let info;
      try { info = routeInfo(); } catch (_) { info = { id: "", revision: null, version: null }; }
      if (!info) {
        if (!document.getElementById("query")) main.replaceChildren(...nodes);
        ui.apply(main); drawList(); return;
      }
      if (info.scope === "recommendations") { recommendations.open(info.source?"members":"sources",{source:info.source,revision:info.revision,status:info.status}); return; }
      main.innerHTML = '<a class="back-link" href="#">' + e(t("← 返回目录")) + '</a><p role="status">' + e(t("api.loading")) + '</p>';
      try {
        const response = info.scope === "discovery" ? await client.discovery(info.id, { revision: info.revision, signal: detailAbort.signal }) : await client.detail(info.id, { revision: info.revision, version: info.version, signal: detailAbort.signal });
        if (generation !== detailGeneration) return;
        detail = response; collectionControls.pin(detail); drawDetail(); if (info.scope !== "discovery") await loadVersions(generation);
      } catch (error) {
        if (generation !== detailGeneration || error.name === "AbortError") return;
        main.innerHTML = '<a class="back-link" href="#">' + e(t("← 返回目录")) + '</a><p role="alert">' + e(t("api.detailError")) + ' · ' + e(error.code || "network_error") + '</p><button id="retry-detail" type="button" class="button">' + e(t("api.retry")) + '</button>';
        document.getElementById("retry-detail").addEventListener("click", route);
      }
    }
    function updateUrl(value) {
      const url = new URL(location.href); url.search = "";
      for (const [key, item] of Object.entries(value)) if (key !== "limit" && item != null) url.searchParams.set(key, typeof item === "object" ? JSON.stringify(item) : String(item));
      history.replaceState(null, "", url.href);
    }
    async function commitSearch(response) {
      if (!response) return;
      updateUrl({ ...session.filters, revision: response.revision });
      const scope = response.scope || "catalog";
      if (scope === "catalog" && response.recommendationFilteringAvailable === true && (!sourceOptions || sourceOptions.revision !== response.revision)) loadCatalogSources(response.revision);
      if (scope === "catalog" && response.recommendationFilteringAvailable === false) { cancelCatalogSources(true); drawSourceOptions(); }
      const failedForRevision = metadataError && metadataRequest && metadataRequest.revision === response.revision && metadataRequest.scope === scope;
      if (!failedForRevision && (!metadata || metadata.revision !== response.revision || (metadata.scope || "catalog") !== scope)) await loadMetadata(response.revision, scope);
    }
    async function search(revision = null) {
      try { await commitSearch(await session.search(filters(), { revision: typeof revision === "string" ? revision : null })); }
      catch (_) { text("catalog-status", t("api.filterError")); }
    }
    async function retrySearch() {
      try { await commitSearch(await session.retry()); }
      catch (_) { text("catalog-status", t("api.filterError")); }
    }
    function schedule(delay) { clearTimeout(timer); timer = setTimeout(search, delay); }
    function selectValue(control, value) {
      if (value && ![...control.options].some((option) => option.value === value)) {
        const option = document.createElement("option"); option.value = value; option.textContent = value; control.appendChild(option);
      }
      control.value = value || "";
    }
    function cancelCatalogSources(unavailable = false) {
      ++sourceGeneration;if(sourceAbort)sourceAbort.abort();sourceAbort=null;
      sourceOptions = unavailable ? {revision:null,items:[],nextCursor:null,busy:false,error:null,unavailable:true} : null;
    }
    function drawSourceOptions() {
      const control=controls["recommendation-source-filter"], selected=control.value;
      control.innerHTML='<option value="">'+e(t("sourceFilter.all"))+'</option>'+(sourceOptions?.items||[]).map(item=>'<option value="'+e(item.id)+'">'+e(item.labels.join(" · "))+'</option>').join("");
      if(selected && ![...control.options].some(option=>option.value===selected)) {
        const option=document.createElement("option");option.value=selected;option.textContent=t("sourceFilter.unknownOption");control.appendChild(option);
      }
      control.value=selected;control.disabled=Boolean(sourceOptions?.unavailable);
      sourceControls.status.textContent=sourceOptions?.busy?t("sourceFilter.loading"):sourceOptions?.unavailable?t("sourceFilter.unavailable"):sourceOptions?.error?["cursor_expired","revision_unavailable"].includes(sourceOptions.error.code)?t("api.expired"):t("sourceFilter.error"):"";
      const retry=sourceControls.retry;retry.hidden=!sourceOptions?.error;retry.disabled=Boolean(sourceOptions?.busy);
      const more=sourceControls.more;more.hidden=!sourceOptions?.nextCursor;more.disabled=Boolean(sourceOptions?.busy||sourceOptions?.error);
    }
    async function loadCatalogSources(revision,cursor=null) {
      const generation=++sourceGeneration;if(sourceAbort)sourceAbort.abort();sourceAbort=new AbortController();
      if(!cursor || sourceOptions?.revision!==revision)sourceOptions={revision,items:[],nextCursor:null,busy:false,error:null,unavailable:false};
      sourceOptions.busy=true;sourceOptions.error=null;sourceOptions.requestCursor=cursor;drawSourceOptions();
      try {
        const response=await client.recommendationSources({revision,cursor,limit:20,signal:sourceAbort.signal});
        if(generation!==sourceGeneration||controls["scope-filter"].value!=="catalog")return;
        sourceOptions.items=[...sourceOptions.items,...response.items];sourceOptions.nextCursor=response.nextCursor;
      }catch(error){if(generation===sourceGeneration&&error.name!=="AbortError"){sourceOptions.error=error;sourceOptions.unavailable=error.code==="recommendations_unavailable";}}
      finally{if(generation===sourceGeneration){sourceOptions.busy=false;drawSourceOptions();}}
    }
    function selectedFacets() { return [...controls["facet-filter"].selectedOptions].map((option) => option.value); }
    async function loadMetadata(revision, scope = controls["scope-filter"].value) {
      const generation = ++metadataGeneration;
      if (metadataAbort) metadataAbort.abort();
      metadataAbort = new AbortController(); metadataRequest = { revision, scope };
      let response;
      try { response = await client.facets(revision, { scope, signal: metadataAbort.signal }); }
      catch (error) {
        if (generation !== metadataGeneration || error.name === "AbortError") return;
        metadata = null; metadataError = error; drawMetadataStatus(); return;
      }
      if (generation !== metadataGeneration || controls["scope-filter"].value !== scope || session.data && (session.data.revision !== revision || (session.data.scope || "catalog") !== scope)) return;
      metadataError = null; drawMetadataStatus();
      const selected = selectedFacets(), subtype = controls["subtype-filter"].value, agent = controls["agent-filter"].value; metadata = response;
      controls["agent-filter"].innerHTML = '<option value="" data-i18n="全部 Agent">' + e(t("全部 Agent")) + '</option>' + (response.agents || []).map((id) => '<option value="' + e(id) + '">' + e(id) + '</option>').join("");
      selectValue(controls["agent-filter"], agent);
      controls["subtype-filter"].innerHTML = '<option value="" data-i18n="全部 subtype">' + e(t("全部 subtype")) + '</option>' + (response.subtypes || []).map((row) => '<option value="' + e(row.value) + '">' + e(row.value) + '</option>').join("");
      selectValue(controls["subtype-filter"], subtype);
      let html = "";
      for (const custom of [false, true]) {
        for (const [kind, rows] of Object.entries(custom ? response.customFacets : response.facets)) {
          html += '<optgroup data-facet-custom="' + custom + '" data-facet-kind="' + e(kind) + '" label="' + e(t(custom ? "api.custom" : "api.controlled") + ' · ' + kind) + '">' + rows.map((row) => { const value = JSON.stringify([custom, kind, row.value]); return '<option value="' + e(value) + '"' + (selected.includes(value) ? ' selected' : '') + '>' + e(row.value) + ' (' + e(number(row.count)) + ')</option>'; }).join("") + '</optgroup>';
        }
      }
      controls["facet-filter"].innerHTML = html;
      for (const value of selected) if (![...controls["facet-filter"].options].some((option) => option.value === value)) {
        const option = document.createElement("option"); option.value = value; option.textContent = JSON.parse(value).slice(1).join(": "); option.selected = true; controls["facet-filter"].appendChild(option);
      }
    }
    async function initialize() {
      const params = new URLSearchParams(location.search);
      const allowed = new Set(["q", "agent", "type", "subtype", "sort", "facets", "customFacets", "metricSource", "recommendationSource", "reviewDisposition", "scope", "revision"]);
      const invalidQuery = [...params.keys()].some((key) => !allowed.has(key) || params.getAll(key).length !== 1 || ["recommendationSource","reviewDisposition"].includes(key) && !params.get(key));
      const scope = params.get("scope") || (location.hash.startsWith("#/discovery/") ? "discovery" : "catalog");
      controls["scope-filter"].value = scope;
      if (!params.has("sort")) controls["sort-select"].value = scope === "discovery" ? "first_seen" : "canonical_updated";
      applyScope();
      if(params.has("reviewDisposition"))selectValue(controls["review-disposition-filter"],params.get("reviewDisposition"));
      if(params.has("recommendationSource"))selectValue(controls["recommendation-source-filter"],params.get("recommendationSource"));
      for (const [key, id] of [["q", "query"], ["agent", "agent-filter"], ["type", "type-filter"], ["subtype", "subtype-filter"], ["sort", "sort-select"]]) if (params.has(key)) selectValue(controls[id], params.get(key));
      controls["scope-filter"].addEventListener("change", () => {
        ++formGeneration; clearTimeout(timer); ++metadataGeneration; if (metadataAbort) metadataAbort.abort();
        metadata = null; metadataRequest = null;
        cancelCatalogSources();drawSourceOptions();
        for (const id of ["agent-filter", "type-filter", "subtype-filter", "recommendation-source-filter", "review-disposition-filter"]) controls[id].value = "";
        controls["agent-filter"].innerHTML = '<option value="">' + e(t("全部 Agent")) + '</option>';
        controls["facet-filter"].innerHTML = "";
        metadataError = null; drawMetadataStatus();
        applyScope(true); schedule(0);
      });
      document.getElementById("retry-filter-options").addEventListener("click", () => { if (metadataRequest) loadMetadata(metadataRequest.revision, metadataRequest.scope); });
      document.getElementById("retry-source-options").addEventListener("click",()=>{if(sourceOptions)loadCatalogSources(sourceOptions.revision,sourceOptions.requestCursor);});
      document.getElementById("more-source-options").addEventListener("click",()=>{if(sourceOptions?.nextCursor)loadCatalogSources(sourceOptions.revision,sourceOptions.nextCursor);});
      controls.query.addEventListener("input", () => { ++formGeneration; schedule(300); });
      for (const id of ["agent-filter", "type-filter", "subtype-filter", "sort-select", "facet-filter", "recommendation-source-filter", "review-disposition-filter"]) controls[id].addEventListener("change", () => { ++formGeneration; schedule(0); });
      document.getElementById("clear-filters").addEventListener("click", () => { ++formGeneration; for (const id of ["query", "agent-filter", "type-filter", "subtype-filter", "recommendation-source-filter", "review-disposition-filter"]) controls[id].value = ""; for (const option of controls["facet-filter"].options) option.selected = false; schedule(0); });
      document.getElementById("previous-page").addEventListener("click", () => session.move(session.page - 1));
      document.getElementById("next-page").addEventListener("click", () => session.move(session.page + 1));
      document.getElementById("retry-query").addEventListener("click", retrySearch);
      document.getElementById("refresh-query").addEventListener("click", search);
      window.addEventListener("hashchange", route);
      window.addEventListener("forge:localechange", () => {
        for (const group of controls["facet-filter"].querySelectorAll("optgroup")) group.label = t(group.dataset.facetCustom === "true" ? "api.custom" : "api.controlled") + " · " + group.dataset.facetKind;
        drawSourceOptions();ui.apply(); applyScope(); if (detail) drawDetail(); else drawList();
      });
      window.addEventListener("pagehide", () => { clearTimeout(timer); session.dispose();cancelCatalogSources(); if (detailAbort) detailAbort.abort(); if (metadataAbort) metadataAbort.abort(); });
      window.addEventListener("keydown", (event) => { if (event.key === "/" && !event.ctrlKey && !event.altKey && !event.metaKey && !["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(document.activeElement.tagName)) { event.preventDefault(); if (document.getElementById("query")) controls.query.focus(); } });
      if (isDetailRoute()) route();
      try {
        if (invalidQuery || (params.has("metricSource") && (params.get("metricSource") !== "npmjs.org" || !["downloads", "download_growth"].includes(params.get("sort"))))) throw new window.ForgeQuery.ApiError("invalid_query");
        if (params.has("scope") && !["catalog", "discovery"].includes(params.get("scope"))) throw new window.ForgeQuery.ApiError("scope_unavailable");
        const initialFormGeneration = formGeneration;
        const manifest = await client.manifest({ revision: params.get("revision") });
        if (initialFormGeneration !== formGeneration) return;
        collectionControls.pin(isDetailRoute()&&detail?detail:manifest);
        reviewsAvailable=Boolean(manifest.features?.discoveryReviews);releaseSortAvailable=Boolean(manifest.features?.releaseSort);applyScope();
        if(scope === "catalog") {
          if(manifest.features?.recommendations)loadCatalogSources(manifest.revision);
          else {cancelCatalogSources(true);drawSourceOptions();}
        }
        const agent = params.get("agent") || "";
        controls["agent-filter"].innerHTML = '<option value="" data-i18n="全部 Agent">' + e(t("全部 Agent")) + '</option>' + (scope === "discovery" ? [] : manifest.agents || []).map((id) => '<option value="' + e(id) + '">' + e(id) + '</option>').join("");
        selectValue(controls["agent-filter"], agent);
        await loadMetadata(manifest.revision); if (initialFormGeneration !== formGeneration) return; selectValue(controls["subtype-filter"], params.get("subtype") || "");
        for (const key of ["facets", "customFacets"]) {
          const values = JSON.parse(params.get(key) || "{}");
          if (!values || typeof values !== "object" || Array.isArray(values)) throw new window.ForgeQuery.ApiError("invalid_query");
          for (const [kind, entries] of Object.entries(values)) {
            if (!Array.isArray(entries)) throw new window.ForgeQuery.ApiError("invalid_query");
            for (const entry of entries) {
              const value = JSON.stringify([key === "customFacets", kind, entry]);
              let option = [...controls["facet-filter"].options].find((item) => item.value === value);
              if (!option) { option = document.createElement("option"); option.value = value; option.textContent = kind + ": " + entry; controls["facet-filter"].appendChild(option); }
              option.selected = true;
            }
          }
        }
        await search(params.get("revision"));
      } catch (error) { session.error = error instanceof window.ForgeQuery.ApiError ? error : new window.ForgeQuery.ApiError("invalid_query"); session.emit(); }
    }
    initialize();
    return { session, client, route };
  }
})();
