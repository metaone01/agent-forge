const { test } = require('node:test');
const assert = require('node:assert/strict');
const { ApiError, Client, CatalogSession, normalizeFilters } = require('../site/assets/query-client.js');
const base = 'https://example.test/project/api/v1/';
const dto = (revision = 'one', nextCursor = null, id = 'plugin.alpha') => ({ revision, generatedAt: '2026-10-12T00:00:00Z', items: [{ id, type: 'plugin' }], nextCursor });
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, text: async () => typeof body === 'string' ? body : JSON.stringify(body) });
const defer = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; };

test('normalization binds explicit download source and separates facet namespaces', () => {
  const value = normalizeFilters({ q: ' terminal ', sort: 'downloads', facets: { capabilities: ['b', 'a', 'a'] }, customFacets: { other: ['x'] } });
  assert.equal(value.q, 'terminal'); assert.equal(value.metricSource, 'npmjs.org');
  assert.deepEqual(value.facets, { capabilities: ['a','b'] }); assert.deepEqual(value.customFacets, { other: ['x'] });
  assert.equal(normalizeFilters({ sort: 'star_growth' }).metricSource, undefined);
});
test('invalid and unavailable filters never silently select another sort', () => {
  for (const filters of [{ sort:'most_used' }, { sort:'relevance' }, { q:'a'.repeat(201) }, { q:'x\0y' }, { type:'bogus' }, { facets:{ other:['x'] } }, { facets:{ runtime:[null] } }, { facets:{ runtime:Array(11).fill('x') } }]) assert.throws(() => normalizeFilters(filters), ApiError);
  for (const limit of [0, 101, true, 2.1]) assert.throws(() => normalizeFilters({}, limit), ApiError);
});
test('API base excludes credentials and non-HTTP schemes', () => {
  for (const value of ['file:///tmp/api/', 'https://user:pass@example.test/api/', base+'?token=x', base+'#fragment']) assert.throws(() => new Client(value), ApiError);
});
test('details encode entire slash/Unicode/suffix identity once and pin exact version', async () => {
  let request;
  const id='dsh/plugin/数据库/metrics%2Fpart';
  const client=new Client(base,{fetchImpl:async(url,options)=>{request={url,options};return response({...dto(),id,canonicalRecord:{version:'1.0.0'}});}});
  await client.detail(id,{revision:'one',version:'1.0.0'});
  const url=new URL(request.url);
  assert.equal(url.pathname,'/project/api/v1/packages/'+encodeURIComponent(id)+'/detail');
  assert.equal(url.searchParams.get('version'),'1.0.0'); assert.equal(url.searchParams.get('revision'),'one');
  assert.equal(request.options.credentials,'omit'); assert.equal(request.options.headers.Authorization,undefined);
});
test('detail rejects other versions and IDs instead of accepting fallback', async () => {
  const client=new Client(base,{fetchImpl:async()=>response({...dto(),id:'plugin.alpha',canonicalRecord:{version:'2.0.0'}})});
  await assert.rejects(client.detail('plugin.alpha',{version:'1.0.0'}),{code:'invalid_response'});
  await assert.rejects(client.detail('wrong-id'),{code:'invalid_response'});
});
test('query URL keeps namespace facets and metric source', async () => {
  let url;
  const client=new Client(base,{fetchImpl:async(value)=>{url=new URL(value);return response(dto());}});
  const filters=normalizeFilters({sort:'download_growth',customFacets:{other:['hello world']}});
  await client.packages(filters,{cursor:'fixture',revision:'one'});
  assert.equal(url.searchParams.get('metricSource'),'npmjs.org');
  assert.deepEqual(JSON.parse(url.searchParams.get('customFacets')),{other:['hello world']});
  assert.equal(url.searchParams.get('cursor'),'fixture'); assert.equal(url.searchParams.get('revision'),'one');
});
test('stable service errors retain code/request ID without exposing remote message',async()=>{
  const client=new Client(base,{fetchImpl:async()=>response({error:{code:'cursor_expired',message:'private-path'},requestId:'request-1'},410)});
  await assert.rejects(client.packages(normalizeFilters()),error=>error.code==='cursor_expired'&&error.status===410&&error.requestId==='request-1'&&!error.message.includes('private-path'));
});
test('malformed, oversized, mismatched or incomplete responses are rejected',async()=>{
  for(const body of ['<html>bad</html>','x'.repeat(2*1024*1024+1),{}, {...dto(),items:null},{...dto(),nextCursor:undefined},{...dto(),items:[{id:'x',type:'unsafe'}]}]){
    const client=new Client(base,{fetchImpl:async()=>response(body)});
    await assert.rejects(client.packages(normalizeFilters()),{code:'invalid_response'});
  }
  const client=new Client(base,{fetchImpl:async()=>response(dto('two'))});
  await assert.rejects(client.packages(normalizeFilters(),{revision:'one'}),{code:'revision_mismatch'});
});
test('request timeout aborts transport even if fetch never settles',async()=>{
  let signal;
  const client=new Client(base,{timeoutMs:15,fetchImpl:async(_,options)=>{signal=options.signal;return new Promise(()=>{});}});
  await assert.rejects(client.packages(normalizeFilters()),{code:'request_timeout'});
  assert.equal(signal.aborted,true);
});
test('network failures have a local stable code',async()=>{
  const client=new Client(base,{fetchImpl:async()=>{throw new Error('private network info');}});
  await assert.rejects(client.packages(normalizeFilters()),{code:'network_error'});
});
test('metadata and version history reject revision drift',async()=>{
  const client=new Client(base,{fetchImpl:async()=>response({...dto('two'),facets:{},customFacets:{}})});
  await assert.rejects(client.facets('one'),{code:'invalid_response'});
  await assert.rejects(client.versions('plugin.alpha',{revision:'one'}),{code:'invalid_response'});
});
test('pagination pins old revision and only follows issued cursors',async()=>{
  const calls=[];
  const session=new CatalogSession({packages:async(filters,options)=>{calls.push(options);return dto(options.revision||'one',options.cursor?null:'next');}});
  await session.search({sort:'name'});await session.move(1);
  assert.equal(calls[1].revision,'one');assert.equal(calls[1].cursor,'next');
  await session.move(0);assert.equal(calls[2].revision,'one');assert.equal(calls[2].cursor,null);
  await session.move(100);assert.equal(calls.length,3);
});
test('changing filters resets cursor/revision after success',async()=>{
  const calls=[];
  const session=new CatalogSession({packages:async(filters,options)=>{calls.push({filters,options});return dto(options.revision||'one','next');}});
  await session.search({q:'old'});await session.move(1);await session.search({q:'new'});
  assert.equal(calls[2].options.cursor,null);assert.equal(calls[2].options.revision,null);assert.equal(session.page,0);assert.equal(session.history.size,1);
});
test('failed search keeps prior results, disables paging, and retries the same query',async()=>{
  let count=0;const calls=[];
  const session=new CatalogSession({packages:async(filters)=>{calls.push(filters.q);if(++count===2)throw new ApiError('service_busy',503);return dto('one',null,filters.q||'old');}});
  await session.search({q:'old'});await session.search({q:'new'});
  assert.equal(session.data.items[0].id,'old');assert.equal(session.snapshot().canPrevious,false);assert.equal(session.error.code,'service_busy');
  await session.retry();assert.equal(session.data.items[0].id,'new');assert.deepEqual(calls,['old','new','new']);assert.equal(session.error,null);
});
test('out-of-order requests cannot overwrite latest search even if abort is ignored',async()=>{
  const old=defer(),newer=defer(),signals=[];
  const session=new CatalogSession({packages:async(filters,options)=>{signals.push(options.signal);return filters.q==='old'?old.promise:newer.promise;}});
  const a=session.search({q:'old'});const b=session.search({q:'new'});
  assert.equal(signals[0].aborted,true);newer.resolve(dto('new'));await b;old.resolve(dto('old'));await a;
  assert.equal(session.data.revision,'new');assert.equal(session.busy,false);
});
test('invalid new filters cancel old work rather than committing its response',async()=>{
  const old=defer();let signal;
  const session=new CatalogSession({packages:async(_,options)=>{signal=options.signal;return old.promise;}});
  const pending=session.search({q:'old'});await session.search({sort:'relevance'});
  assert.equal(signal.aborted,true);old.resolve(dto());await pending;
  assert.equal(session.data,null);assert.equal(session.error.code,'relevance_requires_query');
});
test('retired revision is explicit and a fresh search starts from current',async()=>{
  let retire=false;
  const session=new CatalogSession({packages:async(_,options)=>{if(options.cursor&&retire)throw new ApiError('cursor_expired',410);return dto(retire?'two':'one','next');}});
  await session.search({});retire=true;await session.move(1);
  assert.equal(session.error.code,'cursor_expired');assert.equal(session.data.revision,'one');
  await session.search({});assert.equal(session.data.revision,'two');assert.equal(session.error,null);
});
test('cursor trail is bounded and does not invent unknown pages',async()=>{
  const session=new CatalogSession({packages:async()=>dto('one','next')});await session.search({});
  for(let i=1;i<80;i++)await session.move(i);
  assert.equal(session.history.size,64);assert.equal(session.page,79);
  await session.move(1);assert.equal(session.page,79);
});
test('dispose prevents a late response from altering state',async()=>{
  const pending=defer();const session=new CatalogSession({packages:()=>pending.promise});
  const request=session.search({});session.dispose();pending.resolve(dto());await request;assert.equal(session.data,null);assert.equal(session.busy,false);
});

