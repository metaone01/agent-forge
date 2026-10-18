/* Read-only, revision-bound public query transport. No registry or plugin requests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ForgeQuery = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const sorts = new Set(["canonical_updated", "first_seen", "name", "relevance", "stars", "star_growth", "repo_pushed", "downloads", "download_growth", "latest_release"]);
  const types = new Set(["mcp", "plugin", "skill", "general", "bundle"]);
  class ApiError extends Error {
    constructor(code, status = 0, requestId = null) { super(code); this.name = "ApiError"; this.code = code; this.status = status; this.requestId = requestId; }
  }
  function string(value, maximum = 4096) { return typeof value === "string" && value.length > 0 && value.length <= maximum && !/[\x00-\x1f\x7f]/.test(value); }
  const reviewStatuses = new Set(["not_recorded","rejected","unsupported","needs_metadata","pending","superseded"]);
  const reviewActions = {reject:["rejected","outside_catalog_scope"],unsupported:["unsupported","unsupported_catalog_metadata"],request_metadata:["needs_metadata","additional_metadata_required"],reopen:["pending","reconsider_metadata"]};
  function validateMetadataReview(value,item,asOf) {
    const invalid=()=>{throw new ApiError("invalid_response");};
    const hash=x=>typeof x==="string"&&/^[0-9a-f]{64}$/.test(x);
    const fields=["policyVersion","authority","status","evidenceFingerprint","evidenceMatches","latestDecision","history","historyCount","historyTruncated"];
    if(!value||Object.keys(value).length!==fields.length||fields.some(key=>!Object.hasOwn(value,key))||value.policyVersion!=="candidate-metadata-review-v1"||value.authority!=="local-worker-metadata-triage"||!reviewStatuses.has(value.status)||!hash(value.evidenceFingerprint)||typeof value.evidenceMatches!=="boolean"||!Number.isSafeInteger(value.historyCount)||value.historyCount<0||typeof value.historyTruncated!=="boolean"||!Array.isArray(value.history)||value.history.length!==Math.min(value.historyCount,20)||value.historyTruncated!==(value.historyCount>20))invalid();
    const eventFields=["id","sequence","candidateId","evidenceFingerprint","action","reasonCode","reviewedAt","previousId","policyVersion","authority"];
    for(const event of value.history){
      if(!event||Object.keys(event).length!==eventFields.length||eventFields.some(key=>!Object.hasOwn(event,key))||!hash(event.id)||!hash(event.evidenceFingerprint)||event.candidateId!==item.id||!Number.isSafeInteger(event.sequence)||event.sequence<1||!Object.hasOwn(reviewActions,event.action)||event.reasonCode!==reviewActions[event.action][1]||event.previousId!==null&&!hash(event.previousId)||event.policyVersion!==value.policyVersion||event.authority!==value.authority||!string(event.reviewedAt,100)||!Number.isFinite(Date.parse(event.reviewedAt))||Date.parse(event.reviewedAt)>Date.parse(asOf))invalid();
    }
    if(!value.historyCount){if(value.latestDecision!==null||value.status!=="not_recorded"||value.evidenceMatches)invalid();return;}
    const last=value.history.at(-1);
    if(!value.latestDecision||Object.keys(value.latestDecision).length!==Object.keys(last).length||Object.keys(last).some(key=>value.latestDecision[key]!==last[key]))invalid();
    const matches=last.evidenceFingerprint===value.evidenceFingerprint;
    if(value.evidenceMatches!==matches||value.status!==(matches?reviewActions[last.action][0]:"superseded"))invalid();
    if(!value.historyTruncated&&value.history[0].previousId!==null)invalid();
    for(let index=1;index<value.history.length;index++){
      const prior=value.history[index-1],event=value.history[index];
      if(event.previousId!==prior.id||event.sequence<=prior.sequence||event.reviewedAt<prior.reviewedAt||event.action==="reopen"&&(event.evidenceFingerprint!==prior.evidenceFingerprint||prior.action==="reopen"))invalid();
    }
  }
  const controlPolicy="source-collection-controls-v1",controlAuthority="local-worker-collection-control";
  const controlActions={disable:"operator_collection_pause",enable:"operator_collection_resume"};
  const controlTargetFields={provider:["kind","provider"],github_repository:["kind","repositoryId"],github_repository_name:["kind","fullName"],github_topic:["kind","topic"],npm_package:["kind","package"],curated_file:["kind","repositoryId","path"]};
  const exactFields=(value,fields)=>value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length===fields.length&&fields.every(key=>Object.hasOwn(value,key));
  function validateControlTarget(value){
    const fields=value&&Object.hasOwn(controlTargetFields,value.kind)&&controlTargetFields[value.kind],bad=()=>{throw new ApiError("invalid_response");};
    if(typeof value?.kind!=="string"||!fields||!exactFields(value,fields))bad();
    if(value.kind==="provider"&&!["github.com","npmjs.org"].includes(value.provider))bad();
    if(["github_repository","curated_file"].includes(value.kind)&&(!Number.isSafeInteger(value.repositoryId)||value.repositoryId<1))bad();
    if(value.kind==="github_repository_name"&&(typeof value.fullName!=="string"||!/^[a-z0-9][a-z0-9-]{0,38}\/[a-z0-9_.-]{1,100}$/.test(value.fullName)||value.fullName!==value.fullName.toLowerCase()))bad();
    if(value.kind==="github_topic"&&(typeof value.topic!=="string"||! /^[a-z0-9][a-z0-9-]{0,49}$/.test(value.topic)))bad();
    if(value.kind==="npm_package"&&(typeof value.package!=="string"||value.package.length>214||! /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/.test(value.package)))bad();
    if(value.kind==="curated_file"&&(typeof value.path!=="string"||value.path.length>1024||!value.path.endsWith(".md")||value.path.split("/").some(part=>! /^[A-Za-z0-9_.-]+$/.test(part)||[".","..",".git"].includes(part))))bad();
  }
  function validateControlCoverage(value,required=false){
    const coverage=value.sourceControlCoverage;
    if(coverage===undefined){if(required)throw new ApiError("invalid_response");return;}
    const fields=["policyVersion","authority","asOf","events","targets","disabledTargets","enabledTargets"];
    if(typeof value.generatedAt!=="string"||! /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(value.generatedAt)||!Number.isFinite(Date.parse(value.generatedAt))||new Date(value.generatedAt).toISOString()!==value.generatedAt.replace(/(\.\d{3})\d{3}Z$/,"$1Z"))throw new ApiError("invalid_response");
    if(!exactFields(coverage,fields)||coverage.policyVersion!==controlPolicy||coverage.authority!==controlAuthority||coverage.asOf!==value.generatedAt||["events","targets","disabledTargets","enabledTargets"].some(key=>!Number.isSafeInteger(coverage[key])||coverage[key]<0)||coverage.events<1||coverage.targets<1||coverage.events<coverage.targets||coverage.disabledTargets+coverage.enabledTargets!==coverage.targets)throw new ApiError("invalid_response");
  }
  function controlJson(value){
    return JSON.stringify(value,function(key,item){return item&&typeof item==="object"&&!Array.isArray(item)?Object.fromEntries(Object.keys(item).sort().map(name=>[name,item[name]])):item;});
  }
  function validateControlItem(item,asOf){
    const fields=["id","target","collectionState","policyVersion","authority","latestEvent","history","historyCount","historyTruncated"];
    const bad=()=>{throw new ApiError("invalid_response");},hash=value=>typeof value==="string"&&/^[0-9a-f]{64}$/.test(value);
    if(!exactFields(item,fields)||! /^collection-source:[0-9a-f]{64}$/.test(item.id)||item.policyVersion!==controlPolicy||item.authority!==controlAuthority||!Number.isSafeInteger(item.historyCount)||item.historyCount<1||!Array.isArray(item.history)||item.history.length!==Math.min(20,item.historyCount)||item.historyTruncated!==(item.historyCount>20))bad();
    validateControlTarget(item.target);
    const eventFields=["id","sequence","targetId","target","action","reasonCode","recordedAt","previousId","policyVersion","authority"];
    for(const event of item.history){
      if(!exactFields(event,eventFields)||!hash(event.id)||!Number.isSafeInteger(event.sequence)||event.sequence<1||event.targetId!==item.id||controlJson(event.target)!==controlJson(item.target)||typeof event.action!=="string"||!Object.hasOwn(controlActions,event.action)||event.reasonCode!==controlActions[event.action]||event.policyVersion!==controlPolicy||event.authority!==controlAuthority||typeof event.recordedAt!=="string"||! /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(event.recordedAt)||!Number.isFinite(Date.parse(event.recordedAt))||new Date(event.recordedAt).toISOString()!==event.recordedAt.replace(/(\.\d{3})\d{3}Z$/,"$1Z")||event.recordedAt>asOf||event.previousId!==null&&!hash(event.previousId))bad();
    }
    if(controlJson(item.latestEvent)!==controlJson(item.history.at(-1))||item.collectionState!==(item.latestEvent.action==="disable"?"disabled":"enabled")||!item.historyTruncated&&(item.history[0].previousId!==null||item.history[0].action!=="disable")||item.historyTruncated&&item.history[0].previousId===null)bad();
    for(let index=1;index<item.history.length;index++){
      const before=item.history[index-1],after=item.history[index];
      if(after.previousId!==before.id||after.sequence<=before.sequence||after.recordedAt<before.recordedAt||after.action===before.action)bad();
    }
  }
  const releasePolicy="public-release-observations-v1";
  const releaseId=value=>typeof value==="string"&&/^[1-9][0-9]{0,18}$/.test(value)&&BigInt(value)<=9223372036854775807n;
  const releaseCount=value=>typeof value==="string"&&/^(?:0|[1-9][0-9]{0,18})$/.test(value)&&BigInt(value)<=9223372036854775807n;
  const releaseHash=value=>typeof value==="string"&&/^[0-9a-f]{64}$/.test(value);
  const releaseDate=value=>typeof value==="string"&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(value)&&Number.isFinite(Date.parse(value))&&new Date(value).toISOString()===value.replace(/(\.\d{3})\d{3}Z$/,"$1Z");
  function validateReleaseCoverage(value,required=false){
    const c=value.releaseCoverage,fail=()=>{throw new ApiError("invalid_response");};
    if(c===undefined){if(required)fail();return;}
    const fields=["policyVersion","asOf","pages","repositories","repositoryNamesProven","scans","scanStates","releaseIdentities","assets","packageLinks","exactDistributionLinks","scope","deletionInference","downloadMeaning","staleAfterSeconds"];
    if(!exactFields(c,fields)||c.policyVersion!==releasePolicy||!releaseDate(c.asOf)||c.asOf!==value.generatedAt||["pages","repositories","repositoryNamesProven","scans","releaseIdentities","assets","packageLinks","exactDistributionLinks"].some(key=>!Number.isSafeInteger(c[key])||c[key]<0)||c.pages<1||c.repositories<1||c.repositories>c.pages||c.repositoryNamesProven>c.repositories||c.scans<1||c.scans>c.pages||c.exactDistributionLinks>c.packageLinks||c.scope!=="observed-repository-metadata"||c.deletionInference!=="not_supported"||c.downloadMeaning!=="raw-asset-download-requests-not-users-or-npm-downloads"||c.staleAfterSeconds!==86400)fail();
    if(!exactFields(c.scanStates,["in_progress","partial","traversal_complete"])||Object.values(c.scanStates).some(count=>!Number.isSafeInteger(count)||count<0)||Object.values(c.scanStates).reduce((a,b)=>a+b,0)!==c.scans)fail();
  }
  function validateReleaseItem(item,response,id,version,association){
    const fail=()=>{throw new ApiError("invalid_response");};
    if(!exactFields(item,["release","association","freshness"]))fail();
    const r=item.release;
    if(!exactFields(r,["repositoryId","releaseId","tagName","name","prerelease","targetCommitish","createdAt","publishedAt","assets","observation"])||!releaseId(r.repositoryId)||!releaseId(r.releaseId)||!string(r.tagName,512)||r.name!==null&&r.name!==""&&!string(r.name,1000)||typeof r.prerelease!=="boolean"||!string(r.targetCommitish,512)||!releaseDate(r.createdAt)||!releaseDate(r.publishedAt)||!Array.isArray(r.assets)||r.assets.length>1000)fail();
    if(!exactFields(r.observation,["pageId","endpoint","evidenceHash","observedAt","checkedAt"])||!releaseHash(r.observation.pageId)||!releaseHash(r.observation.evidenceHash)||typeof r.observation.endpoint!=="string"||!new RegExp("^repositories/"+r.repositoryId+"/releases\\?per_page=100&page=(?:[1-9]|10)$").test(r.observation.endpoint)||!releaseDate(r.observation.observedAt)||!releaseDate(r.observation.checkedAt)||r.observation.observedAt>r.observation.checkedAt||r.observation.checkedAt>response.generatedAt)fail();
    const seen=new Set();
    for(const a of r.assets){
      if(!exactFields(a,["assetId","name","state","sizeBytes","downloadCount","createdAt","updatedAt","counterIdentity","unit","measurementKind","scope"])||!releaseId(a.assetId)||seen.has(a.assetId)||!string(a.name,1000)||!string(a.state,100)||!releaseCount(a.sizeBytes)||!releaseCount(a.downloadCount)||!releaseDate(a.createdAt)||!releaseDate(a.updatedAt)||a.counterIdentity!=="github-release-asset:"+r.repositoryId+":"+r.releaseId+":"+a.assetId+":downloads"||a.unit!=="download_requests"||a.measurementKind!=="cumulative"||a.scope!=="github_release_asset")fail();
      seen.add(a.assetId);
    }
    if(!exactFields(item.freshness,["checkedAt","stale","staleAfterSeconds"])||item.freshness.checkedAt!==r.observation.checkedAt||typeof item.freshness.stale!=="boolean"||item.freshness.staleAfterSeconds!==86400)fail();
    if(id===null){if(item.association!==null)fail();return;}
    const a=item.association;
    if(!exactFields(a,["packageId","version","canonicalHash","repositoryId","releaseId","assertions","association","policyVersion"])||a.packageId!==id||a.version!==version||!releaseHash(a.canonicalHash)||a.repositoryId!==r.repositoryId||a.releaseId!==r.releaseId||a.association!=="declared-url-only-not-version-or-install-verification"||a.policyVersion!==releasePolicy||!Array.isArray(a.assertions)||!a.assertions.length)fail();
    for(const p of a.assertions){
      const fields=p?.kind==="repository_context"?["field","url","kind"]:p?.kind==="exact_declared_release"?["field","url","kind","tag","assetName"]:p?.kind==="exact_declared_asset"?["field","url","kind","tag","assetName","assetId"]:null;
      if(!fields||!exactFields(p,fields)||typeof p.field!=="string"||! /^(?:\/links\/repository|\/distributions\/\d+\/url)$/.test(p.field)||!string(p.url,8192))fail();
      let url;try{url=new URL(p.url.replace(/^git\+/,""));}catch(_){fail();}
      if(url.protocol!=="https:"||url.hostname!=="github.com"||url.username||url.password||url.port||url.search||url.hash)fail();
      if(p.kind!=="repository_context"&&(p.tag!==r.tagName||p.field==="/links/repository"))fail();
      if(p.kind==="exact_declared_release"&&p.assetName!==null)fail();
      if(p.kind==="exact_declared_asset"&&(!releaseId(p.assetId)||!string(p.assetName,1000)||!r.assets.some(asset=>asset.assetId===p.assetId&&asset.name===p.assetName)))fail();
    }
    const declared=a.assertions.some(p=>p.kind!=="repository_context");
    if(association==="declared"&&!declared||association==="repository_context"&&declared)fail();
  }
  function validateReleaseChecks(value){
    const fail=()=>{throw new ApiError("invalid_response");};
    if(!Array.isArray(value.repositoryChecks)||value.repositoryChecks.length>20||!Number.isSafeInteger(value.repositoryCheckCount)||value.repositoryCheckCount<value.repositoryChecks.length||value.repositoryChecksTruncated!==(value.repositoryCheckCount>20))fail();
    const seen=new Set();
    for(const check of value.repositoryChecks){
      if(!exactFields(check,["repositoryId","lastCheckedAt","latestTraversal","observedReleaseIdentities","traversals","coverage","deletionInference"])||!releaseId(check.repositoryId)||seen.has(check.repositoryId)||!releaseDate(check.lastCheckedAt)||check.lastCheckedAt>value.generatedAt||check.coverage!=="observed-pages-only"||check.deletionInference!=="not_supported"||!Number.isSafeInteger(check.observedReleaseIdentities)||check.observedReleaseIdentities<0||!Number.isSafeInteger(check.traversals)||check.traversals<1)fail();
      seen.add(check.repositoryId);const s=check.latestTraversal;
      if(!exactFields(s,["scanId","repositoryId","selectedAt","lastCheckedAt","pages","maxPages","state","meaning"])||!releaseHash(s.scanId)||s.repositoryId!==check.repositoryId||!releaseDate(s.selectedAt)||!releaseDate(s.lastCheckedAt)||s.lastCheckedAt>check.lastCheckedAt||s.selectedAt>value.generatedAt||!Number.isInteger(s.pages)||s.pages<1||!Number.isInteger(s.maxPages)||s.maxPages<1||s.maxPages>10||s.pages>s.maxPages||!["partial","in_progress","traversal_complete"].includes(s.state)||s.meaning!=="mutable-page-traversal-not-snapshot-or-deletion-proof")fail();
    }
  }
  const releaseSortPolicy="canonical-declared-release-time-v1";
  function validateReleaseSortCoverage(value,required=false){
    const c=value.releaseSortCoverage,fail=()=>{throw new ApiError("invalid_response");};
    if(c===undefined){if(required)fail();return;}
    if(!value.releaseCoverage||!exactFields(c,["policyVersion","scope","asOf","packages","known","unknown","exactCandidates","futureExcluded","prereleasesIncluded","selection","timestampField","versionPolicy"])||c.policyVersion!==releaseSortPolicy||c.scope!=="canonical-latest-explicit-distribution"||c.asOf!==value.generatedAt||!releaseDate(c.asOf)||["packages","known","unknown","exactCandidates","futureExcluded"].some(k=>!Number.isSafeInteger(c[k])||c[k]<0)||c.known+c.unknown!==c.packages||c.known>c.exactCandidates||c.futureExcluded>c.exactCandidates||c.prereleasesIncluded!==true||c.selection!=="maximum-published-at-then-numeric-repository-release-id"||c.timestampField!=="published_at"||c.versionPolicy!=="canonical-latest-only")fail();
  }
  function validateLatestRelease(item,response){
    const v=item.latestRelease,fail=()=>{throw new ApiError("invalid_response");};
    if(!exactFields(v,["policyVersion","canonicalVersion","canonicalHash","status","publishedAt","reason","candidateCount","eligibleCount","futureCount","selected"])||v.policyVersion!==releaseSortPolicy||v.canonicalVersion!==item.canonicalLatest||v.canonicalVersion!==null&&!string(v.canonicalVersion,1024)||v.canonicalHash!==null&&!releaseHash(v.canonicalHash)||!["known","unknown"].includes(v.status)||["candidateCount","eligibleCount","futureCount"].some(k=>!Number.isSafeInteger(v[k])||v[k]<0)||v.eligibleCount+v.futureCount!==v.candidateCount)fail();
    if(v.status==="unknown"){
      if(v.publishedAt!==null||v.selected!==null||v.eligibleCount!==0||!["no_canonical_version","no_exact_declared_release","future_published_time"].includes(v.reason)||v.reason==="no_canonical_version"&&(v.canonicalHash!==null||v.candidateCount!==0)||v.reason==="no_exact_declared_release"&&(!releaseHash(v.canonicalHash)||v.candidateCount!==0)||v.reason==="future_published_time"&&(!releaseHash(v.canonicalHash)||v.futureCount<1))fail();
      return;
    }
    const r=v.selected;
    if(!releaseHash(v.canonicalHash)||v.eligibleCount<1||v.reason!=="explicit_declared_release"||!releaseDate(v.publishedAt)||v.publishedAt>response.generatedAt||!exactFields(r,["repositoryId","releaseId","tagName","prerelease","publishedAt","associationKinds","observation"])||!releaseId(r.repositoryId)||!releaseId(r.releaseId)||!string(r.tagName,512)||typeof r.prerelease!=="boolean"||r.publishedAt!==v.publishedAt||!Array.isArray(r.associationKinds)||!r.associationKinds.length||r.associationKinds.some(k=>!["exact_declared_release","exact_declared_asset"].includes(k))||JSON.stringify([...new Set(r.associationKinds)].sort())!==JSON.stringify(r.associationKinds))fail();
    const o=r.observation;
    if(!exactFields(o,["pageId","endpoint","evidenceHash","observedAt","checkedAt"])||!releaseHash(o.pageId)||!releaseHash(o.evidenceHash)||typeof o.endpoint!=="string"||!new RegExp("^repositories/"+r.repositoryId+"/releases\\?per_page=100&page=(?:[1-9]|10)$").test(o.endpoint)||!releaseDate(o.observedAt)||!releaseDate(o.checkedAt)||o.observedAt>o.checkedAt||o.checkedAt>response.generatedAt)fail();
  }
  function envelope(value) {
    if (!value || typeof value !== "object" || !string(value.revision, 128) || !string(value.generatedAt, 100)) throw new ApiError("invalid_response");
    validateControlCoverage(value);
    validateReleaseCoverage(value);
    validateReleaseSortCoverage(value);
    return value;
  }
  function normalizeFilters(value = {}, limit = 20) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new ApiError("invalid_query");
    const scope = value.scope == null ? "catalog" : value.scope;
    if (!["catalog", "discovery"].includes(scope)) throw new ApiError("scope_unavailable");
    const output = { limit, sort: value.sort == null ? (scope === "discovery" ? "first_seen" : "canonical_updated") : value.sort };
    if (scope === "discovery") output.scope = scope;
    if (scope === "discovery" && !["first_seen", "name", "relevance"].includes(output.sort)) throw new ApiError("sort_unavailable");
    if (!sorts.has(output.sort)) throw new ApiError("sort_unavailable");
    if (value.q != null) {
      if (typeof value.q !== "string" || value.q.length > 200 || /[\x00-\x1f\x7f]/.test(value.q)) throw new ApiError("invalid_query");
      if (value.q.trim()) output.q = value.q.trim();
    }
    if (output.sort === "relevance" && !output.q) throw new ApiError("relevance_requires_query");
    for (const key of ["agent", "type", "subtype"]) {
      if (value[key]) { if (!string(value[key], 200)) throw new ApiError("invalid_query"); output[key] = value[key]; }
    }
    if (value.reviewDisposition != null) {
      if(scope!=="discovery"||!reviewStatuses.has(value.reviewDisposition))throw new ApiError("invalid_query");
      output.reviewDisposition=value.reviewDisposition;
    }
    if (value.recommendationSource != null) {
      if (scope !== "catalog" || !validateRecommendationSourceId(value.recommendationSource)) throw new ApiError("invalid_query");
      output.recommendationSource = value.recommendationSource;
    }
    if (output.type && !types.has(output.type)) throw new ApiError("invalid_query");
    for (const key of ["facets", "customFacets"]) {
      const facets = value[key] || {};
      if (!facets || typeof facets !== "object" || Array.isArray(facets) || Object.keys(facets).length > 7) throw new ApiError("invalid_query");
      const normalized = {};
      const allowed = ["capabilities", "effects", "dataPractices", "permissions", "runtime", "integrations"].concat(key === "customFacets" ? ["other"] : []);
      for (const kind of Object.keys(facets).sort()) {
        if (!allowed.includes(kind) || !Array.isArray(facets[kind]) || facets[kind].length < 1 || facets[kind].length > 10 || facets[kind].some((item) => !string(item, 200))) throw new ApiError("invalid_query");
        normalized[kind] = [...new Set(facets[kind])].sort();
      }
      if (Object.keys(normalized).length) output[key] = normalized;
    }
    if (output.sort === "downloads" || output.sort === "download_growth") output.metricSource = "npmjs.org";
    return output;
  }

  function validateDiscovery(item,asOf) {
    const fail = () => { throw new ApiError("invalid_response"); };
    const date = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
    if (!item || item.scope !== "discovery" || !string(item.id) || !string(item.name) || !["repository", "manifest"].includes(item.kind) || !date(item.firstSeenAt) || !date(item.lastSeenAt) || item.firstSeenAt > item.lastSeenAt) fail();
    const repo = item.repository;
    if (!repo || !Number.isSafeInteger(repo.githubRepoId) || repo.githubRepoId < 1 || !string(repo.fullName, 140) || !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/.test(repo.fullName) || repo.url !== "https://github.com/" + repo.fullName) fail();
    if (!Array.isArray(item.agents) || !Array.isArray(item.issues) || !Array.isArray(item.sourceEvidence) || item.sourceEvidence.length < 1 || item.sourceEvidence.length > 20 || !Number.isSafeInteger(item.sourceCount) || item.sourceCount < item.sourceEvidence.length || item.evidenceTruncated !== (item.sourceCount > item.sourceEvidence.length)) fail();
    const issueCodes = new Set(["missing_or_invalid_declaration", "canonical_identity_conflict", "upstream_profile_invalid", "skill_directory_name_mismatch", "schema_invalid", "identity_invalid"]);
    if (item.issues.length > 33 || item.issues.some((issue) => !issue || typeof issue !== "object" || !issueCodes.has(issue.code) || issue.path != null && !Array.isArray(issue.path))) fail();
    if (item.lifecycle != null && (!Array.isArray(item.lifecycle) || item.lifecycle.length > 7 || item.lifecycle.some((event) => !event || typeof event.state !== "string" || !date(event.observedAt)))) fail();
    if (!item.freshness || typeof item.freshness.stale !== "boolean" || item.freshness.lastObservationAt !== item.lastSeenAt || item.freshness.staleAfterSeconds !== 86400) fail();
    for (const evidence of item.sourceEvidence) {
      if (!evidence || !string(evidence.sourceId, 100) || !/^[0-9a-f]{64}$/.test(evidence.evidenceHash) || !date(evidence.observedAt) || !date(evidence.checkedAt) || evidence.observedAt > evidence.checkedAt || evidence.checkedAt > item.lastSeenAt || evidence.observedAt < item.firstSeenAt) fail();
      let url;
      try { url = new URL(evidence.url); } catch (_) { fail(); }
      if (url.protocol !== "https:" || url.username || url.password || url.hash || !["github.com", "api.github.com"].includes(url.hostname) || url.port) fail();
    }
    if (item.kind === "repository") {
      if (item.id !== "github-repository:" + repo.githubRepoId || item.name !== repo.fullName || item.type !== null || item.agents.length || item.declaredMetadata !== null || item.manifestEvidence !== null || item.reviewStatus !== "unreviewed" || item.classification !== "discovered" || item.sourceEvidence.some((e) => e.kind !== "github_search" || !e.sourceId.startsWith("github-search:"))) fail();
    } else {
      const manifest = item.manifestEvidence;
      if (!/^manifest:[0-9a-f]{64}$/.test(item.id) || !manifest || !["claude_plugin", "agent_skill"].includes(manifest.format) || !["needs_metadata", "identity_conflict", "review_pending"].includes(item.reviewStatus) || item.classification !== "declared_profile" || item.type !== (manifest.format === "claude_plugin" ? "plugin" : "skill") || manifest.association !== "explicit-profile-not-compatibility-verification" || item.agents.length !== 1 || item.agents[0] !== manifest.agentAssociation || !/^[0-9a-f]{40}$/.test(manifest.commitSha) || !/^[0-9a-f]{40}$/.test(manifest.blobSha) || !/^[0-9a-f]{64}$/.test(manifest.contentSha256)) fail();
      if (manifest.format === "claude_plugin" && manifest.agentAssociation !== "claude-code") fail();
      if (!string(manifest.path, 500) || manifest.path.split("/").some((part) => !/^[A-Za-z0-9_.-]{1,100}$/.test(part) || [".", ".."].includes(part))) fail();
      const url = repo.url + "/blob/" + manifest.commitSha + "/" + manifest.path;
      if (manifest.url !== url || item.sourceEvidence.length !== 1 || item.sourceEvidence[0].kind !== "manifest_declaration" || item.sourceEvidence[0].url !== url || item.sourceEvidence[0].evidenceHash !== manifest.contentSha256 || !item.declaredMetadata || ["name", "description", "version", "license"].some((key) => item.declaredMetadata[key] !== null && typeof item.declaredMetadata[key] !== "string")) fail();
    }
    if(Object.hasOwn(item,"metadataReview"))validateMetadataReview(item.metadataReview,item,asOf);
    return item;
  }

  function validateDiscoveryEnvelope(value) {
    const coverage = value.discoveryCoverage;
    if (value.scope !== "discovery" || !coverage || coverage.policyVersion !== "discovery-evidence-v1" || coverage.asOf !== value.generatedAt || coverage.coverage !== "not_guaranteed" || coverage.adoption !== "review_required" || ["repositories", "manifests", "sources"].some((key) => !Number.isSafeInteger(coverage[key]) || coverage[key] < 0) || !value.freshnessSummary || typeof value.freshnessSummary.stale !== "boolean" || value.freshnessSummary.complete !== false) throw new ApiError("invalid_response");
  }

  function validateReviewCoverage(value) {
    const coverage=value.discoveryReviewCoverage;
    if(!coverage)return;
    if(coverage.policyVersion!=="candidate-metadata-review-v1"||coverage.authority!=="local-worker-metadata-triage"||coverage.asOf!==value.generatedAt||!Number.isSafeInteger(coverage.events)||coverage.events<1||!Number.isSafeInteger(coverage.candidatesWithRecordedHistory)||coverage.candidatesWithRecordedHistory<1||coverage.candidatesWithRecordedHistory>coverage.events||coverage.meaning!=="metadata-scope-triage-not-canonical-adoption-or-safety-approval"||!coverage.visibleDispositions||Array.isArray(coverage.visibleDispositions)||Object.entries(coverage.visibleDispositions).some(([key,count])=>!reviewStatuses.has(key)||!Number.isSafeInteger(count)||count<0)||Object.values(coverage.visibleDispositions).reduce((sum,x)=>sum+x,0)!==value.discoveryCoverage.repositories+value.discoveryCoverage.manifests)throw new ApiError("invalid_response");
  }

  function validateDashboard(value, agent) {
    const invalid = () => { throw new ApiError("invalid_response"); };
    const count = (item) => Number.isSafeInteger(item) && item >= 0;
    const typeCounts = (map, total) => map && typeof map === "object" && !Array.isArray(map) && Object.keys(map).length === types.size && [...types].every((key) => count(map[key])) && Object.values(map).reduce((a,b) => a+b, 0) === total;
    const totals = value.totals, compatibility = value.compatibility;
    if (!value.scope || value.scope.agent !== agent || value.policyVersion !== "canonical-dashboard-v1" || !["precomputed", "on-demand"].includes(value.projectionMode)) invalid();
    if (!totals || !["packages", "versions", "agents", "bundles"].every((key) => count(totals[key])) || totals.versions < totals.packages || !typeCounts(value.types,totals.packages) || totals.bundles !== value.types.bundle) invalid();
    if (!Array.isArray(value.agents) || value.agents.length !== totals.agents || new Set(value.agents.map((item) => item && item.id)).size !== value.agents.length) invalid();
    if (value.agents.some((item) => !item || !string(item.id,100) || !count(item.packageCount) || item.packageCount > totals.packages || !count(item.versionCount) || item.versionCount < item.packageCount || item.versionCount > totals.versions || !typeCounts(item.types,item.packageCount))) invalid();
    if (agent !== null && (value.agents.length !== 1 || value.agents[0].id !== agent || value.agents[0].packageCount !== totals.packages || value.agents[0].versionCount !== totals.versions)) invalid();
    if (!compatibility || compatibility.unit !== "package-agent-declaration" || !count(compatibility.known) || !count(compatibility.unknown) || compatibility.known + compatibility.unknown !== value.agents.reduce((sum,item) => sum+item.packageCount,0)) invalid();
    for (const [items,custom] of [[value.facets,false],[value.customFacets,true]]) {
      const kinds = ["capabilities", "effects", "dataPractices", "permissions", "runtime", "integrations"].concat(custom ? ["other"] : []);
      if (!Array.isArray(items) || items.length > 20 || items.some((item) => !item || !kinds.includes(item.kind) || !string(item.value,200) || !count(item.count) || item.count > totals.packages) || new Set(items.map((item) => JSON.stringify([item.kind,item.value]))).size !== items.length) invalid();
    }
    if (!Array.isArray(value.recentPackages) || value.recentPackages.length > 10 || value.recentPackages.length > totals.packages || value.recentPackages.some((item) => !item || !string(item.id) || !string(item.name) || !types.has(item.type) || typeof item.summary !== "string" || (item.canonicalLatest !== null && !string(item.canonicalLatest,1024)) || (item.canonicalUpdatedAt !== null && !string(item.canonicalUpdatedAt,100))) || new Set(value.recentPackages.map((item) => item.id)).size !== value.recentPackages.length) invalid();
  }

  function validateRecommendationCoverage(value) {
    const coverage = value.recommendationCoverage;
    if (!coverage || coverage.policyVersion !== "public-curated-projection-v1" || coverage.asOf !== value.generatedAt || coverage.coverage !== "configured-scopes-only" || coverage.rankPolicy !== "upstream-rank-unknown" || coverage.endorsement !== "not-inferred" || ["sources", "snapshots", "members", "active", "withdrawn", "packageLinks"].some((key) => !Number.isSafeInteger(coverage[key]) || coverage[key] < 0) || !Array.isArray(value.items) || (value.nextCursor !== null && !string(value.nextCursor,65536)) || !["available","unknown"].includes(value.availability)) throw new ApiError("invalid_response");
  }
  function validateRecommendationSourceId(value) { return string(value,200) && /^curated-source:[0-9a-f]{64}$/.test(value); }
  function validateRecommendationFreshness(value) {
    if (!value || !string(value.lastSuccessAt,100) || !string(value.lastObservationAt,100) || typeof value.stale !== "boolean" || !Number.isFinite(value.ageSeconds) || value.ageSeconds < 0 || value.staleAfterSeconds !== 86400 || value.coverage !== "configured-scopes-only") throw new ApiError("invalid_response");
  }
  function recommendationTarget(value) {
    if (!string(value,2000)) return null;
    let url; try { url = new URL(value); } catch (_) { return null; }
    const parts = url.pathname.replace(/^\/+|\/+$/g, "").split("/");
    if (url.protocol !== "https:" || url.host !== "github.com" || url.username || url.password || parts.length < 2 || parts.some((part)=>!(/^[A-Za-z0-9_.+-]+$/.test(part)) || part === "." || part === "..") || parts.length > 2 && (!["tree","blob"].includes(parts[2]) || parts.length < (parts[2] === "tree" ? 4 : 5)) || !["", "?tab=readme-ov-file"].includes(url.search) || url.hash && !/^#[A-Za-z0-9_-]+$/.test(url.hash)) return null;
    parts[0] = parts[0].toLowerCase(); parts[1] = parts[1].toLowerCase();
    return "https://github.com/" + parts.join("/");
  }
  function validateRecommendationItem(item, packageId, version, source, status, generatedAt) {
    const invalid = () => { throw new ApiError("invalid_response"); };
    const hash = (value, length=64) => typeof value === "string" && new RegExp("^[0-9a-f]{"+length+"}$").test(value);
    const date = (value) => string(value,100) && Number.isFinite(Date.parse(value)) && Date.parse(value) <= Date.parse(generatedAt);
    if (!item || !validateRecommendationSourceId(item.sourceId) || source && item.sourceId !== source || !["active","withdrawn"].includes(item.status) || status !== "all" && item.status !== status || item.sourceRank !== null || item.claimKind !== "curated_listing" || item.endorsement !== "source-listing-not-package-endorsement" || !string(item.name,200) || !date(item.observedAt) || !date(item.completedAt)) invalid();
    let target;
    try { target = new URL(item.targetUrl); } catch (_) { invalid(); }
    if (recommendationTarget(item.targetUrl) !== item.targetUrl || target.search || target.hash) invalid();
    if (!item.source || item.source.id !== item.sourceId || !Array.isArray(item.source.labels) || !item.source.labels.length || item.source.labels.some((label)=>!string(label,200)) || !Number.isSafeInteger(item.source.repositoryId) || item.source.repositoryId < 1 || !string(item.source.path,1024) || !string(item.source.selectionStatement,500)) invalid();
    const proof = (value) => {
      if (!value || !hash(value.id) || !hash(value.commitSha,40) || !hash(value.blobSha,40) || !hash(value.contentSha256) || !date(value.selectionAt) || !date(value.observedAt) || !date(value.checkedAt) || !date(value.completedAt) || !Number.isSafeInteger(value.memberCount) || !Number.isSafeInteger(value.entryCount) || value.memberCount < 0 || value.entryCount < value.memberCount || !value.scope || !Number.isSafeInteger(value.scope.startLine) || !Number.isSafeInteger(value.scope.endLine) || value.scope.startLine >= value.scope.endLine || !Array.isArray(value.steps) || value.steps.length < 5) invalid();
      let url;try { url = new URL(value.url); } catch (_) { invalid(); }
      if (url.protocol !== "https:" || url.host !== "github.com" || url.username || url.password || url.search || url.hash || !url.pathname.includes("/blob/"+value.commitSha+"/")) invalid();
      for (const step of value.steps) {
        let endpoint;try { endpoint = new URL(step.url); } catch (_) { invalid(); }
        if (!["repository","commit","tree","blob","verify"].includes(step.kind) || ![200,304].includes(step.status) || !hash(step.responseHash) || !date(step.observedAt) || !date(step.checkedAt) || endpoint.protocol !== "https:" || endpoint.host !== "api.github.com" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) invalid();
      }
    };
    proof(item.snapshot);
    if (item.snapshotId !== item.snapshot.id || item.completedAt !== item.snapshot.completedAt || item.observedAt !== item.snapshot.selectionAt) invalid();
    if (item.previousSnapshot === null) { if (item.previousSnapshotId !== null || item.status === "withdrawn") invalid(); }
    else { proof(item.previousSnapshot); if (item.previousSnapshotId !== item.previousSnapshot.id) invalid(); }
    if (item.reason !== (item.status === "active" ? "complete_snapshot_membership" : "complete_snapshot_absence")) invalid();
    const citationProof = item.status === "active" ? item.snapshot : item.previousSnapshot;
    if (!Array.isArray(item.citations) || !Number.isSafeInteger(item.citationCount) || item.citationCount < 1 || item.citations.length !== Math.min(item.citationCount,20) || item.citationsTruncated !== (item.citationCount > 20) || item.citations.some((entry)=>!entry || !Number.isSafeInteger(entry.line) || entry.line <= citationProof.scope.startLine || entry.line >= citationProof.scope.endLine || !string(entry.section,500) || recommendationTarget(entry.url) !== item.targetUrl)) invalid();
    validateRecommendationFreshness(item.freshness);
    if (packageId === null) { if (item.packageAssociation !== null) invalid(); }
    else {
      const association = item.packageAssociation;
      if (!association || association.packageId !== packageId || association.version !== version || association.sourceId !== item.sourceId || association.targetUrl !== item.targetUrl || association.association !== "exact-declared-link" || association.endorsement !== "not-inferred" || !hash(association.canonicalHash) || !Array.isArray(association.assertions) || !association.assertions.length || association.assertions.some((entry)=>!entry || !["repository_context","distribution_declaration"].includes(entry.kind) || !string(entry.field) || recommendationTarget(entry.url) !== item.targetUrl)) invalid();
    }
  }

  class Client {
    constructor(base, { fetchImpl = globalThis.fetch, timeoutMs = 10000 } = {}) {
      this.base = new URL(base);
      if (!["http:", "https:"].includes(this.base.protocol) || this.base.username || this.base.password || this.base.search || this.base.hash) throw new ApiError("invalid_api_base");
      if (!this.base.pathname.endsWith("/")) this.base.pathname += "/";
      if (typeof fetchImpl !== "function" || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) throw new ApiError("invalid_client_settings");
      this.fetchImpl = fetchImpl.bind(globalThis); this.timeoutMs = timeoutMs;
    }
    async request(route, parameters = {}, { signal } = {}) {
      if (parameters.revision != null && (typeof parameters.revision !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(parameters.revision))) throw new ApiError("invalid_revision");
      const url = new URL(route, this.base);
      if (url.origin !== this.base.origin || !url.pathname.startsWith(this.base.pathname)) throw new ApiError("invalid_api_route");
      for (const [key, value] of Object.entries(parameters)) if (value != null && value !== "") url.searchParams.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal) { if (signal.aborted) abort(); else signal.addEventListener("abort", abort, { once: true }); }
      let timer;
      const deadline = new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new ApiError("request_timeout")); }, this.timeoutMs); });
      try {
        const work = (async () => {
          const response = await this.fetchImpl(url.href, { headers: { Accept: "application/json" }, credentials: "omit", cache: "no-cache", signal: controller.signal });
          const raw = await response.text();
          if (new TextEncoder().encode(raw).byteLength > 2 * 1024 * 1024) throw new ApiError("invalid_response");
          let value;
          try { value = JSON.parse(raw); } catch (_) { throw new ApiError("invalid_response", response.status); }
          if (!response.ok) {
            const code = value && value.error && value.error.code;
            throw new ApiError(typeof code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : "service_unavailable", response.status,
                               value && string(value.requestId, 100) ? value.requestId : null);
          }
          return envelope(value);
        })();
        return await Promise.race([work, deadline]);
      } catch (error) {
        if (error instanceof ApiError || (error && error.name === "AbortError")) throw error;
        throw new ApiError("network_error");
      } finally { clearTimeout(timer); if (signal) signal.removeEventListener("abort", abort); }
    }
    async manifest({ revision = null, signal } = {}) {
      const value = await this.request("manifest", { revision }, { signal });
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      if(value.features&&Object.hasOwn(value.features,"sourceControls")){
        if(typeof value.features.sourceControls!=="boolean"||Boolean(value.sourceControlCoverage)!==value.features.sourceControls)throw new ApiError("invalid_response");
      }else if(value.sourceControlCoverage)throw new ApiError("invalid_response");
      if(value.features&&Object.hasOwn(value.features,"releases")){if(typeof value.features.releases!=="boolean"||Boolean(value.releaseCoverage)!==value.features.releases)throw new ApiError("invalid_response");}else if(value.releaseCoverage)throw new ApiError("invalid_response");
      if(value.features&&Object.hasOwn(value.features,"releaseSort")){
        if(typeof value.features.releaseSort!=="boolean"||Boolean(value.releaseSortCoverage)!==value.features.releaseSort||value.features.releaseSort&&(!value.features.releases||value.releaseSortPolicyVersion!==releaseSortPolicy||!Array.isArray(value.availableSorts)||value.availableSorts.filter(s=>s==="latest_release").length!==1)||!value.features.releaseSort&&(value.releaseSortPolicyVersion!==undefined||value.availableSorts?.includes("latest_release")))throw new ApiError("invalid_response");
      }else if(value.releaseSortCoverage||value.releaseSortPolicyVersion||value.availableSorts?.includes("latest_release"))throw new ApiError("invalid_response");
      return value;
    }
    async dashboard({ agent = null, revision = null, signal } = {}) {
      if (agent !== null && !string(agent, 100)) throw new ApiError("invalid_query");
      const value = await this.request("dashboard", { agent, revision }, { signal });
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      validateDashboard(value, agent);
      return value;
    }
    async facets(revision, { scope = "catalog", signal } = {}) {
      if (!["catalog", "discovery"].includes(scope)) throw new ApiError("scope_unavailable");
      const value = await this.request("facets", { revision, scope: scope === "discovery" ? scope : null }, { signal });
      if (value.revision !== revision || !value.facets || !value.customFacets || value.scope != null && value.scope !== scope) throw new ApiError("invalid_response");
      if (scope === "discovery" && (value.scope !== scope || !Array.isArray(value.agents) || value.agents.some((id) => !string(id,100)) || !Array.isArray(value.types) || value.types.some((row) => !row || !["plugin","skill"].includes(row.value) || !Number.isSafeInteger(row.count) || row.count < 1) || !Array.isArray(value.subtypes) || value.subtypes.length || Object.keys(value.facets).length || Object.keys(value.customFacets).length)) throw new ApiError("invalid_response");
      return value;
    }
    async versions(id, { revision, cursor = null, signal } = {}) {
      if (!string(id)) throw new ApiError("invalid_query");
      const value = await this.request("packages/" + encodeURIComponent(id) + "/versions", { revision, cursor, limit: 20 }, { signal });
      if (value.revision !== revision || !Array.isArray(value.items) || value.items.some((item) => !item || !string(item.version, 1024)) || (value.nextCursor !== null && !string(value.nextCursor, 65536))) throw new ApiError("invalid_response");
      return value;
    }
    async packages(filters, { cursor = null, revision = null, signal } = {}) {
      const value = await this.request("packages", { ...filters, cursor, revision }, { signal });
      if (!Array.isArray(value.items) || value.items.length > filters.limit || (value.nextCursor !== null && !string(value.nextCursor, 65536))) throw new ApiError("invalid_response");
      if (filters.scope === "discovery") {
        validateDiscoveryEnvelope(value);
        validateReviewCoverage(value);
        if(value.reviewDisposition!=null&&value.reviewDisposition!==filters.reviewDisposition)throw new ApiError("invalid_response");
        if(filters.reviewDisposition && (!value.discoveryReviewCoverage||value.reviewDisposition!==filters.reviewDisposition))throw new ApiError("invalid_response");
        value.items.forEach(item=>{
          validateDiscovery(item,value.generatedAt);
          if(Boolean(value.discoveryReviewCoverage)!==Object.hasOwn(item,"metadataReview")||filters.reviewDisposition&&item.metadataReview.status!==filters.reviewDisposition)throw new ApiError("invalid_response");
        });
      } else if (value.scope != null && value.scope !== "catalog" || value.items.some((item) => !item || !string(item.id) || !types.has(item.type))) throw new ApiError("invalid_response");
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      if(filters.sort==="latest_release"&&(value.sort!=="latest_release"||value.sortPolicyVersion!==releaseSortPolicy||value.sortScope!=="canonical-latest-explicit-distribution"||value.sortAsOf!==value.generatedAt))throw new ApiError("invalid_response");
      validateReleaseSortCoverage(value,filters.sort==="latest_release");
      if(value.releaseSortCoverage&&filters.scope!=="discovery"){
        value.items.forEach(item=>validateLatestRelease(item,value));
      }else if(value.items.some(item=>Object.hasOwn(item,"latestRelease")))throw new ApiError("invalid_response");
      if (Object.hasOwn(value,"recommendationFilteringAvailable") && typeof value.recommendationFilteringAvailable !== "boolean") throw new ApiError("invalid_response");
      if (filters.recommendationSource) {
        const filter = value.recommendationFilter;
        if (value.recommendationFilteringAvailable !== true || !filter || filter.sourceId !== filters.recommendationSource || !Array.isArray(filter.labels) || !filter.labels.length || filter.labels.some(label=>!string(label,200)) || filter.status !== "active" || filter.canonicalVersionPolicy !== "canonical-latest-only" || filter.association !== "exact-declared-link" || filter.endorsement !== "not-inferred" || filter.sourceRank !== null || filter.policyVersion !== "canonical-latest-active-source-v1") throw new ApiError("invalid_response");
        validateRecommendationFreshness(filter.freshness);
        for (const item of value.items) {
          const relation = item.recommendationAssociation;
          if (!relation || relation.sourceId !== filter.sourceId || !string(item.canonicalLatest,1024) || relation.version !== item.canonicalLatest || typeof relation.canonicalHash !== "string" || !/^[0-9a-f]{64}$/.test(relation.canonicalHash) || relation.status !== "active" || relation.association !== "exact-declared-link" || relation.endorsement !== "not-inferred" || relation.sourceRank !== null) throw new ApiError("invalid_response");
        }
      } else if (value.recommendationFilter != null) throw new ApiError("invalid_response");
      return value;
    }
    async discovery(id, { revision = null, signal } = {}) {
      if (!string(id) || !/^(github-repository:[1-9][0-9]{0,18}|manifest:[0-9a-f]{64})$/.test(id)) throw new ApiError("invalid_query");
      const value = await this.request("discoveries/" + encodeURIComponent(id), { revision }, { signal });
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      validateDiscoveryEnvelope(value);
      validateReviewCoverage(value);validateDiscovery(value.item,value.generatedAt);
      if(Boolean(value.discoveryReviewCoverage)!==Object.hasOwn(value.item,"metadataReview"))throw new ApiError("invalid_response");
      if (value.item.id !== id) throw new ApiError("invalid_response");
      return value;
    }
    async recommendationSources({ revision = null, cursor = null, limit = 20, signal } = {}) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || cursor !== null && !string(cursor,65536)) throw new ApiError("invalid_query");
      const value = await this.request("recommendation-sources", { revision, cursor, limit }, { signal });
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      validateRecommendationCoverage(value);
      if (value.items.length > limit) throw new ApiError("invalid_response");
      for (const item of value.items) {
        if (!item || !validateRecommendationSourceId(item.id) || item.provider !== "github-curated-markdown-v1" || !Array.isArray(item.labels) || !item.labels.length || item.labels.some((label)=>!string(label,200)) || !Number.isSafeInteger(item.repositoryId) || item.repositoryId < 1 || !string(item.path,1024) || !string(item.selectionStatement,500) || item.sourceRank !== null || item.claimKind !== "curated_listing" || item.coverage !== "configured-scopes-only") throw new ApiError("invalid_response");
        validateRecommendationFreshness(item.freshness);
      }
      return value;
    }
    async releases(id=null,{repositoryId=null,version=null,association="all",revision=null,cursor=null,limit=20,signal}={}){
      if(id!==null&&!string(id)||repositoryId!==null&&!releaseId(repositoryId)||version!==null&&!string(version,1024)||!["all","declared","repository_context"].includes(association)||id===null&&(version!==null||association!=="all")||!Number.isInteger(limit)||limit<1||limit>100||cursor!==null&&!string(cursor,65536))throw new ApiError("invalid_query");
      const value=await this.request(id===null?"releases":"packages/"+encodeURIComponent(id)+"/releases",{repositoryId,version,association:id===null?null:association,revision,cursor,limit},{signal});
      if(revision&&value.revision!==revision)throw new ApiError("revision_mismatch");
      validateReleaseCoverage(value,true);
      if(value.id!==id||value.repositoryId!==repositoryId||value.associationFilter!==association||value.policyVersion!==releasePolicy||value.order!=="published-desc-repository-id-release-id"||id===null&&value.selectedVersion!==null||id!==null&&!string(value.selectedVersion,1024)||version!==null&&value.selectedVersion!==version||!Array.isArray(value.items)||value.items.length>limit||value.nextCursor!==null&&!string(value.nextCursor,65536)||value.availability!==(value.items.length?"available":"unknown"))throw new ApiError("invalid_response");
      const seen=new Set();
      for(const item of value.items){validateReleaseItem(item,value,id,value.selectedVersion,association);const r=item.release,key=r.repositoryId+":"+r.releaseId;if(seen.has(key)||repositoryId!==null&&r.repositoryId!==repositoryId)throw new ApiError("invalid_response");seen.add(key);}
      for(let index=1;index<value.items.length;index++){const a=value.items[index-1].release,b=value.items[index].release;if(a.publishedAt<b.publishedAt||a.publishedAt===b.publishedAt&&(BigInt(a.repositoryId)>BigInt(b.repositoryId)||a.repositoryId===b.repositoryId&&BigInt(a.releaseId)>=BigInt(b.releaseId)))throw new ApiError("invalid_response");}
      validateReleaseChecks(value);return value;
    }
    async sourceControls({revision=null,cursor=null,limit=20,signal}={}){
      if(!Number.isInteger(limit)||limit<1||limit>100||cursor!==null&&!string(cursor,65536))throw new ApiError("invalid_query");
      const value=await this.request("source-controls",{revision,cursor,limit},{signal});
      if(revision&&value.revision!==revision)throw new ApiError("revision_mismatch");
      validateControlCoverage(value,true);
      if(!Array.isArray(value.items)||value.items.length>limit||value.nextCursor!==null&&!string(value.nextCursor,65536)||new Set(value.items.map(item=>item?.id)).size!==value.items.length)throw new ApiError("invalid_response");
      value.items.forEach(item=>{validateControlItem(item,value.generatedAt);if(item.historyCount>value.sourceControlCoverage.events||item.history.some(event=>event.sequence>value.sourceControlCoverage.events))throw new ApiError("invalid_response");});
      for(let index=1;index<value.items.length;index++)if(value.items[index-1].id>=value.items[index].id)throw new ApiError("invalid_response");
      return value;
    }
    async recommendations(id = null, { source = null, version = null, status = "all", revision = null, cursor = null, limit = 20, signal } = {}) {
      if (id !== null && !string(id) || source !== null && !validateRecommendationSourceId(source) || version !== null && !string(version,1024) || !["all","active","withdrawn"].includes(status) || !Number.isInteger(limit) || limit < 1 || limit > 100 || cursor !== null && !string(cursor,65536) || id === null && (source === null || version !== null)) throw new ApiError("invalid_query");
      const route = id === null ? "recommendations" : "packages/" + encodeURIComponent(id) + "/recommendations";
      const value = await this.request(route, { source, version, status, revision, cursor, limit }, { signal });
      if (revision && value.revision !== revision) throw new ApiError("revision_mismatch");
      validateRecommendationCoverage(value);
      if (value.items.length > limit || id !== null && !string(value.selectedVersion,1024)) throw new ApiError("invalid_response");
      if (value.id !== id || value.sourceId !== source || value.statusFilter !== status || value.order !== "source-target" || value.policyVersion !== "public-curated-projection-v1" || value.selectedVersion !== null && !string(value.selectedVersion,1024) || version !== null && value.selectedVersion !== version || id === null && value.selectedVersion !== null) throw new ApiError("invalid_response");
      value.items.forEach((item)=>validateRecommendationItem(item,id,value.selectedVersion,source,status,value.generatedAt));
      return value;
    }
    async detail(id, { revision = null, version = null, signal } = {}) {
      if (!string(id) || (version !== null && !string(version,1024))) throw new ApiError("invalid_query");
      const value = await this.request("packages/" + encodeURIComponent(id) + "/detail", { revision, version }, { signal });
      if (value.id !== id || (revision && value.revision !== revision) || !Object.hasOwn(value, "canonicalRecord") || (version && (!value.canonicalRecord || value.canonicalRecord.version !== version))) throw new ApiError("invalid_response");
      if(value.releaseSortCoverage)validateLatestRelease(value,value);
      else if(Object.hasOwn(value,"latestRelease"))throw new ApiError("invalid_response");
      return value;
    }
  }

  class CatalogSession {
    constructor(client, { limit = 20, onChange = () => {} } = {}) {
      this.client = client; this.limit = limit; this.onChange = onChange;
      this.data = null; this.filters = null; this.page = 0; this.history = new Map();
      this.busy = false; this.error = null; this.pending = null; this.generation = 0; this.controller = null;
    }
    snapshot() { return { data: this.data, filters: this.filters, page: this.page, busy: this.busy, error: this.error,
      canPrevious: !this.busy && !this.error && this.page > 0 && (this.page === 1 || this.history.has(this.page - 1)),
      canNext: !this.busy && !this.error && Boolean(this.data && this.data.nextCursor) }; }
    emit() { this.onChange(this.snapshot()); }
    async search(filters, { revision = null } = {}) {
      try { return await this.load(normalizeFilters(filters, this.limit), 0, null, revision, true); }
      catch (error) { ++this.generation; if (this.controller) this.controller.abort(); this.pending = null; this.error = error; this.busy = false; this.emit(); return null; }
    }
    async move(page) {
      if (this.busy || this.error || !this.data || !Number.isInteger(page) || page < 0) return null;
      const cursor = page === 0 ? null : page === this.page + 1 ? this.data.nextCursor : this.history.get(page);
      if (page !== 0 && !cursor) return null;
      return this.load(this.filters, page, cursor, this.data.revision);
    }
    retry() { return this.pending ? this.load(...this.pending) : Promise.resolve(null); }
    async load(filters, page, cursor, revision, resetHistory = false) {
      const generation = ++this.generation;
      if (this.controller) this.controller.abort();
      this.controller = new AbortController();
      this.pending = [filters, page, cursor, revision, resetHistory];
      this.busy = true; this.error = null; this.emit();
      try {
        const data = await this.client.packages(filters, { cursor, revision, signal: this.controller.signal });
        if (generation !== this.generation) return null;
        if (revision && data.revision !== revision) throw new ApiError("revision_mismatch");
        if (resetHistory || !revision) this.history.clear();
        this.history.set(page, cursor);
        while (this.history.size > 64) this.history.delete(this.history.keys().next().value);
        this.data = data; this.filters = filters; this.page = page; this.error = null;
        return data;
      } catch (error) {
        if (generation === this.generation && (!error || error.name !== "AbortError")) this.error = error instanceof ApiError ? error : new ApiError("network_error");
        return null;
      } finally { if (generation === this.generation) { this.busy = false; this.emit(); } }
    }
    dispose() { ++this.generation; if (this.controller) this.controller.abort(); this.busy = false; }
  }
  return { ApiError, Client, CatalogSession, normalizeFilters };
});
