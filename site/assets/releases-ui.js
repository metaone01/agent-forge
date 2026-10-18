/* Release evidence stays separate from canonical latest, npm metrics and recommendations. */
(function(){
  "use strict";
  window.ForgeReleases={create};
  function create({client,ui,escapeHTML:e,formatDate}){
    ui.addMessages({
      "zh-CN":{
        "rel.title":"发行与资产观察","rel.notice":"仓库发行不等于插件版本。仅对规范分发声明的精确 tag / 资产 URL 建立关联，未验证安装或版本内容；资产累计下载请求不是使用人数，也不与 npm 下载相加。缺少页面或新列表未出现，不证明删除。",
        "rel.loading":"正在读取发行观察…","rel.error":"发行观察读取失败，保留已读取结果。","rel.expired":"这版发行观察已回收，请读取最新目录。","rel.unknown":"没有可明确关联的发行观察；不代表上游没有发行。","rel.version":"规范声明版本","rel.revision":"目录版本","rel.declared":"关联到规范分发声明","rel.context":"仅所属仓库上下文","rel.asset":"精确资产声明","rel.tag":"精确 tag 声明","rel.prerelease":"上游标为预发行","rel.published":"上游发布时间","rel.observed":"计数观察时间","rel.checked":"最近成功检查","rel.stale":"检查已过期","rel.fresh":"检查未过期","rel.assetName":"资产名","rel.downloads":"累计下载请求","rel.bytes":"字节","rel.moreAssets":"还有 {count} 个资产，完整数据可通过发行查询接口读取。","rel.evidence":"关联与证据","rel.field":"规范字段","rel.hash":"规范记录 hash","rel.source":"上游发行元数据页（可变）","rel.checks":"仓库遍历状态","rel.partial":"遍历不完整","rel.in_progress":"遍历尚未结束","rel.traversal_complete":"本次可变分页遍历结束，不是删除证明","rel.retry":"重试","rel.prev":"上一页","rel.next":"下一页","rel.page":"第 {page} 页"
      },
      en:{
        "rel.title":"Release and asset observations","rel.notice":"Repository releases are not plugin versions. Associations use exact tag or asset URLs declared by the canonical record; installation and version contents are unverified. Cumulative asset download requests are not people or npm downloads. Missing pages or absence from a later list do not prove deletion.",
        "rel.loading":"Loading release observations…","rel.error":"Release observations could not be read. Existing results are retained.","rel.expired":"This release revision was retired. Read the latest catalog.","rel.unknown":"No unambiguous release observation is associated. This does not establish the absence of upstream releases.","rel.version":"Canonical declaration version","rel.revision":"Catalog revision","rel.declared":"Associated canonical distribution declaration","rel.context":"Repository context only","rel.asset":"Exact asset declaration","rel.tag":"Exact tag declaration","rel.prerelease":"Upstream marks this as a prerelease","rel.published":"Upstream publication time","rel.observed":"Counter observation time","rel.checked":"Last successful check","rel.stale":"Check is stale","rel.fresh":"Check is not stale","rel.assetName":"Asset name","rel.downloads":"Cumulative download requests","rel.bytes":"Bytes","rel.moreAssets":"{count} more assets are available through the release query API.","rel.evidence":"Association and evidence","rel.field":"Canonical field","rel.hash":"Canonical record hash","rel.source":"Upstream release metadata page (mutable)","rel.checks":"Repository traversal state","rel.partial":"Partial traversal","rel.in_progress":"Traversal is in progress","rel.traversal_complete":"This mutable-page traversal ended; this is not deletion proof","rel.retry":"Retry","rel.prev":"Previous page","rel.next":"Next page","rel.page":"Page {page}"
      }
    });
    const t=(key,params)=>ui.t(key,params);let context=null,generation=0;
    function stop(){generation++;if(context?.abort)context.abort.abort();context=null;}
    function render(){
      const state=context;if(!state||!state.host.isConnected)return;
      const value=state.data,items=value?.items||[];
      state.host.innerHTML='<details'+(state.open?' open':'')+'><summary><strong>'+e(t('rel.title'))+'</strong></summary><p class="release-notice">'+e(t('rel.notice'))+'</p><p class="release-scope">'+e(t('rel.revision'))+': <code>'+e(state.revision)+'</code> · '+e(t('rel.version'))+': <code>'+e(state.version)+'</code></p>'+(state.busy?'<p role="status">'+e(t('rel.loading'))+'</p>':'')+(state.error?'<p role="alert">'+e(t(['cursor_expired','revision_unavailable'].includes(state.error.code)?'rel.expired':'rel.error'))+'</p>':'')+(value&&!items.length?'<p>'+e(t('rel.unknown'))+'</p>':'')+'<div class="release-results">'+items.map(item=>{
        const r=item.release,a=item.association,declared=a.assertions.some(proof=>proof.kind!=='repository_context');
        const assets=r.assets.slice(0,20);
        return '<article class="release-item"><h3>'+e(r.name||r.tagName)+'</h3><p class="release-association"><strong>'+e(t(declared?'rel.declared':'rel.context'))+'</strong> · <code>'+e(r.tagName)+'</code></p>'+(r.prerelease?'<p>'+e(t('rel.prerelease'))+'</p>':'')+'<p>'+e(t('rel.published'))+': '+e(formatDate(r.publishedAt))+'</p><p>'+e(t('rel.observed'))+': '+e(formatDate(r.observation.observedAt))+' · '+e(t('rel.checked'))+': '+e(formatDate(r.observation.checkedAt))+' · '+e(t(item.freshness.stale?'rel.stale':'rel.fresh'))+'</p>'+(assets.length?'<div class="release-assets"><table><thead><tr><th>'+e(t('rel.assetName'))+'</th><th>'+e(t('rel.downloads'))+'</th><th>'+e(t('rel.bytes'))+'</th></tr></thead><tbody>'+assets.map(asset=>'<tr><td>'+e(asset.name)+'</td><td>'+e(asset.downloadCount)+'</td><td>'+e(asset.sizeBytes)+'</td></tr>').join('')+'</tbody></table></div>':'')+(r.assets.length>20?'<p>'+e(t('rel.moreAssets',{count:r.assets.length-20}))+'</p>':'')+'<details><summary>'+e(t('rel.evidence'))+'</summary><p>'+e(t('rel.hash'))+': <code>'+e(a.canonicalHash)+'</code></p>'+a.assertions.map(proof=>'<p>'+e(t(proof.kind==='repository_context'?'rel.context':proof.kind==='exact_declared_asset'?'rel.asset':'rel.tag'))+' · '+e(t('rel.field'))+': <code>'+e(proof.field)+'</code><code>'+e(proof.url)+'</code></p>').join('')+'<p><a href="https://api.github.com/'+e(r.observation.endpoint)+'" target="_blank" rel="noreferrer">'+e(t('rel.source'))+'</a></p><code>'+e(r.observation.evidenceHash)+'</code></details></article>';
      }).join('')+'</div>'+(value?'<div class="release-checks"><h3>'+e(t('rel.checks'))+'</h3>'+value.repositoryChecks.map(check=>'<p><code>'+e(check.repositoryId)+'</code> · '+e(t('rel.'+check.latestTraversal.state))+' · '+e(formatDate(check.lastCheckedAt))+'</p>').join('')+'</div>':'')+'<div class="release-actions"><button type="button" class="button button-quiet" data-release-prev '+(state.busy||state.page===0||!state.cursors.has(state.page-1)?'disabled':'')+'>'+e(t('rel.prev'))+'</button><output>'+e(t('rel.page',{page:state.page+1}))+'</output><button type="button" class="button button-quiet" data-release-next '+(state.busy||state.error||!value?.nextCursor?'disabled':'')+'>'+e(t('rel.next'))+'</button>'+(state.error?'<button type="button" class="button" data-release-retry '+(state.busy?'disabled':'')+'>'+e(t('rel.retry'))+'</button>':'')+'</div></details>';
      const details=state.host.firstElementChild;
      details.addEventListener('toggle',()=>{if(context!==state)return;state.open=details.open;if(state.open&&!state.data&&!state.busy&&!state.error)load(0,null);});
      state.host.querySelector('[data-release-next]').addEventListener('click',()=>load(state.page+1,value.nextCursor));
      state.host.querySelector('[data-release-prev]').addEventListener('click',()=>load(state.page-1,state.cursors.get(state.page-1)));
      state.host.querySelector('[data-release-retry]')?.addEventListener('click',()=>load(...state.pending));
    }
    async function load(page,cursor){
      const state=context;if(!state||state.busy)return;const token=++generation;
      if(state.abort)state.abort.abort();state.abort=new AbortController();state.busy=true;state.error=null;state.pending=[page,cursor];render();
      try{
        const value=await client.releases(state.id,{version:state.version,revision:state.revision,cursor,limit:20,signal:state.abort.signal});
        if(context!==state||token!==generation)return;
        state.data=value;state.page=page;if(page===0)state.cursors.clear();state.cursors.set(page,cursor);while(state.cursors.size>64)state.cursors.delete(state.cursors.keys().next().value);
      }catch(error){if(context===state&&token===generation&&error.name!=='AbortError')state.error=error;}
      finally{if(context===state&&token===generation){state.busy=false;render();}}
    }
    function packagePanel(detail,host){
      const version=detail.canonicalRecord?.version;
      if(!version||!detail.releaseCoverage){stop();host.hidden=true;return;}
      if(context?.id===detail.id&&context.version===version&&context.revision===detail.revision){context.host=host;render();return;}
      stop();context={id:detail.id,version,revision:detail.revision,host,open:false,data:null,page:0,cursors:new Map([[0,null]]),pending:[0,null],busy:false,error:null};render();
    }
    window.addEventListener('forge:localechange',render);window.addEventListener('pagehide',stop);
    return {stop,packagePanel};
  }
})();