test('browser transport retains the global receiver required by native wrappers',async()=>{
  let receiver;
  const client=new Client(base,{fetchImpl:async function(){receiver=this;return response(dto());}});
  await client.packages(normalizeFilters());assert.equal(receiver,globalThis);
});

test('response cap is measured in UTF-8 bytes, including multibyte metadata',async()=>{
  const client=new Client(base,{fetchImpl:async()=>response({ ...dto(), description:'界'.repeat(800000) })});
  await assert.rejects(client.packages(normalizeFilters()),{code:'invalid_response'});
});

const dashboardDto = (agent = null) => ({ revision:'one', generatedAt:'2026-10-12T00:00:00Z', scope:{agent}, policyVersion:'canonical-dashboard-v1', projectionMode:'precomputed', totals:{packages:2,versions:3,agents:1,bundles:0}, types:{mcp:0,plugin:2,skill:0,general:0,bundle:0}, agents:[{id:'dsh',packageCount:2,versionCount:3,types:{mcp:0,plugin:2,skill:0,general:0,bundle:0}}], facets:[{kind:'capabilities',value:'gui.observe',count:2}], customFacets:[{kind:'other',value:'<b>personal</b>',count:1}], compatibility:{known:1,unknown:1,unit:'package-agent-declaration'}, recentPackages:[{id:'dsh/plugin/数据库',name:'tool',type:'plugin',summary:'public',canonicalLatest:'1.0.0',canonicalUpdatedAt:'2026-10-12T00:00:00Z'}] });
test('dashboard pins revision, scoped Agent and credential omission', async () => {
  let request; const agent='dsh'; const client=new Client(base,{fetchImpl:async(url,options)=>{request={url,options};return response(dashboardDto(agent));}});
  const data=await client.dashboard({agent,revision:'one'}); const url=new URL(request.url);
  assert.equal(url.pathname,'/project/api/v1/dashboard'); assert.equal(url.searchParams.get('agent'),'dsh'); assert.equal(url.searchParams.get('revision'),'one');
  assert.equal(data.types.mcp,0); assert.equal(request.options.credentials,'omit');
});
test('dashboard rejects revision/scope drift and misleading count relationships', async () => {
  const bad=[];
  for(const change of [value=>value.scope.agent='other', value=>value.totals.packages=3, value=>value.types.plugin=-1, value=>value.totals.versions=1, value=>value.compatibility.unknown=0, value=>value.agents[0].versionCount=4, value=>value.facets[0].count=3, value=>value.facets[0].kind='other', value=>value.customFacets.push({...value.customFacets[0]}), value=>value.recentPackages.push({...value.recentPackages[0]}), value=>value.policyVersion='unrecognized']) { const value=dashboardDto(); change(value); bad.push(value); }
  for (const body of bad) { const client=new Client(base,{fetchImpl:async()=>response(body)}); await assert.rejects(client.dashboard(),{code:'invalid_response'}); }
  const client=new Client(base,{fetchImpl:async()=>response({...dashboardDto(),revision:'two'})}); await assert.rejects(client.dashboard({revision:'one'}),{code:'revision_mismatch'});
});
test('dashboard preserves safe global zeros without inventing Agent memberships', async () => {
  const value=dashboardDto(); value.totals={packages:0,versions:0,agents:0,bundles:0};value.types.plugin=0;value.agents=[];value.facets=[];value.customFacets=[];value.recentPackages=[];value.compatibility.known=0;value.compatibility.unknown=0;
  const client=new Client(base,{fetchImpl:async()=>response(value)});assert.equal((await client.dashboard()).totals.packages,0);
});
test('manifest and initial catalog search honor a requested revision', async () => {
  const requests=[]; const client=new Client(base,{fetchImpl:async(url)=>{requests.push(new URL(url));return response(dto());}});
  await client.manifest({revision:'one'}); const session=new CatalogSession(client); await session.search({}, {revision:'one'});
  assert.equal(requests[0].searchParams.get('revision'),'one'); assert.equal(requests[1].searchParams.get('revision'),'one'); assert.equal(session.data.revision,'one');
  await session.search({q:'terminal'});assert.equal(requests[2].searchParams.has('revision'),false);
  const drift=new Client(base,{fetchImpl:async()=>response(dto('two'))});await assert.rejects(drift.manifest({revision:'one'}),{code:'revision_mismatch'});
});

