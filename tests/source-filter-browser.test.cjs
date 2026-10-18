/* Real Edge acceptance against SQLite-generated, read-only response fixtures. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_SOURCE_FILTER_FIXTURE&&process.env.FORGE_SOURCE_FILTER_ARTIFACTS;

test('source filter UI: whole catalog, source options, exact version and revision recovery', {skip:!enabled,timeout:110000}, async()=>{
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_SOURCE_FILTER_FIXTURE,'utf8'));
  const artifacts=path.resolve(process.env.FORGE_SOURCE_FILTER_ARTIFACTS);fs.mkdirSync(artifacts,{recursive:true});
  const site=process.env.FORGE_SOURCE_FILTER_SITE?path.resolve(process.env.FORGE_SOURCE_FILTER_SITE):path.resolve(__dirname,'../site');
  const key=(revision,route,params)=>JSON.stringify([revision,route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(c=>[key(c.revision,c.path,c.parameters),c]));
  const requests=[],network=[],networkEvents=[],networkFailures=[],environmentBlockedRequests=[],exceptions=[],unmatched=[],checks=[];
  let current='filter-one',retired=false,failOptions=true;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length));
      const params=Object.fromEntries(url.searchParams),revision=params.revision||current;
      requests.push({route,params,revision,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      let value=cases.get(key(revision,route,params));
      if(retired&&params.revision==='filter-one')value={status:410,body:{error:{code:params.cursor?'cursor_expired':'revision_unavailable'},requestId:'retired'}};
      if(route==='/api/v1/recommendation-sources'&&failOptions){failOptions=false;value={status:503,body:{error:{code:'service_busy'},requestId:'source-retry'}};}
      if(!value){unmatched.push({route,params,revision});value={status:503,body:{error:{code:'fixture_unmatched'},requestId:'unmatched'}};}
      const respond=()=>{if(!res.destroyed)res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));};
      if(params.q==='slowterm')setTimeout(respond,1100);else respond();return;
    }
    let route;try{route=decodeURIComponent(url.pathname).replace(/^\/project\//,'');}catch(_){res.writeHead(400).end();return;}
    const file=path.resolve(site,route.endsWith('/')?route+'index.html':route);
    if(!file.startsWith(site+path.sep)){res.writeHead(403).end();return;}
    try{res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html','Set-Cookie':'fixture=present; Path=/'}).end(fs.readFileSync(file));}catch(_){res.writeHead(404).end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port+'/project/';
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'forge-source-filter-browser-'));
  const browser=spawn(process.env.FORGE_UI_BROWSER,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket,send,session,failure,diagnostics;
  try{
    let debug;
    for(let attempt=0;attempt<100;attempt++){try{debug=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split(/\r?\n/);break;}catch(_){await timers.setTimeout(100);}}
    assert.ok(debug,'isolated browser exposes debugging port');
    socket=new WebSocket('ws://127.0.0.1:'+debug[0]+debug[1]);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    let sequence=0;const pending=new Map();
    socket.addEventListener('message',event=>{
      const message=JSON.parse(event.data);
      if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);
      if(message.method==='Network.requestWillBeSent'){network.push(message.params.request.url);networkEvents.push(message.params);}
      if(message.method==='Network.loadingFailed')networkFailures.push(message.params);
      if(message.id&&pending.has(message.id)){const item=pending.get(message.id);pending.delete(message.id);clearTimeout(item.timer);message.error?item.reject(new Error(JSON.stringify(message.error))):item.resolve(message.result);}
    });
    send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timed out '+method));},10000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
    const target=await send('Target.createTarget',{url:'about:blank'});session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
    await send('Page.enable',{},session);await send('Runtime.enable',{},session);await send('Network.enable',{},session);
    // Block only the verified host-antivirus injection in this isolated browser target.
    await send('Network.setBlockedURLs',{urls:['*://me.kis.v2.scr.kaspersky-labs.com/*']},session);
    const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const until=async expression=>{const started=Date.now();while(Date.now()-started<8000){if(await evaluate(expression))return;await timers.setTimeout(50);}throw new Error('Timed out: '+expression);};
    const screenshot=async name=>{const result=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);fs.writeFileSync(path.join(artifacts,name),Buffer.from(result.data,'base64'));};
    const select=async(id,value)=>evaluate(`document.getElementById(${JSON.stringify(id)}).value=${JSON.stringify(value)};document.getElementById(${JSON.stringify(id)}).dispatchEvent(new Event('change',{bubbles:true}))`);
    const search=async value=>evaluate(`document.querySelector('#query').value=${JSON.stringify(value)};document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))`);
    const idle="document.querySelector('#results')?.getAttribute('aria-busy')==='false' && document.querySelector('#query-error').hidden";
    const change=async(id,value)=>evaluate(`document.getElementById(${JSON.stringify(id)}).value=${JSON.stringify(value)};document.getElementById(${JSON.stringify(id)}).dispatchEvent(new Event('change',{bubbles:true}))`);
    const catalogUrl=(revision='filter-one')=>base+'catalog/?recommendationSource='+encodeURIComponent(fixture.sourceId)+'&sort=name&revision='+revision;
    await send('Emulation.setDeviceMetricsOverride',{width:1365,height:950,deviceScaleFactor:1,mobile:false},session);
    await send('Page.navigate',{url:catalogUrl()},session);
    await until(idle+" && document.querySelectorAll('.result-card').length===20 && !document.querySelector('#retry-source-options').hidden");
    assert.equal(await evaluate("document.querySelector('#recommendation-source-filter').value"),fixture.sourceId);
    assert.ok((await evaluate("document.querySelector('#source-match-notice').textContent")).includes('原始排名未知'));
    assert.ok((await evaluate("document.querySelector('#source-match-notice').textContent")).includes('检查已过期'));
    assert.equal(await evaluate("document.querySelector('.result-card').dataset.discoveryId"),undefined);
    await evaluate("document.querySelector('#retry-source-options').click()");
    await until("document.querySelector('#retry-source-options').hidden && !document.querySelector('#more-source-options').hidden");
    assert.equal(await evaluate("document.querySelector('#recommendation-source-filter').value"),fixture.sourceId);
    await evaluate("document.querySelector('#more-source-options').click()");
    await until("document.querySelector('#more-source-options').hidden && document.querySelector('#recommendation-source-filter').options.length===22");
    assert.equal(await evaluate("document.querySelector('#recommendation-source-filter').value"),fixture.sourceId);
    checks.push('deep-linked source filter, independent option retry and 20+1 source paging preserve selection');
    const first=await evaluate("[...document.querySelectorAll('.result-link')].map(node=>node.getAttribute('href'))");
    await evaluate("document.querySelector('#next-page').click()");await until(idle+" && document.querySelectorAll('.result-card').length===12");
    const second=await evaluate("[...document.querySelectorAll('.result-link')].map(node=>node.getAttribute('href'))");
    assert.equal(new Set([...first,...second]).size,32);
    assert.ok((await evaluate("document.querySelector('#results').textContent")).includes('matched-30'));
    await evaluate("document.querySelector('#previous-page').click()");await until(idle+" && document.querySelectorAll('.result-card').length===20");
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.result-link')].map(node=>node.getAttribute('href'))"),first);
    checks.push('SQLite source filtering precedes pagination over 62 packages; all 32 matching packages appear once');
    await change('agent-filter','dsh');await until(idle+" && document.querySelector('#agent-filter').value==='dsh'");
    await evaluate("document.querySelector('#next-page').click()");await until(idle+" && document.querySelectorAll('.result-card').length===7");
    await change('agent-filter','');await until(idle+" && document.querySelectorAll('.result-card').length===20");
    await change('sort-select','canonical_updated');await until(idle+" && new URL(location.href).searchParams.get('sort')==='canonical_updated'");
    await change('sort-select','name');await until(idle+" && new URL(location.href).searchParams.get('sort')==='name'");
    checks.push('source filter combines full-catalog Agent filtering and explicit sorting');
    await change('recommendation-source-filter',fixture.moreSourceId);
    await until(idle+" && new URL(location.href).searchParams.get('recommendationSource')==="+JSON.stringify(fixture.moreSourceId));
    await evaluate("document.querySelector('#query').value='slowterm';document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))");
    await until("document.querySelector('#results').getAttribute('aria-busy')==='true'");
    await change('recommendation-source-filter',fixture.sourceId);
    await evaluate("document.querySelector('#query').value='';document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))");
    await until(idle+" && document.querySelectorAll('.result-card').length===20 && new URL(location.href).searchParams.get('recommendationSource')==="+JSON.stringify(fixture.sourceId));
    await timers.setTimeout(1200);assert.equal(await evaluate("document.querySelectorAll('.result-card').length"),20);
    checks.push('source changes cancel slow searches and reject stale responses');
    await change('scope-filter','discovery');await until(idle+" && document.querySelector('#scope-filter').value==='discovery'");
    assert.equal(await evaluate("getComputedStyle(document.querySelector('.source-filter-control')).display"),'none');
    assert.equal(await evaluate("document.querySelector('#recommendation-source-filter').value"),'');
    await change('scope-filter','catalog');await until(idle+" && document.querySelectorAll('.result-card').length===20 && document.querySelector('#sort-select').value==='canonical_updated'");
    assert.equal(await evaluate("document.querySelector('#source-match-notice').hidden"),true);
    checks.push('discovery scope resets and hides canonical-only source selection');
    await evaluate("location.hash='#/recommendations?revision=filter-one'");
    await until("document.querySelector('.rec-results')?.getAttribute('aria-busy')==='false' && document.querySelector('[data-source]')");
    if(fixture.sourceOnSecondPage){await evaluate("document.querySelector('#rec-next').click()");await until("document.querySelector('.rec-results')?.getAttribute('aria-busy')==='false' && document.querySelector('[data-source]')?.dataset.source==="+JSON.stringify(fixture.sourceId));}
    await evaluate("document.querySelector('[data-source=\"'+"+JSON.stringify(fixture.sourceId)+"+'\"] .rec-catalog-link').click()");
    await until(idle+" && document.querySelectorAll('.result-card').length===20 && new URL(location.href).searchParams.get('recommendationSource')==="+JSON.stringify(fixture.sourceId));
    assert.equal(await evaluate("new URL(location.href).searchParams.get('revision')"),'filter-one');
    await evaluate("document.querySelector('.result-link').click()");await until("document.querySelector('#canonical-version')?.value==='1.0.0' && document.querySelector('.rec-results')?.getAttribute('aria-busy')==='false'");
    assert.ok((await evaluate("document.querySelector('.rec-association').textContent")).includes('1.0.0'));
    await change('canonical-version','2.0.0');await until("document.querySelector('#canonical-version')?.value==='2.0.0' && document.querySelector('.rec-empty')");
    await evaluate("document.querySelector('.back-link').click()");await until(idle);
    checks.push('source browsing links to canonical filter; historical detail does not borrow latest source association');
    current='filter-two';await evaluate("document.querySelector('#next-page').click()");await until(idle+" && document.querySelectorAll('.result-card').length===12");
    assert.ok((await evaluate("document.querySelector('#revision-stamp').textContent")).includes('filter-one'));
    await evaluate("document.querySelector('#refresh-query').click()");await until(idle+" && document.querySelectorAll('.result-card').length===1 && document.querySelector('#revision-stamp').textContent.includes('filter-two')");
    assert.ok((await evaluate("document.querySelector('#results').textContent")).includes('matched-00'));
    checks.push('old filtered revision remains stable; explicit refresh reflects withdrawal and changed canonical latest');
    await evaluate("window.ForgeUI.setLocale('en');window.ForgeUI.setTheme('dark')");
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),true);
    assert.ok((await evaluate("document.querySelector('#source-match-notice').textContent")).includes('Upstream ranks are unknown'));
    await evaluate("document.querySelector('.source-filter-control').scrollIntoView()");
    assert.equal(await evaluate("document.querySelector('.source-filter-control').getBoundingClientRect().top>=document.querySelector('.topbar').getBoundingClientRect().bottom"),true);
    await screenshot('source-filter-mobile-dark.png');
    checks.push('English, dark theme and mobile source selector retain explicit neutral semantics');
    await send('Page.navigate',{url:catalogUrl()},session);await until(idle+" && document.querySelectorAll('.result-card').length===20");
    retired=true;await evaluate("document.querySelector('#next-page').click()");await until("!document.querySelector('#query-error').hidden");
    assert.equal(await evaluate("document.querySelectorAll('.result-card').length"),20);
    assert.equal(await evaluate("document.querySelector('#next-page').disabled"),true);
    await evaluate("document.querySelector('#refresh-query').click()");await until(idle+" && document.querySelectorAll('.result-card').length===1");
    checks.push('expired filtered cursor retains last results and refreshes with the same explicit source');
    assert.equal(unmatched.length,0,JSON.stringify(unmatched));assert.equal(exceptions.length,0,JSON.stringify(exceptions));
    assert.equal(requests.some(r=>r.cookie||r.authorization),false);
    assert.equal(network.filter(url=>url.includes('/data/')).length,0);
    for(const event of networkEvents){
      const url=new URL(event.request.url);
      if(url.hostname==='me.kis.v2.scr.kaspersky-labs.com' && /^\/[A-F0-9-]+\/main\.js$/.test(url.pathname) && event.type==='Script' && event.initiator.type==='parser' && event.initiator.url.startsWith(base) && networkFailures.some(item=>item.requestId===event.requestId&&item.blockedReason==='inspector'))environmentBlockedRequests.push({url:event.request.url,requestId:event.requestId,initiator:event.initiator,blockedReason:'inspector'});
    }
    const environmentUrls=new Set(environmentBlockedRequests.map(item=>item.url));
    assert.equal(network.filter(url=>!url.startsWith(base)&&url!==new URL('/favicon.ico',base).href&&!url.startsWith('about:')&&!url.startsWith('data:')&&!environmentUrls.has(url)).length,0);
    checks.push('no catalog JSON downloads, no external media/plugin requests, omitted credentials and no runtime errors');
  }catch(error){
    failure=error;
    if(send&&session)try{diagnostics=await send('Runtime.evaluate',{expression:"JSON.stringify({url:location.href,html:document.querySelector('main')?.innerHTML})",returnByValue:true},session);}catch(_){}
    throw error;
  }finally{
    fs.writeFileSync(path.join(artifacts,'source-filter-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,networkFailures,environmentBlockedRequests,exceptions,unmatched,diagnostics,error:failure&&failure.message,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(send)try{await send('Browser.close');}catch(_){}
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
    const resolved=path.resolve(profile),tempRoot=path.resolve(os.tmpdir());
    assert.ok(resolved.startsWith(tempRoot+path.sep)&&path.basename(resolved).startsWith('forge-source-filter-browser-'));
    for(let attempt=0;attempt<6;attempt++){try{fs.rmSync(resolved,{recursive:true,force:true});break;}catch(error){if(attempt===5){if(!failure)throw error;}else await timers.setTimeout(100*(attempt+1));}}
  }
});
