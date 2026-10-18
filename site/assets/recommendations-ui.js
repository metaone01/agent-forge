/* Source-scoped curated metadata; exact versions and upstream ranks stay explicit. */
(function () {
  'use strict';
  window.ForgeRecommendations = { create };
  function create({client, ui, escapeHTML: e, formatDate, main}) {
    ui.addMessages({
      'zh-CN': { 'rec.catalog':'查看关联规范包', 'rec.title':'来源精选', 'rec.browse':'浏览精选来源', 'rec.notice':'来源精选成员关系不代表本站推荐、插件背书、质量或安全认证。原始排名未知；按来源与目标字母序展示。', 'rec.unknown':'当前范围没有可用的关联事实，不能据此判断全网没有推荐。', 'rec.unavailable':'这一版目录尚未提供来源精选查询。', 'rec.error':'查询失败，请重试。', 'rec.expired':'这一版目录已回收，请读取最新来源。', 'rec.latest':'读取最新来源', 'rec.all':'全部状态', 'rec.active':'仍在来源列表中', 'rec.withdrawn':'已从来源列表移除', 'rec.fresh':'来源检查未过期', 'rec.stale':'来源检查已过期', 'rec.rank':'原始排名：未知', 'rec.current':'当前完整快照', 'rec.previous':'前一完整快照', 'rec.context':'仓库上下文关联，不推断插件背书', 'rec.distribution':'精确分发声明链接关联，不推断插件背书', 'rec.version':'关联规范版本', 'rec.citations':'引用', 'rec.truncated':'另有 {count} 条引用未在此项展开。', 'rec.back':'返回目录', 'rec.loading':'正在查询来源精选…', 'rec.retry':'重试', 'rec.prev':'上一页', 'rec.next':'下一页', 'rec.page':'第 {page} 页', 'rec.history':'仅保留最近 64 页的访问游标。', 'rec.selection':'来源声明', 'rec.scope':'收录范围', 'rec.checked':'最后成功检查', 'rec.observed':'最近选取观察', 'rec.filter':'成员状态', 'rec.shared':'来源成员可能是仓库或路径，不代表已经收录或可安装的规范插件。' },
      en: { 'rec.catalog':'View associated canonical packages', 'rec.title':'Source curated lists', 'rec.browse':'Browse curated sources', 'rec.notice':'Source list membership is not a site recommendation, package endorsement, quality or safety certification. Upstream ranks are unknown; sources and targets use alphabetical order.', 'rec.unknown':'No associated facts are available in this scope. This does not establish the absence of recommendations elsewhere.', 'rec.unavailable':'This catalog revision has no curated-source query feature.', 'rec.error':'Query failed. Please retry.', 'rec.expired':'This revision was retired. Read the latest sources.', 'rec.latest':'Read latest sources', 'rec.all':'All statuses', 'rec.active':'Present in source list', 'rec.withdrawn':'Removed from source list', 'rec.fresh':'Source check is not stale', 'rec.stale':'Source check is stale', 'rec.rank':'Upstream rank: unknown', 'rec.current':'Current complete snapshot', 'rec.previous':'Previous complete snapshot', 'rec.context':'Repository context; no inferred package endorsement', 'rec.distribution':'Exact declared distribution link; no inferred endorsement', 'rec.version':'Associated canonical version', 'rec.citations':'Citations', 'rec.truncated':'{count} more citations are not expanded here.', 'rec.back':'Back to catalog', 'rec.loading':'Loading curated sources…', 'rec.retry':'Retry', 'rec.prev':'Previous page', 'rec.next':'Next page', 'rec.page':'Page {page}', 'rec.history':'Only the latest 64 visited page cursors are retained.', 'rec.selection':'Source statement', 'rec.scope':'Indexed scope', 'rec.checked':'Last successful check', 'rec.observed':'Latest selection observation', 'rec.filter':'Membership status', 'rec.shared':'A source member can be a repository or path. It is not necessarily a registered or installable package.' }
    });
    const t=(key,params)=>ui.t(key,params);
    let context=null, sequence=0;
    const link=(source=null,revision=null,status='all')=>{
      const params=new URLSearchParams();if(revision)params.set('revision',revision);if(status!=='all')params.set('status',status);
      return '#/recommendations'+(source?'/'+encodeURIComponent(source):'')+(params.size?'?'+params:'');
    };
    function stop(){++sequence;if(context?.abort)context.abort.abort();context=null;}
    function time(value){return e(formatDate(value));}
    function external(url,label){return '<a href="'+e(url)+'" target="_blank" rel="noreferrer">'+e(label)+'</a>';}
    function freshness(value){return '<p class="rec-freshness">'+e(t(value.stale?'rec.stale':'rec.fresh'))+' · '+e(t('rec.checked'))+': '+time(value.lastSuccessAt)+' · '+e(t('rec.observed'))+': '+time(value.lastObservationAt)+'</p>';}
    function proof(value,label){return '<details class="rec-proof"><summary>'+e(t(label))+'</summary>'+external(value.url,value.commitSha)+'<code>blob '+e(value.blobSha)+'</code><code>SHA-256 '+e(value.contentSha256)+'</code><p>'+time(value.selectionAt)+' → '+time(value.completedAt)+'</p></details>';}
    function item(value){
      const association=value.packageAssociation;
      const citations=value.citations.map(row=>'<li>'+external(row.url,row.section+' · L'+row.line)+'</li>').join('');
      return '<article class="rec-item" data-target="'+e(value.targetUrl)+'" data-membership="'+e(value.status)+'"><h3>'+external(value.targetUrl,value.name)+'</h3><p class="rec-membership">'+e(t('rec.'+value.status))+' · '+e(t('rec.rank'))+'</p><p><a class="rec-source-link" href="'+e(link(value.sourceId,context.revision))+'">'+e(value.source.labels.join(' · '))+'</a></p>'+freshness(value.freshness)+(association?'<p class="rec-association">'+e(t('rec.version'))+': '+e(association.version)+' · '+e(t(association.assertions.some(row=>row.kind==='distribution_declaration')?'rec.distribution':'rec.context'))+'</p><code>'+e(association.canonicalHash)+'</code>':'')+proof(value.snapshot,'rec.current')+(value.previousSnapshot?proof(value.previousSnapshot,'rec.previous'):'')+'<details class="rec-citations"><summary>'+e(t('rec.citations'))+' ('+value.citationCount+')</summary><ul>'+citations+'</ul>'+(value.citationsTruncated?'<p>'+e(t('rec.truncated',{count:value.citationCount-value.citations.length}))+'</p>':'')+'</details></article>';
    }
    function canonicalLink(source) {
      const url=new URL(location.href);url.hash="";url.search="";url.searchParams.set("recommendationSource",source);url.searchParams.set("revision",context.revision);url.searchParams.set("sort","name");return url.href;
    }
    function source(value){return '<article class="rec-item" data-source="'+e(value.id)+'"><h3><a href="'+e(link(value.id,context.revision))+'">'+e(value.labels.join(' · '))+'</a></h3><p><a class="rec-catalog-link" href="'+e(canonicalLink(value.id))+'">'+e(t('rec.catalog'))+'</a></p><p>'+e(t('rec.selection'))+': '+e(value.selectionStatement)+'</p><p>'+e(t('rec.scope'))+': '+e(value.path)+' · '+e(value.startHeading)+' → '+e(value.endHeading)+'</p>'+freshness(value.freshness)+'</article>';}
    function render(){
      const ctx=context;if(!ctx||!ctx.host.isConnected)return;
      let message=ctx.busy?t('rec.loading'):ctx.error?t(['cursor_expired','revision_unavailable'].includes(ctx.error.code)?'rec.expired':ctx.error.code==='recommendations_unavailable'?'rec.unavailable':'rec.error'):'';
      const values=ctx.data?.items||[];
      ctx.host.innerHTML=(ctx.kind==='package'?'':'<a class="back-link" href="#">'+e(t('rec.back'))+'</a>')+'<h2>'+e(t('rec.title'))+'</h2><p class="scope-notice">'+e(t('rec.notice'))+'</p><p><a class="rec-browse" href="'+e(link(null,ctx.revision))+'">'+e(t('rec.browse'))+'</a></p>'+(ctx.kind==='sources'?'': '<label>'+e(t('rec.filter'))+' <select id="rec-status">'+['all','active','withdrawn'].map(status=>'<option value="'+status+'"'+(status===ctx.status?' selected':'')+'>'+e(t('rec.'+status))+'</option>').join('')+'</select></label>')+'<p class="rec-revision">'+e(ctx.revision||'')+(ctx.version?' · '+e(t('rec.version'))+': '+e(ctx.version):'')+'</p>'+(ctx.kind==='members'?'<p>'+e(t('rec.shared'))+'</p>':'')+'<p id="rec-status-message" role="status">'+e(message)+'</p><button id="rec-retry" type="button" class="button"'+(!ctx.error?' hidden':'')+(ctx.busy?' disabled':'')+'>'+e(t('rec.retry'))+'</button>'+(['cursor_expired','revision_unavailable'].includes(ctx.error?.code)?'<a class="button rec-latest" href="'+e(link(ctx.source))+'">'+e(t('rec.latest'))+'</a>':'')+'<div class="rec-results" aria-busy="'+ctx.busy+'">'+values.map(ctx.kind==='sources'?source:item).join('')+'</div>'+(!ctx.busy&&!ctx.error&&!values.length?'<p class="rec-empty">'+e(t('rec.unknown'))+'</p>':'')+'<nav class="rec-pagination"><button id="rec-prev" type="button" class="button button-small button-quiet"'+(ctx.position===0||ctx.busy?' disabled':'')+'>'+e(t('rec.prev'))+'</button><output>'+e(t('rec.page',{page:ctx.dataPage||ctx.history[ctx.position].page}))+'</output><button id="rec-next" type="button" class="button button-small button-quiet"'+(!ctx.data?.nextCursor||ctx.busy||ctx.error?' disabled':'')+'>'+e(t('rec.next'))+'</button></nav><p class="muted">'+e(t('rec.history'))+'</p>';
      ctx.host.querySelector('#rec-retry').addEventListener('click',()=>load(ctx));
      ctx.host.querySelector('#rec-prev').addEventListener('click',()=>{ctx.position--;load(ctx);});
      ctx.host.querySelector('#rec-next').addEventListener('click',()=>{
        const entry={cursor:ctx.data.nextCursor,page:ctx.history[ctx.position].page+1};ctx.history.splice(ctx.position+1);ctx.history.push(entry);ctx.position++;
        if(ctx.history.length>64){ctx.history.shift();ctx.position--;}load(ctx);
      });
      const status=ctx.host.querySelector('#rec-status');if(status)status.addEventListener('change',()=>{
        if(ctx.kind==='members'){location.hash=link(ctx.source,ctx.revision,status.value);return;}
        ctx.status=status.value;ctx.history=[{cursor:null,page:1}];ctx.position=0;ctx.data=null;ctx.dataPage=null;load(ctx);
      });
      ui.apply(ctx.host);
    }
    async function load(ctx){
      const current=++sequence;if(ctx.abort)ctx.abort.abort();ctx.abort=new AbortController();ctx.busy=true;ctx.error=null;render();
      try{
        const options={revision:ctx.revision,cursor:ctx.history[ctx.position].cursor,limit:20,signal:ctx.abort.signal};
        const value=ctx.kind==='sources'?await client.recommendationSources(options):await client.recommendations(ctx.id,{...options,source:ctx.source,version:ctx.version,status:ctx.status});
        if(current!==sequence||context!==ctx)return;
        ctx.data=value;ctx.dataPage=ctx.history[ctx.position].page;ctx.revision=value.revision;
      }catch(error){if(current===sequence&&context===ctx&&error.name!=='AbortError')ctx.error=error;}
      finally{if(current===sequence&&context===ctx){ctx.busy=false;render();}}
    }
    function open(kind,{id=null,source=null,revision=null,version=null,status='all',host=main}={}){
      stop();context={kind,id,source,revision,version,status,host,data:null,error:null,busy:false,abort:null,history:[{cursor:null,page:1}],position:0};load(context);
    }
    function packagePanel(detail,host){
      const version=detail.canonicalRecord?.version;if(!version){host.textContent=t('rec.unknown');return;}
      if(context?.kind==='package'&&context.id===detail.id&&context.version===version&&context.revision===detail.revision){context.host=host;render();return;}
      open('package',{id:detail.id,revision:detail.revision,version,host});
    }
    window.addEventListener('forge:localechange',render);
    window.addEventListener('pagehide',stop);
    return {stop,open,packagePanel,link};
  }
})();