test('invalid explicit revisions are rejected before any network request', async () => {
  let requests=0;const client=new Client(base,{fetchImpl:async()=>{requests++;return response(dto());}});
  for(const revision of ['', '../one', 'x'.repeat(97), 5]) {
    await assert.rejects(client.manifest({revision}),{code:'invalid_revision'});
    await assert.rejects(client.dashboard({revision}),{code:'invalid_revision'});
  }
  assert.equal(requests,0);
});
test('new revision-pinned searches discard cursors from the previous query after success', async () => {
  const client={packages:async()=>dto('one','new-next')};const session=new CatalogSession(client);
  session.history.set(1,'old-filter-cursor');session.history.set(2,'old-deep-cursor');
  await session.search({q:'new'}, {revision:'one'});
  assert.equal(session.history.has(1),false);assert.equal(session.history.has(2),false);
  assert.equal(session.snapshot().canNext,true);assert.equal(session.snapshot().canPrevious,false);
});

test('empty or invalid explicitly requested versions do not fall back to canonical latest', async () => {
  let requests=0;const client=new Client(base,{fetchImpl:async()=>{requests++;return response({...dto(),id:'plugin.alpha',canonicalRecord:{version:'2.0.0'}});}});
  for (const version of ['', false, 123, 'x'.repeat(1025)]) await assert.rejects(client.detail('plugin.alpha',{version}),{code:'invalid_query'});
  assert.equal(requests,0);
});
function discoveryDto(revision = 'one', nextCursor = null) {
  const generatedAt = '2026-10-12T00:00:00.000000Z', observedAt = '2026-10-11T23:00:00.000000Z';
  const item = {id:'github-repository:123',scope:'discovery',kind:'repository',name:'owner/repo',type:null,
    firstSeenAt:observedAt,lastSeenAt:observedAt,agents:[],issues:[],declaredMetadata:null,manifestEvidence:null,
    reviewStatus:'unreviewed',classification:'discovered',repository:{githubRepoId:123,fullName:'owner/repo',url:'https://github.com/owner/repo'},
    sourceEvidence:[{sourceId:'github-search:'+'b'.repeat(64),kind:'github_search',url:'https://api.github.com/search/repositories?q=topic%3Amcp-server',evidenceHash:'a'.repeat(64),observedAt,checkedAt:observedAt}],
    sourceCount:1,evidenceTruncated:false,freshness:{stale:false,lastObservationAt:observedAt,staleAfterSeconds:86400}};
  return {revision,generatedAt,scope:'discovery',items:[item],nextCursor,
    discoveryCoverage:{policyVersion:'discovery-evidence-v1',asOf:generatedAt,coverage:'not_guaranteed',adoption:'review_required',repositories:1,manifests:0,sources:1},
    freshnessSummary:{stale:false,complete:false}};
}

