/* Local collection controls are independent from retained package and source facts. */
(function(){
  "use strict";
  window.ForgeSourceControls={create};
  function create({client,ui,escapeHTML:e,formatDate,host}){
    ui.addMessages({
      "zh-CN":{
        "ctrl.title":"采集控制记录","ctrl.notice":"这是运行者对采集的本地控制。停用不删除已收录事实，也不表示插件质量、安全或精选来源撤回。没有控制记录的范围默认开启；任一匹配范围停用都会阻止新采集，单项恢复不能覆盖其他停用范围。",
        "ctrl.counts":"已记录 {targets} 个范围：{disabled} 项停用，{enabled} 项恢复。","ctrl.revision":"本面板目录版本","ctrl.disabled":"本项停用","ctrl.enabled":"本项恢复","ctrl.provider":"提供方","ctrl.github_repository":"GitHub 仓库 ID","ctrl.github_repository_name":"GitHub 仓库名称","ctrl.github_topic":"GitHub Topic","ctrl.npm_package":"npm 包","ctrl.curated_file":"精选列表文件","ctrl.loading":"正在读取采集控制…","ctrl.error":"采集控制读取失败，已保留本页记录。","ctrl.expired":"这版控制记录已回收，请读取最新目录。","ctrl.unavailable":"这版目录未提供采集控制记录。","ctrl.retry":"重试","ctrl.latest":"读取最新控制记录","ctrl.prev":"上一页","ctrl.next":"下一页","ctrl.page":"第 {page} 页","ctrl.history":"控制历史","ctrl.truncated":"仅展开最近 20 条；共有 {count} 条记录。","ctrl.pause":"运行者暂停采集","ctrl.resume":"运行者恢复采集"
      },
      en:{
        "ctrl.title":"Collection controls","ctrl.notice":"These are local operator controls. Pausing retains indexed facts and does not establish package quality, safety or removal from a curated source. Unrecorded scopes default to enabled. Any matching pause blocks new collection; resuming one scope does not override other pauses.",
        "ctrl.counts":"{targets} recorded scopes: {disabled} paused, {enabled} resumed.","ctrl.revision":"Revision for this panel","ctrl.disabled":"This scope paused","ctrl.enabled":"This scope resumed","ctrl.provider":"Provider","ctrl.github_repository":"GitHub repository ID","ctrl.github_repository_name":"GitHub repository name","ctrl.github_topic":"GitHub Topic","ctrl.npm_package":"npm package","ctrl.curated_file":"Curated list file","ctrl.loading":"Loading collection controls…","ctrl.error":"Collection controls could not be read. Existing entries are retained.","ctrl.expired":"This control revision was retired. Read the latest revision.","ctrl.unavailable":"This revision has no recorded collection controls.","ctrl.retry":"Retry","ctrl.latest":"Read latest controls","ctrl.prev":"Previous page","ctrl.next":"Next page","ctrl.page":"Page {page}","ctrl.history":"Control history","ctrl.truncated":"Only the latest 20 of {count} events are expanded.","ctrl.pause":"Operator paused collection","ctrl.resume":"Operator resumed collection"
      }
    });
    let context=null,generation=0;
    const t=(key,params)=>ui.t(key,params);
    const label=target=>t("ctrl."+target.kind)+": "+(target.provider||target.fullName||target.topic||target.package||(String(target.repositoryId)+(target.path?" / "+target.path:"")));
    function stop(){generation++;if(context?.abort)context.abort.abort();}
    function draw(){
      if(!context){host.hidden=true;host.innerHTML="";return;}
      host.hidden=false;const state=context;
      const error=state.error?'<p class="control-error" role="alert">'+e(t(state.error.code==="cursor_expired"||state.error.code==="revision_unavailable"?"ctrl.expired":state.error.code==="source_controls_unavailable"?"ctrl.unavailable":"ctrl.error"))+'</p>':"";
      const items=state.data?.items||[];
      host.innerHTML='<details'+(state.open?' open':'')+'><summary><strong>'+e(t("ctrl.title"))+'</strong> <span class="muted">'+e(t("ctrl.counts",{targets:state.coverage.targets,disabled:state.coverage.disabledTargets,enabled:state.coverage.enabledTargets}))+'</span></summary><p class="control-notice">'+e(t("ctrl.notice"))+'</p><p class="control-revision">'+e(t("ctrl.revision"))+': <code>'+e(state.revision)+'</code> · '+e(formatDate(state.coverage.asOf))+'</p>'+error+(state.busy?'<p role="status">'+e(t("ctrl.loading"))+'</p>':'')+'<div class="control-items">'+items.map(item=>'<article class="control-item"><h3>'+e(label(item.target))+'</h3><p><strong>'+e(t(item.collectionState==="disabled"?"ctrl.disabled":"ctrl.enabled"))+'</strong> · '+e(formatDate(item.latestEvent.recordedAt))+'</p><code>'+e(item.id)+'</code><details><summary>'+e(t("ctrl.history"))+'</summary>'+ (item.historyTruncated?'<p>'+e(t("ctrl.truncated",{count:item.historyCount}))+'</p>':'')+'<ol>'+item.history.map(event=>'<li>'+e(t(event.action==="disable"?"ctrl.pause":"ctrl.resume"))+' · '+e(formatDate(event.recordedAt))+'<code>'+e(event.id)+'</code></li>').join('')+'</ol></details></article>').join('')+'</div><div class="control-actions"><button type="button" class="button button-quiet" data-control-prev '+(state.busy||state.page===0||!state.cursors.has(state.page-1)?'disabled':'')+'>'+e(t("ctrl.prev"))+'</button><output>'+e(t("ctrl.page",{page:state.page+1}))+'</output><button type="button" class="button button-quiet" data-control-next '+(state.busy||state.error||!state.data?.nextCursor?'disabled':'')+'>'+e(t("ctrl.next"))+'</button>'+(state.error?'<button type="button" class="button" data-control-retry '+(state.busy?'disabled':'')+'>'+e(t("ctrl.retry"))+'</button>':'')+'<button type="button" class="button button-quiet" data-control-latest '+(state.busy?'disabled':'')+'>'+e(t("ctrl.latest"))+'</button></div></details>';
      const details=host.firstElementChild;
      details.addEventListener('toggle',()=>{if(context!==state)return;state.open=details.open;if(state.open&&!state.data&&!state.busy&&!state.error)load(0,null);});
      host.querySelector('[data-control-next]').addEventListener('click',()=>load(state.page+1,state.data.nextCursor));
      host.querySelector('[data-control-prev]').addEventListener('click',()=>load(state.page-1,state.cursors.get(state.page-1)||null));
      host.querySelector('[data-control-retry]')?.addEventListener('click',()=>load(...state.pending));
      host.querySelector('[data-control-latest]').addEventListener('click',latest);
    }
    function pin(value,{explicit=false}={}){
      const coverage=value?.sourceControlCoverage;
      if(!coverage){stop();context=null;draw();return;}
      if(context?.manual&&!explicit&&context.catalogRevision===value.revision)return;
      if(context?.revision===value.revision)return;
      const catalogRevision=explicit?(context?.catalogRevision||context?.revision||value.revision):value.revision;
      const open=context?.open||false;stop();context={revision:value.revision,catalogRevision,manual:explicit,coverage,open,page:0,cursors:new Map([[0,null]]),data:null,error:null,busy:false,pending:[0,null]};
      draw();if(open)load(0,null);
    }
    async function load(page,cursor){
      const state=context;if(!state||state.busy||page<0)return;
      const token=++generation;if(state.abort)state.abort.abort();state.abort=new AbortController();state.pending=[page,cursor];state.busy=true;state.error=null;draw();
      try{
        const data=await client.sourceControls({revision:state.revision,cursor,limit:20,signal:state.abort.signal});
        if(context!==state||token!==generation)return;
        state.data=data;state.page=page;if(page===0)state.cursors.clear();state.cursors.set(page,cursor);while(state.cursors.size>64)state.cursors.delete(state.cursors.keys().next().value);
      }catch(error){if(context===state&&token===generation&&error.name!=="AbortError")state.error=error;}
      finally{if(context===state&&token===generation){state.busy=false;draw();}}
    }
    async function latest(){
      const state=context;if(!state||state.busy)return;
      const token=++generation;state.busy=true;state.error=null;draw();
      try{
        const manifest=await client.manifest();if(context!==state||token!==generation)return;
        const revisionChanged=manifest.revision!==state.revision;
        pin(manifest,{explicit:true});
        if(context===state&&!revisionChanged){state.busy=false;load(0,null);}
      }catch(error){if(context===state&&token===generation){state.busy=false;state.error=error;draw();}}
    }
    window.addEventListener('forge:localechange',draw);
    window.addEventListener('pagehide',()=>{stop();context=null;});
    return {pin};
  }
})();