test('discovery defaults to first_seen and never silently requests canonical metric sorts', () => {
  assert.deepEqual(normalizeFilters({scope:'discovery'}),{limit:20,sort:'first_seen',scope:'discovery'});
  assert.equal(normalizeFilters({}).sort,'canonical_updated');
  for(const value of [{scope:'unknown'}, {scope:''}, {scope:'discovery',sort:'canonical_updated'}, {scope:'discovery',sort:'stars'}, {scope:'discovery',sort:'downloads'}, {scope:'discovery',sort:'relevance'}]) assert.throws(()=>normalizeFilters(value),ApiError);
});
test('discovery list and detail preserve null type and send read-only encoded routes', async () => {
  const requests=[];
  const client=new Client(base,{fetchImpl:async(url,options)=>{
    requests.push({url:new URL(url),options});const value=discoveryDto();
    return response(url.includes('/discoveries/') ? {...value,item:value.items[0]} : value);
  }});
  const list=await client.packages(normalizeFilters({scope:'discovery'}),{revision:'one'});
  const detail=await client.discovery('github-repository:123',{revision:'one'});
  assert.equal(list.items[0].type,null);assert.deepEqual(list.items[0],detail.item);
  assert.equal(requests[0].url.searchParams.get('scope'),'discovery');
  assert.equal(requests[0].url.searchParams.get('sort'),'first_seen');
  assert.equal(requests[1].url.pathname,'/project/api/v1/discoveries/github-repository%3A123');
  assert.equal(requests[1].url.searchParams.get('revision'),'one');
  assert.equal(requests[1].options.credentials,'omit');
});
test('cross-scope responses and invented repository profile claims are rejected', async () => {
  const malformed=[];
  for(const change of [v=>v.scope='catalog',v=>v.items[0].type='plugin',v=>v.items[0].agents=['claude-code'],v=>v.items[0].repository.url='https://private.test',v=>v.items[0].reviewStatus='recommended',v=>v.items[0].sourceEvidence[0].url='https://user:secret@github.com/x',v=>v.items[0].sourceEvidence[0].evidenceHash='bad',v=>v.discoveryCoverage.coverage='global',v=>v.items[0].freshness.lastObservationAt='future',v=>v.items[0].sourceCount=0]) {
    const value=discoveryDto();change(value);malformed.push(value);
  }
  for(const body of malformed) {
    const client=new Client(base,{fetchImpl:async()=>response(body)});
    await assert.rejects(client.packages(normalizeFilters({scope:'discovery'})),{code:'invalid_response'});
  }
  const canonical=new Client(base,{fetchImpl:async()=>response(discoveryDto())});
  await assert.rejects(canonical.packages(normalizeFilters()),{code:'invalid_response'});
});
test('manifest discovery validates explicit profile and fixed declaration provenance', async () => {
  const value=discoveryDto();value.discoveryCoverage.manifests=1;value.discoveryCoverage.repositories=0;
  const item=value.items[0];item.id='manifest:'+'f'.repeat(64);item.kind='manifest';item.name='hello';item.type='plugin';item.agents=['claude-code'];item.reviewStatus='needs_metadata';item.classification='declared_profile';item.declaredMetadata={name:'hello',description:null,version:null,license:null};
  const url='https://github.com/owner/repo/blob/'+'c'.repeat(40)+'/.claude-plugin/plugin.json';
  item.manifestEvidence={path:'.claude-plugin/plugin.json',format:'claude_plugin',agentAssociation:'claude-code',commitSha:'c'.repeat(40),blobSha:'d'.repeat(40),contentSha256:'a'.repeat(64),url,association:'explicit-profile-not-compatibility-verification'};
  Object.assign(item.sourceEvidence[0],{sourceId:'github.com',kind:'manifest_declaration',url});
  const client=new Client(base,{fetchImpl:async()=>response({...value,item})});
  assert.equal((await client.discovery(item.id)).item.declaredMetadata.version,null);
  for(const change of [v=>v.item.manifestEvidence.path='../plugin.json',v=>v.item.manifestEvidence.commitSha='bad',v=>v.item.agents=['other'],v=>v.item.reviewStatus='adopted',v=>v.item.sourceEvidence[0].url='https://github.com/owner/repo/blob/main/plugin.json']) {
    const body=structuredClone({...value,item});change(body);
    await assert.rejects(new Client(base,{fetchImpl:async()=>response(body)}).discovery(item.id),{code:'invalid_response'});
  }
});
test('discovery details preserve precise errors and explicitly requested revision', async () => {
  let requests=0;
  const fail=new Client(base,{fetchImpl:async()=>{requests++;return response({error:{code:'discovery_unavailable'},requestId:'r1'},503);}});
  await assert.rejects(fail.discovery('github-repository:123'),{code:'discovery_unavailable',status:503});
  await assert.rejects(fail.discovery('bad'),{code:'invalid_query'});assert.equal(requests,1);
  const drift=discoveryDto('two');
  await assert.rejects(new Client(base,{fetchImpl:async()=>response({...drift,item:drift.items[0]})}).discovery('github-repository:123',{revision:'one'}),{code:'revision_mismatch'});
});
test('discovery session pages remain on one revision and clear cursor history when scope changes', async () => {
  const requests=[];
  const client=new Client(base,{fetchImpl:async(url)=>{
    const query=new URL(url).searchParams;requests.push(query);
    return response(query.get('scope')==='discovery'?discoveryDto('one',query.has('cursor')?null:'candidate-next'):dto());
  }});
  const session=new CatalogSession(client);
  await session.search({scope:'discovery'});await session.move(1);
  assert.equal(requests[1].get('revision'),'one');assert.equal(requests[1].get('cursor'),'candidate-next');
  await session.search({scope:'catalog'});
  assert.equal(session.data.items[0].type,'plugin');assert.equal(session.history.has(1),false);
  session.dispose();
});
test('SQLite-generated closed-loop discovery DTO validates in the frontend transport', {skip:!process.env.FORGE_DISCOVERY_FIXTURE}, async () => {
  const body=JSON.parse(require('node:fs').readFileSync(process.env.FORGE_DISCOVERY_FIXTURE,'utf8'));
  const client=new Client(base,{fetchImpl:async()=>response(body)});
  const list=await client.packages(normalizeFilters({scope:'discovery'}));
  assert.equal(list.discoveryCoverage.repositories,2);assert.equal(list.discoveryCoverage.manifests,1);
});

test('facet metadata uses the requested scope and rejects cross-scope Agent options', async () => {
  let url;
  const body={revision:'one',generatedAt:'2026-10-12T00:00:00Z',scope:'discovery',agents:['claude-code'],types:[{value:'plugin',count:1}],facets:{},customFacets:{},subtypes:[]};
  const client=new Client(base,{fetchImpl:async(value)=>{url=new URL(value);return response(body);}});
  assert.deepEqual((await client.facets('one',{scope:'discovery'})).agents,['claude-code']);
  assert.equal(url.searchParams.get('scope'),'discovery');
  assert.equal(url.searchParams.get('revision'),'one');
  for(const edit of [v=>v.scope='catalog',v=>v.types[0].count=-1,v=>v.types[0].value='mcp',v=>v.agents=[null],v=>v.facets={capabilities:[{value:'invented',count:1}]}]) {
    const invalid=structuredClone(body);edit(invalid);
    await assert.rejects(new Client(base,{fetchImpl:async()=>response(invalid)}).facets('one',{scope:'discovery'}),{code:'invalid_response'});
  }
  await assert.rejects(client.facets('one',{scope:'unknown'}),{code:'scope_unavailable'});
});
const recommendationFixture = require('./fixtures/recommendation-query.json');
const recommendationClient = (body, requests = []) => new Client(base, { fetchImpl: async (url, options) => {
  requests.push({ url: new URL(url), options }); return response(body);
}});
test('SQLite recommendations preserve exact Unicode identity/version and omit credentials', async () => {
  const body = recommendationFixture.package, requests = [];
  const result = await recommendationClient(body, requests).recommendations(body.id, { version: body.selectedVersion, revision: body.revision });
  assert.deepEqual(result, body);
  assert.equal(requests[0].url.pathname, '/project/api/v1/packages/' + encodeURIComponent(body.id) + '/recommendations');
  assert.equal(requests[0].url.searchParams.get('version'), '1.0.0');
  assert.equal(requests[0].options.credentials, 'omit');
  assert.equal(result.items[0].sourceRank, null);
  assert.equal(result.items[0].packageAssociation.endorsement, 'not-inferred');
});
test('SQLite source directory and source-scoped member browsing share a revision', async () => {
  const sources = recommendationFixture.sources, global = recommendationFixture.global, requests = [];
  assert.deepEqual(await recommendationClient(sources).recommendationSources({ revision: sources.revision }), sources);
  assert.deepEqual(await recommendationClient(global, requests).recommendations(null, { source: global.sourceId, revision: global.revision }), global);
  assert.equal(requests[0].url.pathname, '/project/api/v1/recommendations');
  assert.equal(requests[0].url.searchParams.get('source'), global.sourceId);
  assert.equal(global.items[0].packageAssociation, null);
});
test('SQLite withdrawal proof and truncated citations remain explicit', async () => {
  for (const key of ['withdrawn', 'bounded']) {
    const body = recommendationFixture[key];
    const value = await recommendationClient(body).recommendations(body.id, { version: body.selectedVersion, status: body.statusFilter, revision: body.revision });
    assert.deepEqual(value, body);
  }
  assert.equal(recommendationFixture.withdrawn.items[0].status, 'withdrawn');
  assert.ok(recommendationFixture.withdrawn.items[0].previousSnapshot);
  assert.equal(recommendationFixture.bounded.items[0].citationCount, 25);
  assert.equal(recommendationFixture.bounded.items[0].citations.length, 20);
  assert.equal(recommendationFixture.bounded.items[0].citationsTruncated, true);
});
test('recommendations reject invented rank, endorsement, citation or snapshot proof', async () => {
  const edits = [
    v => v.items[0].sourceRank = 1,
    v => v.items[0].citations[0].url = 'javascript:alert(1)',
    v => v.items[0].citations[0].url = 'https://github.com/wrong/target',
    v => v.items[0].packageAssociation.assertions[0].url = 'https://user:pass@github.com/o/alpha',
    v => v.selectedVersion = null,
    v => v.items[0].endorsement = 'package-endorsement',
    v => v.items[0].packageAssociation.endorsement = 'recommended',
    v => v.items[0].packageAssociation.canonicalHash = 'wrong',
    v => v.items[0].snapshot.contentSha256 = 'wrong',
    v => v.items[0].snapshot.steps[0].url = 'https://user:pass@api.github.com/repos/x/y',
    v => v.items[0].targetUrl = 'javascript:alert(1)',
    v => v.items[0].citations[0].line = v.items[0].snapshot.scope.endLine,
    v => v.items[0].previousSnapshotId = 'invented',
    v => v.items[0].source.id = 'other',
    v => v.items[0].reason = 'recommended',
    v => v.items[0].freshness.stale = null,
    v => v.recommendationCoverage.rankPolicy = 'best-first',
    v => v.order = 'source_rank',
    v => v.availability = 'recommended',
  ];
  for (const edit of edits) {
    const body = structuredClone(recommendationFixture.package); edit(body);
    await assert.rejects(recommendationClient(body).recommendations(body.id, { version: '1.0.0', revision: body.revision }), { code: 'invalid_response' });
  }
  const withdrawn = structuredClone(recommendationFixture.withdrawn); withdrawn.items[0].previousSnapshot = null; withdrawn.items[0].previousSnapshotId = null;
  await assert.rejects(recommendationClient(withdrawn).recommendations(withdrawn.id, { status: 'withdrawn' }), { code: 'invalid_response' });
});
test('recommendations reject source/status/version fallback and mismatched revisions', async () => {
  const body = recommendationFixture.package;
  await assert.rejects(recommendationClient(body).recommendations('wrong'), { code: 'invalid_response' });
  await assert.rejects(recommendationClient(body).recommendations(body.id, { version: 'missing' }), { code: 'invalid_response' });
  await assert.rejects(recommendationClient(body).recommendations(body.id, { revision: 'wrong' }), { code: 'revision_mismatch' });
  await assert.rejects(recommendationClient(body).recommendations(body.id, { status: 'withdrawn' }), { code: 'invalid_response' });
  const other = structuredClone(body); other.sourceId = 'curated-source:' + 'a'.repeat(64);
  await assert.rejects(recommendationClient(other).recommendations(body.id, { source: other.sourceId }), { code: 'invalid_response' });
});
test('invalid recommendation requests are rejected before transport', async () => {
  const requests = [], client = recommendationClient(recommendationFixture.package, requests);
  for (const [id, opts] of [[null, {}], [null, { source: 'wrong' }], [null, { source: recommendationFixture.global.sourceId, version: '1.0' }], ['id', { status: 'rank' }], ['id', { limit: 101 }], ['id', { limit: true }], ['id', { cursor: '' }]]) {
    await assert.rejects(client.recommendations(id, opts), { code: 'invalid_query' });
  }
  for (const opts of [{ limit: 0 }, { cursor: '' }]) await assert.rejects(client.recommendationSources(opts), { code: 'invalid_query' });
  assert.equal(requests.length, 0);
});
const sourceFilterFixture=require('./fixtures/recommendation-filter-query.json');
test('canonical source normalization validates identity and rejects discovery scope',()=>{
  const id=sourceFilterFixture.filtered.recommendationFilter.sourceId;
  assert.equal(normalizeFilters({recommendationSource:id,sort:'name'}).recommendationSource,id);
  for(const value of ['','wrong','curated-source:'+'F'.repeat(64),true])assert.throws(()=>normalizeFilters({recommendationSource:value}),{code:'invalid_query'});
  assert.throws(()=>normalizeFilters({scope:'discovery',recommendationSource:id}),{code:'invalid_query'});
  assert.throws(()=>normalizeFilters({recommendationSource:id,sort:'source_rank'}),{code:'sort_unavailable'});
});
test('SQLite full-catalog source filter DTO carries exact version/hash and binds request source',async()=>{
  const body=sourceFilterFixture.filtered,requests=[];
  const client=recommendationClient(body,requests);
  const value=await client.packages(normalizeFilters({recommendationSource:body.recommendationFilter.sourceId,sort:'name'}),{revision:body.revision});
  assert.deepEqual(value,body);assert.equal(requests[0].url.searchParams.get('recommendationSource'),body.recommendationFilter.sourceId);
  assert.equal(requests[0].options.credentials,'omit');assert.equal(value.items[0].recommendationAssociation.version,value.items[0].canonicalLatest);
  assert.equal(value.items[0].recommendationAssociation.endorsement,'not-inferred');assert.equal(value.recommendationFilter.sourceRank,null);
});
test('source-filter transport rejects silent unfiltered fallback and invented rank or endorsement',async()=>{
  const edits=[v=>delete v.recommendationFilter,v=>v.recommendationFilter.sourceId='curated-source:'+'a'.repeat(64),v=>v.recommendationFilteringAvailable=false,
    v=>v.recommendationFilter.sourceRank=1,v=>v.recommendationFilter.status='withdrawn',v=>v.recommendationFilter.canonicalVersionPolicy='any-version',
    v=>v.recommendationFilter.endorsement='recommended',v=>v.recommendationFilter.freshness.stale=null,
    v=>delete v.items[0].recommendationAssociation,v=>v.items[0].recommendationAssociation.version='wrong',
    v=>v.items[0].recommendationAssociation.canonicalHash=['a'.repeat(64)],v=>v.items[0].recommendationAssociation.sourceRank=1,
    v=>v.items[0].recommendationAssociation.sourceId='other',v=>v.items[0].recommendationAssociation.status='withdrawn',
    v=>v.items[0].recommendationAssociation.endorsement='recommended'];
  const filters=normalizeFilters({recommendationSource:sourceFilterFixture.filtered.recommendationFilter.sourceId,sort:'name'});
  for(const edit of edits){const body=structuredClone(sourceFilterFixture.filtered);edit(body);await assert.rejects(recommendationClient(body).packages(filters),{code:'invalid_response'});}
  await assert.rejects(recommendationClient(sourceFilterFixture.plain).packages(filters),{code:'invalid_response'});
});
test('unfiltered catalog rejects an unsolicited source filter',async()=>{
  await assert.rejects(recommendationClient(sourceFilterFixture.filtered).packages(normalizeFilters()),{code:'invalid_response'});
  assert.deepEqual(await recommendationClient(sourceFilterFixture.plain).packages(normalizeFilters({sort:'name'})),sourceFilterFixture.plain);
});
const reviewFixture=require('./fixtures/candidate-review-query.json');
test('metadata triage filter normalization is candidate-only and never claims approval',()=>{
  assert.equal(normalizeFilters({scope:'discovery',reviewDisposition:'rejected'}).reviewDisposition,'rejected');
  for(const input of [{reviewDisposition:'rejected'},{scope:'discovery',reviewDisposition:'approved'},{scope:'discovery',reviewDisposition:''}])assert.throws(()=>normalizeFilters(input),{code:'invalid_query'});
});
test('SQLite metadata review DTO keeps automatic classification and local triage separate',async()=>{
  const body=reviewFixture.filtered,requests=[];
  const value=await recommendationClient(body,requests).packages(normalizeFilters({scope:'discovery',reviewDisposition:'rejected'}),{revision:body.revision});
  assert.equal(value.items[0].reviewStatus,'unreviewed');assert.equal(value.items[0].metadataReview.status,'rejected');
  assert.equal(requests[0].url.searchParams.get('reviewDisposition'),'rejected');assert.equal(requests[0].options.credentials,'omit');
  for(const key of ['detail','superseded']){
    const dto=reviewFixture[key];assert.deepEqual(await recommendationClient(dto).discovery(dto.item.id,{revision:dto.revision}),dto);
  }
  assert.equal(reviewFixture.superseded.item.metadataReview.status,'superseded');
});
test('review transport rejects authority, private labels, false state and cutoff mutation',async()=>{
  const edits=[v=>v.items[0].metadataReview.status='approved',v=>v.items[0].metadataReview.authority='verified-safe',
    v=>v.items[0].metadataReview.operatorLabel='secret',v=>v.items[0].metadataReview.evidenceMatches=false,
    v=>v.items[0].metadataReview.history[0].reasonCode='quality_score',v=>v.items[0].metadataReview.history[0].candidateId='github-repository:999',
    v=>v.items[0].metadataReview.history[0].reviewedAt='2099-01-01T00:00:00Z',v=>v.items[0].metadataReview.historyCount=21,
    v=>v.items[0].metadataReview.history[0].operatorLabel='secret',v=>delete v.items[0].metadataReview,
    v=>v.discoveryReviewCoverage.authority='approved',v=>v.reviewDisposition='pending'];
  for(const edit of edits){const body=structuredClone(reviewFixture.filtered);edit(body);await assert.rejects(recommendationClient(body).packages(normalizeFilters({scope:'discovery',reviewDisposition:'rejected'})),{code:'invalid_response'});}
  await assert.rejects(recommendationClient(reviewFixture.filtered).packages(normalizeFilters({scope:'discovery'})),{code:'invalid_response'});
  await assert.rejects(recommendationClient(reviewFixture.list).packages(normalizeFilters({scope:'discovery',reviewDisposition:'rejected'})),{code:'invalid_response'});
});

function collectionControlDto(){
  const target={kind:"provider",provider:"github.com"},policyVersion="source-collection-controls-v1",authority="local-worker-collection-control";
  const event={id:"a".repeat(64),sequence:1,targetId:"collection-source:"+"b".repeat(64),target,action:"disable",reasonCode:"operator_collection_pause",recordedAt:"2026-10-12T00:00:00.000000Z",previousId:null,policyVersion,authority};
  return {revision:"one",generatedAt:"2026-10-12T00:00:01.000000Z",sourceControlCoverage:{policyVersion,authority,asOf:"2026-10-12T00:00:01.000000Z",events:1,targets:1,disabledTargets:1,enabledTargets:0},
    items:[{id:event.targetId,target,collectionState:"disabled",policyVersion,authority,latestEvent:event,history:[event],historyCount:1,historyTruncated:false}],nextCursor:null};
}
test('collection controls bind GET revision, cursor and count without adding writes',async()=>{
  let url,options;const body=collectionControlDto();const client=new Client(base,{fetchImpl:async(value,config)=>{url=new URL(value);options=config;return response(body);}});
  assert.deepEqual(await client.sourceControls({revision:'one',limit:20}),body);
  assert.equal(url.pathname,'/project/api/v1/source-controls');assert.equal(url.searchParams.get('revision'),'one');assert.equal(url.searchParams.get('limit'),'20');assert.equal(options.credentials,'omit');
  for(const limit of [0,101,true,1.1])await assert.rejects(client.sourceControls({limit}),e=>e.code==='invalid_query');
  await assert.rejects(client.sourceControls({revision:'two'}),e=>e.code==='revision_mismatch');
});
test('collection controls reject private fields, inconsistent totals, history and claims',async()=>{
  const edits=[
    d=>d.items[0].operatorLabel='private',d=>d.items[0].history[0].operatorLabel='private',d=>d.items[0].target.rating='trusted',
    d=>d.items[0].collectionState='enabled',d=>d.items[0].history[0].recordedAt='2026-10-13T00:00:00.000000Z',
    d=>d.items[0].history[0].recordedAt='2026-02-30T00:00:00.000000Z',d=>d.items[0].historyCount=2,
    d=>d.items[0].historyTruncated=true,d=>d.items[0].history[0].previousId='c'.repeat(64),
    d=>d.items[0].history[0].action='approve',d=>d.sourceControlCoverage.disabledTargets=0,
    d=>d.sourceControlCoverage.events=0,d=>d.sourceControlCoverage.asOf='wrong',d=>d.items.push(d.items[0]),
    d=>d.items[0].target={kind:'github_repository',repositoryId:9007199254740992},
    d=>d.items[0].target={kind:'github_repository_name',fullName:'example/repo extra'},
    d=>d.items[0].target={kind:'curated_file',repositoryId:123,path:'../README.md'},
    d=>d.items[0].target={kind:'constructor'},d=>d.items[0].target.kind=['provider'],d=>d.items[0].history[0].action=['disable'],d=>d.items=[null],d=>d.nextCursor=42,
  ];
  for(const edit of edits){const body=structuredClone(collectionControlDto());edit(body);const client=new Client(base,{fetchImpl:async()=>response(body)});await assert.rejects(client.sourceControls({revision:'one'}),e=>e.code==='invalid_response');}
});
test('collection control manifest feature is explicit while old manifests remain readable',async()=>{
  const body=collectionControlDto();body.features={sourceControls:true};const client=new Client(base,{fetchImpl:async()=>response(body)});
  await client.manifest({revision:'one'});
  for(const edit of [d=>d.features.sourceControls=false,d=>d.features.sourceControls='true',d=>delete d.features.sourceControls,d=>delete d.sourceControlCoverage]){
    const value=structuredClone(body);edit(value);await assert.rejects(new Client(base,{fetchImpl:async()=>response(value)}).manifest(),e=>e.code==='invalid_response');
  }
  const legacy=dto();assert.deepEqual(await new Client(base,{fetchImpl:async()=>response(legacy)}).manifest(),legacy);
});
test('SQLite collection control fixture validates all target types and truncated history', {skip:!process.env.FORGE_SOURCE_CONTROL_FIXTURE},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(process.env.FORGE_SOURCE_CONTROL_FIXTURE,'utf8'));
  const cases=fixture.cases.filter(item=>item.path==='/api/v1/source-controls');assert.equal(cases.length,4);
  for(const item of cases){const client=new Client(base,{fetchImpl:async()=>response(item.body)});const value=await client.sourceControls({revision:item.revision,cursor:item.parameters.cursor||null});assert.equal(value.sourceControlCoverage.targets,23);}
});

const releaseFixturePath=process.env.FORGE_RELEASE_FIXTURE;
function actualReleaseResponse(){
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseFixturePath,'utf8'));
  return fixture.cases.find(item=>item.revision==='release-one'&&item.path==='/api/v1/packages/plugin.beta/releases').body;
}
test('SQLite Release responses validate every page and preserve exact decimal counters', {skip:!releaseFixturePath},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseFixturePath,'utf8'));
  const cases=fixture.cases.filter(item=>item.path==='/api/v1/releases'||item.path.endsWith('/releases'));
  assert.ok(cases.length>5);
  for(const item of cases){
    const id=item.path==='/api/v1/releases'?null:item.path.slice('/api/v1/packages/'.length,-'/releases'.length);
    const client=new Client(base,{fetchImpl:async()=>response(item.body)});
    await client.releases(id,{revision:item.revision,version:item.parameters.version||null,cursor:item.parameters.cursor||null});
  }
  assert.equal(actualReleaseResponse().items[0].release.assets[0].downloadCount,'0');
});
test('Release client binds exact version/revision and never posts credentials', {skip:!releaseFixturePath},async()=>{
  let called;const body=actualReleaseResponse();const client=new Client(base,{fetchImpl:async(url,options)=>{called={url:new URL(url),options};return response(body);}});
  await client.releases('plugin.beta',{revision:'release-one',version:'1.0.0'});
  assert.equal(called.url.pathname,'/project/api/v1/packages/plugin.beta/releases');assert.equal(called.url.searchParams.get('version'),'1.0.0');assert.equal(called.options.credentials,'omit');
  await assert.rejects(client.releases('plugin.beta',{revision:'release-two'}),error=>error.code==='revision_mismatch');
  for(const input of [{version:'1.0.0'},{repositoryId:'0123'},{limit:101},{association:'trusted'}])await assert.rejects(client.releases(null,input),error=>error.code==='invalid_query');
});
test('Release response rejects made-up associations, precision loss, private fields and deletion claims', {skip:!releaseFixturePath},async()=>{
  const edits=[
    d=>d.items[0].release.assets[0].downloadCount=0,d=>d.items[0].release.assets[0].downloadCount='9223372036854775808',
    d=>d.items[0].release.assets[0].unit='users',d=>d.items[0].release.assets[0].scope='npm_package',
    d=>d.items[0].release.assets[0].browser_download_url='https://example.test/plugin.zip',d=>d.items[0].release.assets[0].counterIdentity='combined-downloads',
    d=>d.items[0].release.author='private',d=>d.items[0].release.observation.credentialIdentity='private',
    d=>d.items[0].release.observation.endpoint='repositories/999/releases?per_page=100&page=1',
    d=>d.items[0].release.observation.observedAt='2099-01-01T00:00:00.000000Z',
    d=>d.items[0].association.version='2.0.0',d=>d.items[0].association.assertions[0].assetId='999',
    d=>d.items[0].association.assertions[0].tag='v2.0.0',d=>d.items[0].association.assertions[0].url='https://token@github.com/owner/repo/releases/download/v1.0.0/plugin.zip',
    d=>d.releaseCoverage.deletionInference='deleted',d=>d.releaseCoverage.scanStates.partial+=1,
    d=>d.repositoryChecks[0].latestTraversal.state='snapshot_complete',d=>d.repositoryChecksTruncated=true,
    d=>d.items.push(d.items[0]),d=>d.nextCursor=12,d=>d.availability='none',
  ];
  for(const edit of edits){const body=structuredClone(actualReleaseResponse());edit(body);await assert.rejects(new Client(base,{fetchImpl:async()=>response(body)}).releases('plugin.beta',{revision:'release-one',version:'1.0.0'}),error=>error.code==='invalid_response');}
});
test('Release feature/coverage is explicit and unavailable errors remain actionable', {skip:!releaseFixturePath},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseFixturePath,'utf8'));const body=fixture.cases.find(item=>item.path==='/api/v1/manifest').body;
  await new Client(base,{fetchImpl:async()=>response(body)}).manifest();
  for(const edit of [d=>delete d.features.releases,d=>d.features.releases=false,d=>delete d.releaseCoverage]){
    const value=structuredClone(body);edit(value);await assert.rejects(new Client(base,{fetchImpl:async()=>response(value)}).manifest(),error=>error.code==='invalid_response');
  }
  await assert.rejects(new Client(base,{fetchImpl:async()=>response({error:{code:'releases_unavailable'}},503)}).releases('plugin.beta'),error=>error.code==='releases_unavailable');
});

const releaseSortFixturePath=process.env.FORGE_RELEASE_SORT_FIXTURE;
test('explicit release order validates every SQLite page and exact detail', {skip:!releaseSortFixturePath},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseSortFixturePath,'utf8'));
  let count=0;
  for(const entry of fixture.cases){
    const client=new Client(base,{fetchImpl:async()=>response(entry.body,entry.status)});
    if(entry.path==='/api/v1/manifest'){await client.manifest({revision:entry.body.revision});count++;}
    if(entry.path==='/api/v1/packages'&&entry.parameters.sort==='latest_release'){
      const result=await client.packages(normalizeFilters({sort:'latest_release',q:entry.parameters.q,agent:entry.parameters.agent,type:entry.parameters.type}),{revision:entry.body.revision,cursor:entry.parameters.cursor||null});
      assert.equal(result.sortPolicyVersion,'canonical-declared-release-time-v1');count++;
    }
    if(entry.path.endsWith('/detail')){await client.detail(entry.body.id,{revision:entry.body.revision,version:entry.parameters.version||null});count++;}
  }
  assert.ok(count>=20);
});
test('release sort refuses forged policy, context links, unknown eligibility and private evidence',{skip:!releaseSortFixturePath},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseSortFixturePath,'utf8'));
  const entry=fixture.cases.find(e=>e.path==='/api/v1/packages'&&e.parameters.sort==='latest_release'&&e.body.items.some(i=>i.latestRelease.status==='known'));
  const mutations=[v=>v.sort='canonical_updated',v=>v.sortPolicyVersion='invented',v=>delete v.releaseSortCoverage,
    v=>v.releaseSortCoverage.known++,v=>v.items[0].latestRelease.canonicalVersion='fake',
    v=>v.items[0].latestRelease.selected.associationKinds=['repository_context'],
    v=>v.items[0].latestRelease.selected.observation.credential='private',
    v=>v.items[0].latestRelease.publishedAt='2099-01-01T00:00:00.000000Z',
    v=>v.items[0].latestRelease.candidateCount++,v=>v.items[0].latestRelease.canonicalHash=null];
  for(const mutate of mutations){const value=structuredClone(entry.body);mutate(value);const client=new Client(base,{fetchImpl:async()=>response(value)});await assert.rejects(client.packages(normalizeFilters({sort:'latest_release'})),{code:'invalid_response'});}
  const unknown=fixture.cases.find(e=>e.path==='/api/v1/packages'&&e.parameters.sort==='latest_release'&&e.parameters.q==='gamma');
  const value=structuredClone(unknown.body);value.items[0].latestRelease.eligibleCount=1;
  await assert.rejects(new Client(base,{fetchImpl:async()=>response(value)}).packages(normalizeFilters({sort:'latest_release',q:'gamma'})),{code:'invalid_response'});
  assert.throws(()=>normalizeFilters({scope:'discovery',sort:'latest_release'}),{code:'sort_unavailable'});
});
test('release sort capability cannot be advertised without evidence or sorting policy',{skip:!releaseSortFixturePath},async()=>{
  const fixture=JSON.parse(require('node:fs').readFileSync(releaseSortFixturePath,'utf8'));
  const manifest=fixture.cases.find(e=>e.path==='/api/v1/manifest').body;
  for(const mutate of [v=>delete v.features.releaseSort,v=>v.features.releaseSort='yes',v=>v.features.releases=false,v=>v.releaseSortPolicyVersion='guessed',v=>v.availableSorts=v.availableSorts.filter(s=>s!=='latest_release')]){
    const value=structuredClone(manifest);mutate(value);await assert.rejects(new Client(base,{fetchImpl:async()=>response(value)}).manifest(),{code:'invalid_response'});
  }
});
